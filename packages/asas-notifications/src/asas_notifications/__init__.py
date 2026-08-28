"""Asas notifications — generic notification engine with a per-channel outbox.

Extracted from Teamy (epic WXL-209/WXL-222; extraction epic TEAMY-466, design
record 0017). The package never imports host models. Producers register event
kinds and emit through :func:`notify` inside their own transaction (the insert
IS the enqueue); the in-app feed is the ``notification`` row itself; every other
channel goes through the ``notification_delivery`` outbox and a registered
channel adapter. Dispatch is duplicate-safe under concurrent passes (per-row CAS
claims with stale-claim reclaim) and at-least-once overall.

Host contract (table-owning + router variant):

- :func:`migrate` — package Alembic chain (adopt-or-create).
- :func:`build_router` — the ``/me/notifications`` feed API; the host passes its
  session dependency and applies auth at include time.
- :func:`configure_context_resolver` / :func:`configure_recipient_filter` — the
  host supplies "who is the current (user, org)" and "which recipients may see
  this record" (a notification must never leak a private record).
- :func:`register_kind` / :func:`register_adapter` — the kind catalog and
  channel adapters (email, chat, …) are the host's.
- :func:`dispatch_pending` — one outbox pass; the host owns the cadences
  (after-commit hook, boot sweep, periodic job).
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
    Category,
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    Reason,
    Urgency,
)
from .router import build_router
from .service import (
    configure_context_resolver,
    configure_recipient_filter,
    dispatch_pending,
    notify,
    register_kind,
    suppressed,
)

__version__ = "0.15.0"

__all__ = [
    "Category",
    "ChannelAdapter",
    "DeliveryPayload",
    "DeliveryStatus",
    "LoggingAdapter",
    "Notification",
    "NotificationDelivery",
    "Reason",
    "SkipDelivery",
    "Urgency",
    "build_router",
    "configure_context_resolver",
    "configure_recipient_filter",
    "dispatch_pending",
    "migrate",
    "notify",
    "register_adapter",
    "register_kind",
    "service",
    "suppressed",
    "__version__",
]
