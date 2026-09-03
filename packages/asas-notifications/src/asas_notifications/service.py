"""Emitter seam + routing policy + dispatcher (WXL-222; DR 0003).

- Producers call ``notify`` inside their own transaction — the insert IS the
  enqueue — passing the **application action** that caused the emit and the
  four axes (topic/nature/urgency/reason). No registration: the action is a
  reference, not a declaration; ``register_kind`` survives one release as a
  deprecating shim (DR 0003 I-3).
- Routing resolves per channel, most specific wins: topic policy row → axis
  policy row → the built-in fallback, which is exactly the pre-0.16 rule —
  ``low`` is in-app only (ambient activity never emails you — the epic's KPI),
  ``normal``/``high`` add an email delivery row. Empty policy tables therefore
  reproduce 0.15 behavior bit-for-bit (the DR's equivalence guarantee).
  ``in_app`` is the notification row itself; a policy that disables it for an
  emit suppresses the whole insert (no row, no anchor for deliveries).
- The dispatcher is queue-shaped but v1-simple: an after-commit hook plus a
  startup/periodic sweep (same self-heal pattern as ``search/semantic.py``), core
  SQL only (an ORM session inside ``after_commit`` would re-fire the hook). Send
  failures never fail the producing transaction; a real worker can replace this
  later with zero schema change.
"""

import logging
import warnings
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable, Optional, Sequence

from sqlalchemy import and_ as sa_and
from sqlalchemy import func as sa_func
from sqlalchemy import or_ as sa_or
from sqlalchemy import select as sa_select
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from .channels import DeliveryPayload, SkipDelivery, adapter_for
from .models import (
    Category,
    DeliveryStatus,
    Nature,
    Notification,
    NotificationChannelPolicy,
    NotificationDelivery,
    NotificationTopic,
    Reason,
    Urgency,
)

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
# A `sending` claim older than this belongs to a crashed pass and reclaims to
# pending. Claims are held per row, only for the duration of one adapter send
# (SMTP timeout is 30s), so five minutes is comfortably past any live send.
STALE_CLAIM_SECONDS = 300

# ── legacy kind shim (DR 0003 I-3 — one release, then removed) ───────────────

IN_APP = "in_app"
#: The seeded platform topic: the designated home for ad hoc emits and for
#: legacy ``register_kind`` specs that predate the topic axis.
DEFAULT_TOPIC = "general"


@dataclass(frozen=True)
class KindSpec:
    category: Nature  # field name kept for one release — hosts introspect it
    urgency: Urgency
    reason: Reason
    topic: str = DEFAULT_TOPIC


_KINDS: dict[str, KindSpec] = {}


def register_kind(
    kind: str,
    *,
    category: Nature,
    urgency: Urgency,
    reason: Reason,
    topic: str = DEFAULT_TOPIC,
) -> None:
    """DEPRECATED (DR 0003): the kind catalog is gone — pass the action and the
    four axes on :func:`notify` instead. This shim keeps a 0.15 wiring working
    for one release: a registered kind supplies axis defaults when ``notify``
    is called with its name and no axes; ``topic`` defaults to the seeded
    ``general`` topic so a legacy emit always passes topic validation."""
    warnings.warn(
        "register_kind() is deprecated: pass action= and the four axes "
        "(topic/nature/urgency/reason) on notify() instead (DR 0003)",
        DeprecationWarning,
        stacklevel=2,
    )
    _KINDS[kind] = KindSpec(Nature(category), Urgency(urgency), Reason(reason), topic)


def registered_kinds() -> dict[str, KindSpec]:
    return dict(_KINDS)


# ── app seams (wired in notifications_wiring.py) ─────────────────────────────

# (session) -> (user_id, org_id) of the current request, or None outside one.
_context_resolver: Optional[Callable[[Session], Optional[tuple[int, int]]]] = None
# (session, user_ids, entity_type, entity_id, record) -> user_ids allowed to
# know the subject exists. `record` is None when the producer did not have the
# row; the id is always passed so the filter can resolve it itself.
_recipient_filter: Optional[
    Callable[[Session, Sequence[int], str, Optional[int], Any], Sequence[int]]
] = None


