"""Appending and verifying. The concurrency rule lives here.

:func:`append` puts a row on **the caller's own session** and does not commit. That
is the central promise: the audit entry commits with the business change, in one
transaction, so you can never end up with a record of something that did not
happen, nor a change with no record. A second session, or a helpful ``commit()``
here, would break it into two fates.

**The hard part is the tail read, and it is not the obvious lock.** Appending means
reading the newest entry for this tenant and chaining off it, so two concurrent
appends must not both read the same tail. The instinct is ``SELECT ... FOR UPDATE``
on the tail row, and it does not work: a waiter that took the row lock *before* the
winner's ``INSERT`` is holding a lock on a row that is no longer the tail, so it
proceeds, chains off a stale fingerprint, and **forks the chain**. Nothing fails at
the time. It surfaces later as a verification break that looks like tampering, over
data nobody touched.

The fix is a lock that exists independently of any row, taken **before** the tail
is read: a per-tenant advisory lock, held for the transaction. Per tenant, because
chains are per tenant and one tenant's write rate must not serialise another's.

**Never hold that lock across slow work.** Every append in the tenant queues behind
it, so a hash of a large payload, a network call, or a password KDF inside the same
transaction turns a per-tenant lock into a per-tenant stall. Compute first, append
last.

SQLite has no advisory locks — and its writer serialisation is NOT enough: the
tail READ happens before the INSERT, outside any write lock, so two threads can
both read the same tail and fork the chain even though their INSERTs serialise.
On SQLite the append path therefore holds a process-wide mutex across the tail
read and the flush (single-process is the deployment model SQLite implies; a
multi-process SQLite host is outside this package's guarantee and documented as
such). A UNIQUE(org_id, hash_prev) index backstops both engines declaratively:
a fork that slips past any lock becomes the loser's IntegrityError, never a
silent verification break weeks later.
"""

from __future__ import annotations

import threading
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Optional, Sequence

from sqlalchemy import text
from sqlmodel import Session, select

from asas_audit.chain import VerifyReport, chain_payload, compute_hash, verify_rows
from asas_audit.models import AuditEvent

#: Taken before the tail read, released when the caller's transaction ends.
#: ``hashtextextended`` turns the per-tenant key into the bigint the advisory
#: lock functions take, so two tenants collide only on a hash collision rather
#: than by construction.
_LOCK = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('asas_audit_chain:' || :key, 0))"
)


#: SQLite's writer lock serialises INSERTs, not the tail READ before them —
#: without this mutex two threads read the same tail and fork the chain.
_SQLITE_APPEND_MUTEX = threading.Lock()


