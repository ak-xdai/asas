"""The one table. Append-only, hash-chained, tenant-scoped.

**Identity is opaque strings, deliberately.** ``org_id`` and ``resource_id`` are
``str``, not ``int``, because a host that keys tenants by UUID and one that keys
them by integer must both be able to adopt this without a fork. The chain payload
stringifies every field anyway (see :mod:`asas_audit.chain`), so there is nothing
to gain from a narrower column and a whole class of adoption problem to lose. No
FKs to host tables either, for the same reason the rest of the family has none.

Two identity columns, and they answer different questions:

* ``seq`` is the **chain order**, a database-assigned integer. Verification walks
  rows in ``seq`` order, so it has to come from the database rather than from the
  application, and it has to be monotonic.
* ``id`` is the **event identity**, a UUID string, which is what a host puts in a
  URL or hands to a support ticket. It is also what goes into the hash, so the
  fingerprint does not depend on the sequence number the database happened to
  assign.

``occurred_at`` is when the business action happened, which is not always when the
row was written: a queued job records an event that occurred earlier. It is part
of the hash, so it cannot be corrected after the fact, which is the point.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime, BigInteger, Column, Index, Integer, LargeBinary
from sqlalchemy import JSON
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return str(uuid.uuid4())


class AuditEvent(SQLModel, table=True):
    """One business action, permanently.

    Rows are never updated and never deleted. That is enforced three ways, not
    one, because each layer fails in a different situation:

    1. no mutating code in this package,
    2. row-level security, so one tenant cannot reach another's history,
    3. **a database trigger that rejects UPDATE and DELETE for anyone**, which is
       the layer that survives a compromised application or a direct connection.

    The third is eight lines of plpgsql that nobody writes unprompted, and it is
    the difference between a log that is append-only by convention and one that is
    append-only in fact.
    """

    __tablename__ = "audit_event"
    __table_args__ = (
        # The tail read on every append: newest row for this tenant. DESC so the
        # index answers it without a sort.
        Index("ix_audit_event_org_seq", "org_id", "seq"),
        # The entity timeline: "everything that happened to this record".
        Index("ix_audit_event_resource", "org_id", "resource_type", "resource_id",
              "occurred_at"),
        Index("ix_audit_event_actor", "org_id", "actor", "occurred_at"),
        # The never-fork invariant, stated declaratively: a fork IS two rows
        # sharing (org_id, hash_prev). The locks make forks queue; this index
        # makes any fork that slips past them a loud IntegrityError for the
        # loser instead of a silent verification break weeks later. (NULL
        # genesis rows are exempt — both engines treat NULLs as distinct —
        # and the genesis race is covered by the locks.)
        Index("ix_audit_event_org_hash_prev", "org_id", "hash_prev", unique=True),
    )

    #: ``BigInteger`` because an audit log is the table least likely to be
    #: pruned, and ``with_variant`` because SQLite only auto-increments a plain
    #: ``INTEGER PRIMARY KEY``: a ``BIGINT`` one is an ordinary column there, so
    #: every insert would fail its NOT NULL. The dual-engine rule is what surfaces
    #: this; on Postgres alone the BigInteger would simply have worked.
    seq: Optional[int] = Field(
        default=None,
        sa_column=Column(
            BigInteger().with_variant(Integer, "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
    )
    id: str = Field(default_factory=_new_id, unique=True, index=True, max_length=64)
    # org_id is served by the leading column of every composite above — a
    # standalone index would be pure write amplification on the append path.
    org_id: str = Field(max_length=64)

    #: Who acted. A host's own principal string (a subject claim, a service name),
    #: not a foreign key: the actor may be a system, and an account that is later
    #: deleted must not take its history with it.
    actor: str = Field(max_length=200)
    #: What they did, as a stable code ("invoice.approved"), never a sentence.
    #: Prose cannot be translated and cannot be filtered on.
    action: str = Field(max_length=100)
    resource_type: str = Field(max_length=64)
    resource_id: str = Field(max_length=64)

    #: The details. Whatever the host needs to make the entry meaningful later,
    #: and it is part of the hash, so it cannot be revised afterwards.
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    #: Stored timezone-aware; append() normalises to UTC before hashing, so
    #: the value hashed is the value that round-trips on every engine. The
    #: sa_column matches the migration (timestamptz) — the two schema
    #: definitions must state one type or metadata-created databases drift.
    occurred_at: datetime = Field(
        default_factory=_utcnow,
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )

    #: The link to the previous entry for this tenant. NULL for the first one.
    hash_prev: Optional[bytes] = Field(
        default=None, sa_column=Column(LargeBinary(32), nullable=True)
    )
    hash_current: bytes = Field(sa_column=Column(LargeBinary(32), nullable=False))