def configure_context_resolver(
    fn: Optional[Callable[[Session], Optional[tuple[int, int]]]]
) -> None:
    """The resolver is consulted on read paths too (feed, counts, ownership
    checks), not only at emit — it must return ``None`` cheaply outside a
    request rather than raise, per its type: ``(session) -> (user_id, org_id)
    or None``."""
    global _context_resolver
    _context_resolver = fn


def configure_recipient_filter(
    fn: Optional[Callable[[Session, Sequence[int], str, Optional[int], Any], Sequence[int]]]
) -> None:
    """Install the host's visibility filter for notification recipients.

    Called as ``fn(session, user_ids, entity_type, entity_id, record)`` for every
    ``notify`` that names an ``entity_type``, and must return the subset of
    ``user_ids`` allowed to know the subject exists.

    ``record`` is the subject row **when the producer had it**, and ``None`` when
    it did not — a generic producer may hold only the type and the id. The filter
    is handed both so it can resolve the row itself in that case; returning
    ``user_ids`` unchanged is the right answer for an entity type that needs no
    filtering.

    Filtering has to happen here, before the rows are written: a notification is
    a **copy** of a fact, so there is no redaction pass afterwards.
    """
    global _recipient_filter
    _recipient_filter = fn


def current_user_id(session: Session) -> Optional[int]:
    ctx = _context_resolver(session) if _context_resolver else None
    return ctx[0] if ctx else None


def current_org_id(session: Session) -> Optional[int]:
    """The request's org, when a context resolver is configured and inside a
    request. Feed/read/archive queries constrain on it *in addition to*
    ``user_id`` — defense in depth for multi-tenant hosts: host-level tenancy
    listeners remain the first line, this is the second. Outside a request (or
    with no resolver) it is None and no org constraint applies — single-tenant
    behavior is unchanged."""
    ctx = _context_resolver(session) if _context_resolver else None
    return ctx[1] if ctx else None


def _recipient_conditions(session: Session, user_id: int) -> list:
    """THE tenancy chokepoint: every recipient-facing query builds its WHERE
    from this list, so the org guard cannot be forgotten at one site. Keep new
    feed/count/bulk queries on it."""
    conditions = [Notification.user_id == user_id]
    org_id = current_org_id(session)
    if org_id is not None:
        conditions.append(Notification.org_id == org_id)
    return conditions


# ── routing policy (DR 0003 S-5) ─────────────────────────────────────────────

# Config reads ride the hot emit path, so topic keys and policy rows are cached
# in-process for a short TTL — admin changes propagate within a minute across
# replicas, with no invalidation bus (deliberate; DR 0003).
CONFIG_TTL_SECONDS = 60
_topic_cache: dict[str, tuple[datetime, frozenset]] = {}
_policy_cache: dict[Optional[int], tuple[datetime, tuple]] = {}


def config_cache_clear() -> None:
    """Drop the cached topic/policy config (tests; admin APIs after a write)."""
    _topic_cache.clear()
    _policy_cache.clear()


def _fresh(entry) -> bool:
    return entry is not None and (
        (datetime.utcnow() - entry[0]).total_seconds() < CONFIG_TTL_SECONDS
    )


def _all_topic_keys(session: Session, *, refresh: bool = False) -> frozenset:
    """Every topic key in the table (any org). Validation is org-agnostic —
    its job is catching typos and unseeded topics, and a key seeded for any
    org is not a typo; org scoping happens in policy resolution, which just
    falls back for a topic with no rows for this org."""
    entry = None if refresh else _topic_cache.get("all")
    if not _fresh(entry):
        rows = session.exec(select(NotificationTopic.key)).all()
        entry = (datetime.utcnow(), frozenset(rows))
        _topic_cache["all"] = entry
    return entry[1]