def _lock_chain(session: Session, org_id: str):
    """Return a context manager holding the chain closed for this append."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(_LOCK, {"key": str(org_id)})
        return nullcontext()  # the advisory lock releases with the transaction
    return _SQLITE_APPEND_MUTEX


def _tail(session: Session, org_id: str) -> Optional[AuditEvent]:
    return session.exec(
        select(AuditEvent)
        .where(AuditEvent.org_id == str(org_id))
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    ).first()


def append(
    session: Session,
    *,
    org_id: Any,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: Any,
    payload: Optional[dict[str, Any]] = None,
    occurred_at: Optional[datetime] = None,
) -> AuditEvent:
    """Record one business action on the caller's session, without committing.

    Call it inside the transaction that makes the change it describes. The caller
    commits both together; a rollback takes the entry with it, which is correct:
    an audit record for a change that never happened is worse than none.

    Returns the row, live on the caller's session, so its ``id`` and ``seq`` are
    available after the caller flushes.
    """
    org_key = str(org_id)

    # ── Normalise BEFORE any lock ("compute first, append last") ──
    #
    # occurred_at is normalised to UTC at the door, so the value hashed is the
    # value that round-trips on every engine (SQLite stores the wall clock and
    # drops the offset; a non-UTC aware input would otherwise verify as
    # tampered). A naive input is declared to be UTC — the only reading that
    # is stable across processes.
    when = occurred_at if occurred_at is not None else datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    when = when.astimezone(timezone.utc)
    # The payload is stored exactly as hashed: one round trip through the
    # canonical encoding up front, so (a) a non-JSON-safe value fails HERE, in
    # append, never as a TypeError at the host's commit after we reported
    # success, and (b) what verify() reads back re-encodes to the same bytes
    # regardless of the host's json_serializer or non-string dict keys.
    from asas_audit.chain import normalize_payload
    normalized_payload = normalize_payload(dict(payload or {}))

    lock = _lock_chain(session, org_key)
    with lock:
        # The flush is what makes the tail read see this transaction's earlier
        # appends: two entries in one unit of work must chain to each other,
        # not both to the row that preceded them.
        session.flush()
        tail = _tail(session, org_key)
        hash_prev = bytes(tail.hash_current) if tail is not None else None

        row = AuditEvent(
            org_id=org_key,
            actor=actor,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id),
            payload=normalized_payload,
            hash_prev=hash_prev,
            occurred_at=when,
        )
        row.hash_current = compute_hash(
            hash_prev,
            chain_payload(
                event_id=row.id,
                org_id=org_key,
                actor=row.actor,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                payload=row.payload,
                occurred_at=row.occurred_at,
            ),
        )
        session.add(row)
        # On SQLite the mutex must cover the INSERT itself, or a second thread
        # could still interleave between our tail read and our write.
        if session.get_bind().dialect.name != "postgresql":
            session.flush()
    return row


def verify(session: Session, org_id: Any) -> VerifyReport:
    """Re-derive one tenant's whole chain and report where, if anywhere, it stops
    adding up.

    Reads in ``seq`` order, which is the order the chain was written in. The
    arithmetic is :func:`asas_audit.chain.verify_rows`, which is pure, so the
    tamper cases are asserted without a database and this function only has to
    get the query right.
    """
    # Streamed, not materialised: an append-only table's whole history in one
    # .all() is O(n) memory per verify call; verify_rows only ever needs
    # sequential access, so a server-side cursor keeps this O(1).
    rows = session.exec(
        select(AuditEvent)
        .where(AuditEvent.org_id == str(org_id))
        .order_by(AuditEvent.seq)
        .execution_options(yield_per=500)
    )
    return verify_rows(str(org_id), rows)


def history(
    session: Session,
    *,
    org_id: Any,
    resource_type: Optional[str] = None,
    resource_id: Optional[Any] = None,
    actor: Optional[str] = None,
    action: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 100,
    offset: int = 0,
) -> Sequence[AuditEvent]:
    """The read: what happened, filtered, newest first.

    Newest first because that is what a reader wants and what an unfiltered page
    should show; :func:`verify` is the one caller that needs ascending order, and
    it asks for it explicitly rather than reusing this.

    ``resource_id`` without ``resource_type`` is refused: an id is only unique
    within a type, so the pair is the address of a record and half of it is a
    query that quietly matches the wrong rows.
    """
    if resource_id is not None and resource_type is None:
        raise ValueError(
            "resource_id needs resource_type: an id is unique only within a type, "
            "so filtering on the id alone can match another type's record."
        )
    statement = select(AuditEvent).where(AuditEvent.org_id == str(org_id))
    if resource_type is not None:
        statement = statement.where(AuditEvent.resource_type == resource_type)
    if resource_id is not None:
        statement = statement.where(AuditEvent.resource_id == str(resource_id))
    if actor is not None:
        statement = statement.where(AuditEvent.actor == actor)
    if action is not None:
        statement = statement.where(AuditEvent.action == action)
    if since is not None:
        statement = statement.where(AuditEvent.occurred_at >= since)
    if until is not None:
        statement = statement.where(AuditEvent.occurred_at <= until)
    statement = statement.order_by(AuditEvent.seq.desc()).limit(limit).offset(offset)
    return session.exec(statement).all()
