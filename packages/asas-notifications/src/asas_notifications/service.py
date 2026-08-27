"""Emitter seam + routing policy + dispatcher (WXL-222).

- Producers ``register_kind`` at wiring time (defaults: category/urgency/reason) and
  call ``notify`` inside their own transaction — the insert IS the enqueue.
- The routing policy maps urgency to external channels: ``low`` is in-app only
  (ambient activity never emails you — the epic's KPI), ``normal``/``high`` get an
  email delivery row. ``in_app`` is intrinsic: the notification row itself.
- The dispatcher is queue-shaped but v1-simple: an after-commit hook plus a
  startup/periodic sweep (same self-heal pattern as ``search/semantic.py``), core
  SQL only (an ORM session inside ``after_commit`` would re-fire the hook). Send
  failures never fail the producing transaction; a real worker can replace this
  later with zero schema change.
"""

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional, Sequence

from sqlalchemy import and_ as sa_and
from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from .channels import DeliveryPayload, SkipDelivery, adapter_for
from .models import (
    Category,
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    Reason,
    Urgency,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# A `sending` claim older than this belongs to a crashed pass and reclaims to
# pending. Claims are held per row, only for the duration of one adapter send
# (SMTP timeout is 30s), so five minutes is comfortably past any live send.
STALE_CLAIM_SECONDS = 300

# ── kind registry ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class KindSpec:
    category: Category
    urgency: Urgency
    reason: Reason


_KINDS: dict[str, KindSpec] = {}


def register_kind(
    kind: str, *, category: Category, urgency: Urgency, reason: Reason
) -> None:
    """Producers declare a kind's defaults at wiring time. Emitting an unregistered
    kind fails loud — the catalog in the epic is the source of truth."""
    _KINDS[kind] = KindSpec(Category(category), Urgency(urgency), Reason(reason))


def registered_kinds() -> dict[str, KindSpec]:
    return dict(_KINDS)


# ── app seams (wired in notifications_wiring.py) ─────────────────────────────

# (session) -> (user_id, org_id) of the current request, or None outside one.
_context_resolver: Optional[Callable[[Session], Optional[tuple[int, int]]]] = None
# (session, user_ids, entity_type, record) -> user_ids allowed to see the record.
_recipient_filter: Optional[
    Callable[[Session, Sequence[int], str, Any], Sequence[int]]
] = None


def configure_context_resolver(
    fn: Optional[Callable[[Session], Optional[tuple[int, int]]]]
) -> None:
    global _context_resolver
    _context_resolver = fn


def configure_recipient_filter(
    fn: Optional[Callable[[Session, Sequence[int], str, Any], Sequence[int]]]
) -> None:
    global _recipient_filter
    _recipient_filter = fn


def current_user_id(session: Session) -> Optional[int]:
    ctx = _context_resolver(session) if _context_resolver else None
    return ctx[0] if ctx else None


# ── routing policy ────────────────────────────────────────────────────────────


def _channels_for(category: Category, urgency: Urgency, reason: Reason) -> list[str]:
    """urgency low → in-app only; normal/high → email. Later: filtered by per-user
    (reason × category) → channel preferences — the schema already carries all keys."""
    if urgency is Urgency.low:
        return []
    return ["email"]


# ── emit ──────────────────────────────────────────────────────────────────────

_suppress_notify: ContextVar[bool] = ContextVar("notifications_suppressed", default=False)


@contextmanager
def suppressed():
    """No-op every ``notify`` inside this context (TEAMY-476).

    For bulk writers (the work import) that deliberately reuse the normal
    routers: per-record notification fan-out would be a storm, so the bulk
    caller suppresses it and emits its own coalesced digest afterwards.
    Unregistered kinds still fail loud — suppression silences delivery,
    never catalog mistakes."""
    token = _suppress_notify.set(True)
    try:
        yield
    finally:
        _suppress_notify.reset(token)


def notify(
    session: Session,
    recipients: Iterable[int],
    kind: str,
    *,
    title: str,
    actor_user_id: Optional[int] = None,
    body: Optional[str] = None,
    link: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    org_id: Optional[int] = None,
    record: Any = None,
    category: Optional[Category] = None,
    urgency: Optional[Urgency] = None,
    reason: Optional[Reason] = None,
    coalesce_unread: bool = False,
    merge_body: Optional[Callable[[Optional[str], Optional[str]], Optional[str]]] = None,
) -> list[Notification]:
    """Insert notification (+ delivery) rows in the caller's transaction.

    - Actor exclusion is built in: ``actor_user_id`` never notifies itself.
    - Notifications are tenant-owned: ``org_id`` is stamped from the explicit
      parameter, else the context resolver; with neither, ``ValueError`` at the
      emit site — background producers acting *for* a tenant pass the org
      explicitly (DR 0001 T4/T7).
    - When ``record`` is passed, recipients are filtered through the configured
      visibility filter (``access.can_view_record`` via wiring) — a notification
      must never leak a private record (the search-index rule).
    - category/urgency/reason default from the registered kind; producers override
      per-emit only when the event is genuinely ambiguous (e.g. an @mention that
      carries an explicit ask). Never inferred from message text.
    - ``coalesce_unread`` (TEAMY-298): an UNREAD row for the same (recipient,
      kind, entity) is updated in place — title/body replaced (``merge_body(old,
      new)`` when given), ``created_at`` refreshed — instead of inserting, so an
      edit burst stays one live bell entry. Only ambient emits coalesce: it
      requires an entity key and is ignored whenever the emit routes to external
      channels (each email-worthy event stays a discrete row), and read **or
      archived** rows are never rewritten — merging into a row the recipient can
      no longer see would drop the event.

    The caller owns the commit — the insert rides the producing transaction, so a
    notification exists iff the domain change committed.
    """
    spec = _KINDS.get(kind)
    if spec is None:
        raise LookupError(f"unregistered notification kind: {kind}")
    if _suppress_notify.get():
        return []
    # Notifications are tenant-owned and ``Notification.org_id`` is NOT NULL.
    # Stamping order (DR 0001 T4, issue #27): explicit parameter → context
    # resolver → fail loud HERE, at the emit site, with the fix in the message
    # — never as an engine-specific IntegrityError at flush, which would also
    # take the producer's whole transaction down with it (audit defect T-2).
    org = org_id
    if org is None:
        ctx = _context_resolver(session) if _context_resolver else None
        org = ctx[1] if ctx else None
    if org is None:
        raise ValueError(
            "notify() has no org for this emit: pass org_id= explicitly "
            "(background jobs, CLI, boot sweeps) or configure the context "
            "resolver — Notification.org_id is NOT NULL"
        )
    cat = Category(category) if category else spec.category
    urg = Urgency(urgency) if urgency else spec.urgency
    rsn = Reason(reason) if reason else spec.reason

    ids = list(dict.fromkeys(u for u in recipients if u is not None))
    if actor_user_id is not None:
        ids = [u for u in ids if u != actor_user_id]
    if record is not None and _recipient_filter is not None:
        # "must never leak a private record" is only enforceable when the
        # filter can actually run. A record without its entity_type used to
        # skip filtering silently — the wrong default for a rule stated as
        # "never": fail loud at the producer instead.
        if not entity_type:
            raise ValueError(
                "notify(record=...) requires entity_type — the visibility "
                "filter cannot run without it"
            )
        ids = list(_recipient_filter(session, ids, entity_type, record))
    if not ids:
        return []

    channels = _channels_for(cat, urg, rsn)
    coalesce = coalesce_unread and not channels and entity_type and entity_id is not None
    updated: list[Notification] = []
    if coalesce:
        remaining: list[int] = []
        for user_id in ids:
            existing = session.exec(
                select(Notification)
                .where(
                    Notification.user_id == user_id,
                    # The org axis is part of the coalesce identity (DR 0001
                    # T5, defect T-6): where hosts' entity ids are not
                    # globally unique, an org-2 emit must never fold into —
                    # and overwrite — an org-1 row for the same (user, kind,
                    # entity).
                    Notification.org_id == org,
                    Notification.kind == kind,
                    Notification.entity_type == entity_type,
                    Notification.entity_id == entity_id,
                    Notification.read_at.is_(None),
                    # An archived row has left the recipient's inbox. Folding a
                    # new event into it would update something they can no
                    # longer see in the default feed — the event would land
                    # nowhere. Coalescing only ever merges into a LIVE row.
                    Notification.archived_at.is_(None),
                )
                .order_by(Notification.created_at.desc())
            ).first()
            if existing is None:
                remaining.append(user_id)
                continue
            existing.title = title
            existing.body = merge_body(existing.body, body) if merge_body else body
            existing.created_at = datetime.utcnow()
            session.add(existing)
            updated.append(existing)
        ids = remaining
        if not ids:
            return updated

    created: list[Notification] = []
    for user_id in ids:
        n = Notification(
            user_id=user_id,
            org_id=org,
            kind=kind,
            category=cat,
            urgency=urg,
            reason=rsn,
            entity_type=entity_type,
            entity_id=entity_id,
            title=title,
            body=body,
            link=link,
        )
        session.add(n)
        created.append(n)
    session.flush()  # ids for the delivery rows
    for n in created:
        for channel in channels:
            session.add(NotificationDelivery(notification_id=n.id, channel=channel))
    return updated + created


# ── feed / read state ────────────────────────────────────────────────────────


def unread_count(session: Session, user_id: int) -> int:
    """Unread rows still in the inbox. Archived rows are excluded — they have left
    the recipient's list, so counting them would leave a badge pointing at nothing."""
    rows = session.exec(
        select(Notification.id).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.archived_at.is_(None),
        )
    ).all()
    return len(rows)