def _topic_known(session: Session, topic: str) -> bool:
    """Membership with a fresh re-query on miss: a topic seeded on another
    replica inside the TTL window must degrade to one extra SELECT, never to a
    transaction-aborting false LookupError. (The inverse staleness — a key
    cached from a transaction that later rolled back — expires with the TTL;
    the emit it would wrongly admit routes to fallback policy and is
    harmless.)"""
    if topic in _all_topic_keys(session):
        return True
    return topic in _all_topic_keys(session, refresh=True)


@dataclass(frozen=True)
class _PolicyRow:  # a detached, cache-safe copy of NotificationChannelPolicy
    id: int
    org_id: Optional[int]
    topic: Optional[str]
    urgency: Optional[str]
    nature: Optional[str]
    channel: str
    enabled: bool
    mandatory: bool


def _policy_rows(session: Session, org: Optional[int]) -> tuple:
    entry = _policy_cache.get(org)
    if not _fresh(entry):
        rows = session.exec(
            select(NotificationChannelPolicy).where(
                sa_or(
                    NotificationChannelPolicy.org_id.is_(None),
                    NotificationChannelPolicy.org_id == org,
                )
            )
        ).all()
        entry = (
            datetime.utcnow(),
            tuple(
                _PolicyRow(
                    id=r.id,
                    org_id=r.org_id,
                    topic=r.topic,
                    urgency=r.urgency.value if r.urgency else None,
                    nature=r.nature.value if r.nature else None,
                    channel=r.channel,
                    enabled=r.enabled,
                    mandatory=r.mandatory,
                )
                for r in rows
            ),
        )
        _policy_cache[org] = entry
    return entry[1]


