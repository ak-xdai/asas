"""Notification rows + the per-channel delivery outbox + deviation-only config
(WXL-222; DR 0003).

``notification`` IS the in-app delivery (insert = enqueue, read_at = seen);
``notification_delivery`` exists only for external channels — one row per
(notification, channel) the routing policy selects at emit time. Enums are
plain VARCHARs (``native_enum=False``, dual-engine rule).

DR 0003: a notification **references the application action that caused it**
(``action`` — a free string in the app's ``entity.verb`` grammar, declared
nowhere) and carries four classification axes. Management attaches to the axes,
never to individual actions; the config tables (``notification_topic``,
``notification_channel_policy``) store **deviations** from code defaults —
platform rows (``org_id NULL``) plus optional org override rows, DR 0001's
shared-with-overrides pattern.

``org_id`` follows the tenancy epic's mapping (WXL-218): notifications are
tenant data — org-scoped in the catalog, stamped from the producing request's
context.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from sqlalchemy import JSON, CheckConstraint, Column
from sqlalchemy import Enum as SAEnum
from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel


# ── enums (stored as plain VARCHAR — native_enum=False) ──────────────────────


class Nature(str, Enum):
    """What the notification demands of the recipient (drives UI + email subject).
    One of DR 0003's four axes; ``category`` until 0.15 — renamed because the
    word now belongs to nothing (the old kind catalog is gone)."""

    action = "action"
    info = "info"
    warning = "warning"


#: Deprecated alias for :class:`Nature` (0.15's name). One release, then gone.
Category = Nature


class Urgency(str, Enum):
    """How interruptive delivery may be (Apple interruption levels, coarsened)."""

    low = "low"
    normal = "normal"
    high = "high"


class Reason(str, Enum):
    """Why THIS recipient (GitHub participating-vs-watching, generalized)."""

    requested = "requested"
    participant = "participant"
    watching = "watching"


class DeliveryStatus(str, Enum):
    pending = "pending"
    sending = "sending"  # claimed by a dispatch pass (TEAMY-475); stale claims reclaim
    sent = "sent"
    failed = "failed"
    skipped = "skipped"


class Notification(SQLModel, table=True):
    __tablename__ = "notification"
    __table_args__ = (
        # The feed: WHERE user_id = ? [AND org_id = ?] AND archived_at IS (NOT)
        # NULL ORDER BY created_at DESC, id DESC. org_id sits second so the
        # org-scoped queries filter on the index while unscoped single-tenant
        # queries still use the user_id prefix. Subsumes the old single-column
        # user_id index (dropped in migration 0003). id trails as the ORDER BY
        # tiebreaker so tie-heavy batch emits still stream straight off the
        # index.
        Index(
            "ix_notification_user_org_archived_created",
            "user_id", "org_id", "archived_at", "created_at", "id",
        ),
        # The badge: WHERE user_id = ? [AND org_id = ?]
        # AND read_at IS NULL AND archived_at IS NULL.
        Index(
            "ix_notification_user_org_read_archived",
            "user_id", "org_id", "read_at", "archived_at",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(index=True)  # no host FK — plain int (extraction rule)
    user_id: int  # the recipient (no host FK); indexed via the composites above
    # The application action that caused this notification (DR 0003 S-2):
    # provenance + coalescing identity + the future actions-layer join key.
    # A *reference without declaration* — never validated against a catalog.
    # NULL for ad hoc emits (a one-off "import finished"), which therefore
    # never coalesce.
    action: Optional[str] = Field(default=None, index=True)
    # The four axes (DR 0003 S-1). `topic` is the management/preference
    # grouping, validated against notification_topic at emit (the one
    # reference that policy and preferences depend on). Nullable only for
    # rows that predate migration 0004 — new emits always carry one.
    topic: Optional[str] = Field(default=None, index=True)
    nature: Nature = Field(
        sa_column=Column(SAEnum(Nature, native_enum=False), nullable=False)
    )
    urgency: Urgency = Field(
        sa_column=Column(SAEnum(Urgency, native_enum=False), nullable=False)
    )
    reason: Reason = Field(
        sa_column=Column(SAEnum(Reason, native_enum=False), nullable=False)
    )
    # Generic subject reference (never an FK — the package is entity-agnostic).
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    title: str
    body: Optional[str] = None
    link: Optional[str] = None  # frontend deep link, e.g. "/teams/42"
    # DR 0003 S-4: the template reference + structured payload stored alongside
    # the rendered text, so a future localization DR can move the feed to
    # read-time rendering without a migration. `data` is a denormalized
    # presentation payload — the structured sibling of `title`, same
    # PII/retention posture as the row. Rendering itself lands with U-4.
    template: Optional[str] = None
    data: Optional[dict[str, Any]] = Field(
        default=None, sa_column=Column(JSON, nullable=True)
    )
    read_at: Optional[datetime] = None
    # Dealt with — out of the recipient's inbox. A separate axis from `read_at`:
    # reading is seeing, archiving is finishing, and a host may well want an
    # action notification to survive being read (Teamy TEAMY-692 does).
    archived_at: Optional[datetime] = None
    # Reserved for auto-clearing `action` notifications when the underlying task
    # completes. Deliberately unused: Teamy weighed it for TEAMY-692 and chose
    # the archive gesture instead, so nothing writes this column today.
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationDelivery(SQLModel, table=True):
    __tablename__ = "notification_delivery"
    __table_args__ = (
        # The dispatcher's scan (status = pending) and the stale-claim sweep
        # (status = sending AND claimed_at < cutoff). Prefix covers the old
        # single-column status index (dropped in migration 0003).
        Index("ix_notification_delivery_status_claimed", "status", "claimed_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    notification_id: int = Field(foreign_key="notification.id", index=True)
    channel: str = Field(index=True)  # "email" now; "slack"/"teams" later
    status: DeliveryStatus = Field(
        default=DeliveryStatus.pending,
        sa_column=Column(SAEnum(DeliveryStatus, native_enum=False), nullable=False),
    )
    attempts: int = Field(default=0)
    # When the row was CAS-claimed (status → sending); a claim older than
    # ``service.STALE_CLAIM_SECONDS`` belongs to a crashed pass and reclaims.
    claimed_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None
    last_error: Optional[str] = None


# ── deviation-only configuration (DR 0003 S-3) ───────────────────────────────


class NotificationTopic(SQLModel, table=True):
    """A preference/management grouping (~5–8 per app; Android-channel-shaped).

    Platform rows have ``org_id NULL``; an org override row (same ``key``,
    org set) beats the platform row. Migration 0004 seeds one platform row,
    ``general`` — the designated topic for ad hoc emits and the legacy
    ``register_kind`` shim."""

    __tablename__ = "notification_topic"
    __table_args__ = (
        UniqueConstraint("org_id", "key", name="uq_notification_topic_org_key"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: Optional[int] = Field(default=None, index=True)  # NULL = platform row
    key: str = Field(index=True)  # e.g. "approvals", "activity"
    name: str
    description: Optional[str] = None
    # Locked topics (e.g. security) never appear on the preference screen.
    # Enforced by U-3's preference API, carried here so admin UI can read it.
    user_configurable: bool = True
    sort_order: int = 0
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class NotificationChannelPolicy(SQLModel, table=True):
    """One routing deviation: enables/disables a channel for a topic OR an axis
    condition (urgency and/or nature) — exactly one of the two, CHECK-enforced.

    Resolution precedence (DR 0003 S-5, per channel, most specific wins):
    topic row → axis row → the built-in code fallback (`low` → in-app only,
    else in-app + email). Org override rows beat platform rows within a tier.
    ``mandatory`` marks channels user preferences may not disable (U-3)."""

    __tablename__ = "notification_channel_policy"
    __table_args__ = (
        CheckConstraint(
            "(topic IS NOT NULL AND urgency IS NULL AND nature IS NULL) OR "
            "(topic IS NULL AND (urgency IS NOT NULL OR nature IS NOT NULL))",
            name="ck_notification_channel_policy_one_condition",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: Optional[int] = Field(default=None, index=True)  # NULL = platform row
    topic: Optional[str] = Field(default=None, index=True)
    urgency: Optional[Urgency] = Field(
        default=None,
        sa_column=Column(SAEnum(Urgency, native_enum=False), nullable=True),
    )
    nature: Optional[Nature] = Field(
        default=None,
        sa_column=Column(SAEnum(Nature, native_enum=False), nullable=True),
    )
    channel: str  # "in_app", "email", "teams", …
    enabled: bool = True
    mandatory: bool = False  # exempt from user preference narrowing (U-3)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
