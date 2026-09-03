"""Asas notifications — generic notification engine with a per-channel outbox.

Extracted from Teamy (epic WXL-209/WXL-222; extraction epic TEAMY-466, design
record 0017; reshaped by DR 0003). The package never imports host models.
Producers emit through :func:`notify` inside their own transaction (the insert
IS the enqueue), passing the application ``action`` that caused the event — a
reference, declared nowhere — plus four classification axes
(``topic``/``nature``/``urgency``/``reason``); routing attaches to the axes via
the policy tables, never to individual actions. The in-app feed is the
``notification`` row itself; every other channel goes through the
``notification_delivery`` outbox and a registered channel adapter. Dispatch is
duplicate-safe under concurrent passes (per-row CAS claims with stale-claim
reclaim) and at-least-once overall.

Host contract (table-owning + router variant):

- :func:`migrate` — package Alembic chain (adopt-or-create).
- :func:`build_router` — the ``/me/notifications`` feed API; the host passes its
  session dependency and applies auth at include time.
- :func:`configure_context_resolver` / :func:`configure_recipient_filter` — the
  host supplies "who is the current (user, org)" and "which recipients may see
  this record" (a notification must never leak a private record).
- seeded :class:`NotificationTopic` rows + :func:`register_adapter` — the topic
  vocabulary and channel adapters (email, chat, …) are the host's.
- :func:`dispatch_pending` — one outbox pass; the host owns the cadences
  (after-commit hook, boot sweep, periodic job).
- :func:`register_kind` — **deprecated shim** (one release): maps a registered
  kind's defaults onto the axes for legacy ``notify(kind=...)`` emits.
"""

from . import service
from .channels import (
    ChannelAdapter,
    DeliveryPayload,
    LoggingAdapter,
    SkipDelivery,
    register_adapter,
)
from .migrate import migrate
from .models import (
    Category,  # deprecated alias for Nature (one release)
    DeliveryStatus,
    Nature,
    Notification,
    NotificationChannelPolicy,
    NotificationDelivery,
    NotificationTopic,
    Reason,
    Urgency,
)
from .router import build_router
from .service import (
    DEFAULT_TOPIC,
    IN_APP,
    config_cache_clear,
    configure_context_resolver,
    configure_recipient_filter,
    dispatch_pending,
    notify,
    register_kind,  # deprecated shim (DR 0003 I-3; one release)
    resolve_channels,
    suppressed,
)

__version__ = "0.16.0"

__all__ = [
    "Category",
    "ChannelAdapter",
    "DEFAULT_TOPIC",
    "DeliveryPayload",
    "DeliveryStatus",
    "IN_APP",
    "LoggingAdapter",
    "Nature",
    "Notification",
    "NotificationChannelPolicy",
    "NotificationDelivery",
    "NotificationTopic",
    "Reason",
    "SkipDelivery",
    "Urgency",
    "build_router",
    "config_cache_clear",
    "configure_context_resolver",
    "configure_recipient_filter",
    "dispatch_pending",
    "migrate",
    "notify",
    "register_adapter",
    "register_kind",
    "resolve_channels",
    "service",
    "suppressed",
    "__version__",
]