def resolve_channels(
    session: Session,
    org: Optional[int],
    *,
    topic: str,
    nature: Nature,
    urgency: Urgency,
) -> dict[str, bool]:
    """The effective channel set for one emit: ``{channel: mandatory}`` for
    every **enabled** channel.

    Per channel, most specific wins (DR 0003 S-5): a topic row beats an axis
    row beats the built-in fallback; within a tier an org override row beats a
    platform row, and an axis row matching more fields beats one matching
    fewer. The fallback is the pre-0.16 rule in code — ``low`` → in-app only,
    else in-app + email — so empty policy tables reproduce 0.15 routing
    exactly. Reason is not a policy condition yet (it joins with U-3's
    preference layer)."""
    rows = _policy_rows(session, org)
    channels = {IN_APP, "email"} | {r.channel for r in rows}
    resolved: dict[str, bool] = {}
    for channel in channels:
        topic_rows = [r for r in rows if r.channel == channel and r.topic == topic and r.topic is not None]
        axis_rows = [
            r
            for r in rows
            if r.channel == channel
            and r.topic is None
            and (r.urgency is None or r.urgency == urgency.value)
            and (r.nature is None or r.nature == nature.value)
        ]
        pick = None
        # Ties (equally specific duplicate rows — the table has no uniqueness
        # constraint) resolve to the NEWEST row: an admin's latest change must
        # take effect, never be shadowed by a stale predecessor.
        if topic_rows:
            pick = max(topic_rows, key=lambda r: (r.org_id is not None, r.id))
        elif axis_rows:
            pick = max(
                axis_rows,
                key=lambda r: (
                    r.org_id is not None,
                    (r.urgency is not None) + (r.nature is not None),
                    r.id,
                ),
            )
        if pick is not None:
            if pick.enabled:
                resolved[channel] = pick.mandatory
        elif channel == IN_APP:
            resolved[channel] = False
        elif channel == "email" and urgency is not Urgency.low:
            resolved[channel] = False
    return resolved


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
    action: Optional[str] = None,
    *,
    topic: Optional[str] = None,
    nature: Optional[Nature] = None,
    urgency: Optional[Urgency] = None,
    reason: Optional[Reason] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    link: Optional[str] = None,
    template: Optional[str] = None,
    data: Optional[dict] = None,
    actor_user_id: Optional[int] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    org_id: Optional[int] = None,
    record: Any = None,
    coalesce_unread: bool = False,
    merge_body: Optional[Callable[[Optional[str], Optional[str]], Optional[str]]] = None,
    category: Optional[Nature] = None,  # deprecated alias for nature (0.15 name)
    kind: Optional[str] = None,  # deprecated alias for action (0.15 name)
) -> list[Notification]:
    """Insert notification (+ delivery) rows in the caller's transaction.

    DR 0003: ``action`` is the application action that caused this emit
    (``"job.publish"`` — imperative, the app's own vocabulary), a *reference
    without declaration*: provenance, the coalescing identity, and nothing
    else. ``None`` marks an ad hoc one-off, which never coalesces. The four
    axes travel on the call; ``topic`` is required with an action (ad hoc
    emits land in the seeded ``general`` topic) and must exist in
    ``notification_topic`` — the one fail-loud reference, because policy and
    preferences key on it. Channels come from :func:`resolve_channels`; when
    policy disables ``in_app`` for this emit nothing is inserted at all (the
    row is both the feed entry and the anchor for delivery rows).

    ``template=`` and ``data=`` are stored for U-4's renderer (and a possible
    read-time feed renderer later); ``title`` remains required until then.

    - Actor exclusion is built in: ``actor_user_id`` never notifies itself.
    - Notifications are tenant-owned: ``org_id`` is stamped from the explicit
      parameter, else the context resolver; with neither, ``ValueError`` at the
      emit site — background producers acting *for* a tenant pass the org
      explicitly (DR 0001 T4/T7).
    - **Whenever ``entity_type`` is given**, recipients run through the configured
      visibility filter — a notification must never leak a private record (the
      search-index rule). ``record`` is passed to the filter when the producer
      has it and is ``None`` otherwise; the filter always receives ``entity_id``
      and decides.
    - ``coalesce_unread`` (TEAMY-298): an UNREAD row for the same (org,
      recipient, action, entity) is updated in place — title/body replaced
      (``merge_body(old, new)`` when given), latest ``data`` wins, ``created_at``
      refreshed — instead of inserting, so an edit burst stays one live bell
      entry. Only ambient emits coalesce: it requires an action and an entity
      key and is ignored whenever the emit routes to external channels, and
      read **or archived** rows are never rewritten.

    The caller owns the commit — the insert rides the producing transaction, so a
    notification exists iff the domain change committed.
    """
    # ── legacy shims (one release — DR 0003 I-2/I-3) ──
    if kind is not None:
        warnings.warn(
            "notify(kind=...) is deprecated: the parameter is action= now",
            DeprecationWarning,
            stacklevel=2,
        )
        action = action if action is not None else kind
    if category is not None:
        warnings.warn(
            "notify(category=...) is deprecated: the axis is nature= now",
            DeprecationWarning,
            stacklevel=2,
        )
        nature = nature if nature is not None else category
    # The shim applies ONLY to fully-legacy calls (no axis passed at all): a
    # call site that states even one axis has been converted and must get the
    # new fail-loud contract, not silent backfill from a spec that will be
    # deleted next release.
    if (
        action is not None
        and nature is None
        and urgency is None
        and reason is None
        and topic is None
        and (spec := _KINDS.get(action)) is not None
    ):
        warnings.warn(
            f"notify({action!r}) is using register_kind() defaults — pass the "
            "four axes explicitly; the kind shim goes away next release (DR 0003)",
            DeprecationWarning,
            stacklevel=2,
        )
        nature, urgency, reason, topic = spec.category, spec.urgency, spec.reason, spec.topic

    missing = [
        name
        for name, value in (("nature", nature), ("urgency", urgency), ("reason", reason))
        if value is None
    ]
    if missing:
        raise TypeError(
            f"notify() is missing the {', '.join(missing)} axis/axes: pass them "
            "explicitly — there is no kind catalog to default from (DR 0003)"
        )
    if action is not None and topic is None:
        raise TypeError(
            "notify(action=...) requires topic= — the management/preference "
            "axis every emit must carry (DR 0003)"
        )
    if topic is None:
        topic = DEFAULT_TOPIC  # ad hoc emits land in the seeded general topic
    if title is None:
        raise TypeError("notify() requires title= (template rendering lands with U-4)")
    nat = Nature(nature)
    urg = Urgency(urgency)
    rsn = Reason(reason)

    # The one reference an emit can get wrong that management depends on:
    # policy rows and (U-3) preferences key on topic, so an unknown topic is a
    # catalog mistake and fails loud — INSIDE suppressed() too, exactly like
    # 0.15's unregistered-kind error: suppression silences delivery, never
    # call-site mistakes.
    if not _topic_known(session, topic):
        raise LookupError(
            f"unknown notification topic {topic!r}: seed it in "
            "notification_topic (platform row) before emitting into it"
        )

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
    ids = list(dict.fromkeys(u for u in recipients if u is not None))
    if actor_user_id is not None:
        ids = [u for u in ids if u != actor_user_id]
    if record is not None and not entity_type and _recipient_filter is not None:
        # "must never leak a private record" is only enforceable when the
        # filter can actually run. A record without its entity_type used to
        # skip filtering silently — the wrong default for a rule stated as
        # "never": fail loud at the producer instead.
        #
        # Conditioned on a filter being configured: a host that has none has
        # declared nothing restricted, so a stray `record` is merely redundant.
        raise ValueError(
            "notify(record=...) requires entity_type — the visibility "
            "filter cannot run without it"
        )
    if entity_type and _recipient_filter is not None:
        # **The filter runs whenever there is a subject**, not only when the
        # caller happened to pass the row — see configure_recipient_filter.
        ids = list(_recipient_filter(session, ids, entity_type, entity_id, record))
    if not ids:
        return []

    resolved = resolve_channels(session, org, topic=topic, nature=nat, urgency=urg)
    if IN_APP not in resolved:
        # The notification row is both the feed entry and the FK anchor for
        # delivery rows, so "no in_app" means nothing lands anywhere. That is
        # what an admin disabling a topic's in_app asks for (DR 0003 S-5:
        # muted = not inserted); external-without-feed-row would need its own
        # schema and is deliberately unsupported.
        log.info("notify(%s): in_app disabled by policy — nothing inserted", action)
        return []
    external = sorted(c for c in resolved if c != IN_APP)

    coalesce = (
        coalesce_unread
        and not external
        and action is not None
        and entity_type
        and entity_id is not None
    )
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
                    # and overwrite — an org-1 row for the same (user, action,
                    # entity).
                    Notification.org_id == org,
                    Notification.action == action,
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
            # The fold IS the latest event, so its data, classification and
            # template all come along with its text (DR 0003 S-3: latest data
            # wins, like the title) — a pre-0004 row (topic NULL) gets labeled
            # on first fold. Keeping a PREVIOUS fold's data (or template) while
            # taking the new one's counterpart would hand U-4's renderer a
            # template/data pairing no single emit ever produced.
            existing.data = data
            existing.topic = topic
            existing.template = template
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
            action=action,
            topic=topic,
            nature=nat,
            urgency=urg,
            reason=rsn,
            entity_type=entity_type,
            entity_id=entity_id,
            title=title,
            body=body,
            link=link,
            template=template,
            data=data,
        )
        session.add(n)
        created.append(n)
    session.flush()  # ids for the delivery rows
    for n in created:
        for channel in external:
            session.add(NotificationDelivery(notification_id=n.id, channel=channel))
    return updated + created