def mark_read(session: Session, user_id: int, notification_id: int) -> Optional[Notification]:
    n = session.get(Notification, notification_id)
    if n is None or n.user_id != user_id:
        return None
    if n.read_at is None:
        n.read_at = datetime.utcnow()
        session.add(n)
        session.commit()
        session.refresh(n)
    return n


def mark_all_read(session: Session, user_id: int) -> int:
    """Every unread row, archived ones included — a superset of what
    :func:`unread_count` counts, so this can never leave the badge non-zero."""
    rows = session.exec(
        select(Notification).where(
            Notification.user_id == user_id, Notification.read_at.is_(None)
        )
    ).all()
    now = datetime.utcnow()
    for n in rows:
        n.read_at = now
        session.add(n)
    session.commit()
    return len(rows)


# ── archive state ────────────────────────────────────────────────────────────
#
# The second axis: `read_at` is seen, `archived_at` is dealt with. Kept apart so a
# host can keep an actionable notification in front of the recipient after they
# have read it, and clear it only when they act on it or file it away.


def archive(session: Session, user_id: int, notification_id: int) -> Optional[Notification]:
    """Idempotent: archiving an archived row is a no-op, not an error.

    Sequentially that also keeps the original timestamp; two *concurrent*
    archives of the same row can race and the later write wins, since this is a
    read-then-write like ``mark_read`` beside it rather than a CAS like the
    dispatcher's claim. Deliberate — the dispatcher CASes because losing that
    race sends a duplicate email, while losing this one moves a timestamp by
    milliseconds on a row that ends archived either way.
    """
    n = session.get(Notification, notification_id)
    if n is None or n.user_id != user_id:
        return None
    if n.archived_at is None:
        n.archived_at = datetime.utcnow()
        session.add(n)
        session.commit()
        session.refresh(n)
    return n


def unarchive(session: Session, user_id: int, notification_id: int) -> Optional[Notification]:
    """Back into the inbox. Read state is untouched — the two axes are independent,
    so restoring a row does not make it unread again."""
    n = session.get(Notification, notification_id)
    if n is None or n.user_id != user_id:
        return None
    if n.archived_at is not None:
        n.archived_at = None
        session.add(n)
        session.commit()
        session.refresh(n)
    return n


def archive_read(session: Session, user_id: int) -> int:
    """Bulk "clear what I've dealt with": archives the recipient's read rows and
    leaves unread ones alone. Never archives unread rows — that would hide
    something the recipient has not seen."""
    rows = session.exec(
        select(Notification).where(
            Notification.user_id == user_id,
            Notification.read_at.is_not(None),
            Notification.archived_at.is_(None),
        )
    ).all()
    now = datetime.utcnow()
    for n in rows:
        n.archived_at = now
        session.add(n)
    session.commit()
    return len(rows)


# ── dispatcher (after-commit + sweep) ────────────────────────────────────────

_notification_t = Notification.__table__
_delivery_t = NotificationDelivery.__table__


def _stale_cutoff() -> datetime:
    return datetime.utcnow() - timedelta(seconds=STALE_CLAIM_SECONDS)


def has_pending(conn) -> bool:
    row = conn.execute(
        sa_select(_delivery_t.c.id)
        .where(
            sa_or(
                sa_and(
                    _delivery_t.c.status == DeliveryStatus.pending.value,
                    _delivery_t.c.attempts < MAX_ATTEMPTS,
                ),
                # A crashed pass's stale claim counts as pending — otherwise it
                # would only reclaim once some unrelated new row shows up.
                sa_and(
                    _delivery_t.c.status == DeliveryStatus.sending.value,
                    _delivery_t.c.claimed_at < _stale_cutoff(),
                ),
            )
        )
        .limit(1)
    ).first()
    return row is not None