# ── feed / read state ────────────────────────────────────────────────────────


def unread_count(session: Session, user_id: int) -> int:
    """Unread rows still in the inbox. Archived rows are excluded — they have left
    the recipient's list, so counting them would leave a badge pointing at nothing.

    Counted in SQL (it used to fetch every id and ``len()`` them) and org-scoped
    when a request context is available."""
    return session.exec(
        select(sa_func.count())
        .select_from(Notification)
        .where(
            *_recipient_conditions(session, user_id),
            Notification.read_at.is_(None),
            Notification.archived_at.is_(None),
        )
    ).one()


def list_feed(
    session: Session,
    user_id: int,
    *,
    state: str = "open",
    unread_only: bool = False,
    nature: Optional[Nature] = None,
    page: int = 1,
    page_size: int = 20,
    category: Optional[Nature] = None,  # deprecated alias for nature (0.15 name)
) -> tuple[list[Notification], int]:
    """One page of the recipient's feed plus the filtered total, paged in SQL.

    The single feed query in the package — the router stays thin (the
    asas-lookups service/router split), and a host digest job can call this
    directly. ``total`` (COUNT) and the page SELECT are two statements with no
    shared snapshot: a commit landing between them can skew total against the
    page by a row — the standard COUNT + LIMIT/OFFSET trade, transient and
    self-healing on the next poll."""
    if category is not None:
        warnings.warn(
            "list_feed(category=...) is deprecated: the parameter is nature= now",
            DeprecationWarning,
            stacklevel=2,
        )
        nature = nature if nature is not None else category
    conditions = _recipient_conditions(session, user_id)
    if state == "open":
        conditions.append(Notification.archived_at.is_(None))
    elif state == "archived":
        conditions.append(Notification.archived_at.is_not(None))
    if unread_only:
        conditions.append(Notification.read_at.is_(None))
    if nature is not None:
        conditions.append(Notification.nature == nature)
    total = session.exec(
        select(sa_func.count()).select_from(Notification).where(*conditions)
    ).one()
    rows = session.exec(
        select(Notification)
        .where(*conditions)
        .order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return list(rows), total


def _owned(session: Session, user_id: int, notification_id: int) -> Optional[Notification]:
    """The row, iff it belongs to this recipient — and, when a request context
    supplies an org, to this org. A cross-org id probe answers exactly like a
    missing row (404 at the router), never confirming the row exists."""
    n = session.get(Notification, notification_id)
    if n is None or n.user_id != user_id:
        return None
    org_id = current_org_id(session)
    if org_id is not None and n.org_id != org_id:
        return None
    return n


def mark_read(session: Session, user_id: int, notification_id: int) -> Optional[Notification]:
    """Mark one owned row read (idempotent); None when :func:`_owned` says the
    row is not this recipient's — or, under an org context, not this org's."""
    n = _owned(session, user_id, notification_id)
    if n is None:
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
    result = session.execute(
        sa_update(Notification)
        .where(*_recipient_conditions(session, user_id))
        .where(Notification.read_at.is_(None))
        .values(read_at=datetime.utcnow())
    )
    session.commit()
    return result.rowcount


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
    n = _owned(session, user_id, notification_id)
    if n is None:
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
    n = _owned(session, user_id, notification_id)
    if n is None:
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
    result = session.execute(
        sa_update(Notification)
        .where(*_recipient_conditions(session, user_id))
        .where(Notification.read_at.is_not(None))
        .where(Notification.archived_at.is_(None))
        .values(archived_at=datetime.utcnow())
    )
    session.commit()
    return result.rowcount


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
                _notification_t.c.action,
                _notification_t.c.topic,
                _notification_t.c.nature,
                _notification_t.c.urgency,
                _notification_t.c.reason,
                _notification_t.c.title,
                _notification_t.c.body,
                _notification_t.c.link,
                _notification_t.c.data,
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
            action=r.action,
            topic=r.topic,
            nature=r.nature,
            urgency=r.urgency,
            reason=r.reason,
            title=r.title,
            body=r.body,
            link=r.link,
            data=r.data,
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