def _finish(engine, delivery_id: int, **values) -> None:
    values.setdefault("claimed_at", None)
    with engine.begin() as conn:
        conn.execute(
            sa_update(_delivery_t).where(_delivery_t.c.id == delivery_id).values(**values)
        )


def dispatch_pending(engine, *, limit: int = 100) -> int:
    """Send pending deliveries through their channel adapters. Returns the number
    of rows that reached a terminal-or-retried state this pass. Failed sends stay
    retryable until ``MAX_ATTEMPTS``; a missing adapter or ``SkipDelivery`` marks
    the row skipped.

    Duplicate-safe under concurrent passes (TEAMY-475): each row is claimed with
    a rows-affected CAS UPDATE (pending → sending) committed *before* the adapter
    send, so an overlapping pass — the after-commit hook racing the 60s job, or a
    second app instance — loses the CAS and skips the row instead of re-sending
    it. The send itself runs outside any transaction; the outcome commits in a
    short follow-up transaction. Claims left by a crashed process reclaim to
    pending after ``STALE_CLAIM_SECONDS``. The overall contract stays
    at-least-once (a crash between send and mark re-sends that one row) — same
    as the jobs queue."""
    with engine.begin() as conn:
        conn.execute(
            sa_update(_delivery_t)
            .where(_delivery_t.c.status == DeliveryStatus.sending.value)
            .where(_delivery_t.c.claimed_at < _stale_cutoff())
            .values(status=DeliveryStatus.pending.value, claimed_at=None)
        )
        rows = conn.execute(
            sa_select(
                _delivery_t.c.id,
                _delivery_t.c.notification_id,
                _delivery_t.c.channel,
                _delivery_t.c.attempts,
                _notification_t.c.user_id,
                _notification_t.c.org_id,
                _notification_t.c.kind,
                _notification_t.c.category,
                _notification_t.c.urgency,
                _notification_t.c.reason,
                _notification_t.c.title,
                _notification_t.c.body,
                _notification_t.c.link,
                _notification_t.c.created_at,
            )
            .select_from(
                _delivery_t.join(
                    _notification_t,
                    _delivery_t.c.notification_id == _notification_t.c.id,
                )
            )
            .where(_delivery_t.c.status == DeliveryStatus.pending.value)
            .where(_delivery_t.c.attempts < MAX_ATTEMPTS)
            .order_by(_delivery_t.c.id)
            .limit(limit)
        ).all()

    handled = 0
    for r in rows:
        with engine.begin() as conn:
            claimed = conn.execute(
                sa_update(_delivery_t)
                .where(_delivery_t.c.id == r.id)
                .where(_delivery_t.c.status == DeliveryStatus.pending.value)
                .values(
                    status=DeliveryStatus.sending.value, claimed_at=datetime.utcnow()
                )
            ).rowcount
        if claimed != 1:
            continue  # another pass owns this row
        adapter = adapter_for(r.channel)
        if adapter is None:
            _finish(
                engine,
                r.id,
                status=DeliveryStatus.skipped.value,
                last_error="no adapter registered for channel",
            )
            handled += 1
            continue
        payload = DeliveryPayload(
            delivery_id=r.id,
            notification_id=r.notification_id,
            channel=r.channel,
            recipient_user_id=r.user_id,
            org_id=r.org_id,
            kind=r.kind,
            category=r.category,
            urgency=r.urgency,
            reason=r.reason,
            title=r.title,
            body=r.body,
            link=r.link,
            created_at=r.created_at,
        )
        try:
            adapter.send(payload)
        except SkipDelivery as exc:
            _finish(
                engine,
                r.id,
                status=DeliveryStatus.skipped.value,
                last_error=str(exc) or None,
            )
        except Exception as exc:  # noqa: BLE001 — any send error is a retryable failure
            attempts = r.attempts + 1
            _finish(
                engine,
                r.id,
                status=(
                    DeliveryStatus.failed.value
                    if attempts >= MAX_ATTEMPTS
                    else DeliveryStatus.pending.value
                ),
                attempts=attempts,
                last_error=str(exc)[:500],
            )
            log.warning("notification delivery %s failed (attempt %s)", r.id, attempts)
        else:
            _finish(
                engine,
                r.id,
                status=DeliveryStatus.sent.value,
                attempts=r.attempts + 1,
                sent_at=datetime.utcnow(),
                last_error=None,
            )
        handled += 1
    return handled
