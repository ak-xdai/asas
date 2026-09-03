"""Channel adapters — the house provider pattern (cf. ``services/linkedin/``):
adding a channel = one adapter class + one ``register_adapter`` call, no caller
changes. Adapters are channel-specific formatters/senders; the payload they get is
channel-agnostic (title/body/link + taxonomy), per the epic's renderer rule.

WXL-222 ships the protocol + registry; the real email adapter lands with WXL-225.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

log = logging.getLogger(__name__)


class SkipDelivery(Exception):
    """Raised by an adapter to mark the delivery ``skipped`` (e.g. channel not
    configured for this deployment) — a graceful no-retry outcome, distinct from a
    failure (which retries up to the attempt cap)."""


@dataclass(frozen=True)
class DeliveryPayload:
    """Everything an adapter may need, without touching the ORM. Recipient contact
    details (email address, Slack id) are the adapter's own concern — resolved on
    its side of the seam so this package stays model-free.

    DR 0003 S-7 (0.16, breaking): ``kind`` → ``action`` and ``category`` →
    ``nature``, plus the new ``topic`` and ``data`` fields — the payload's one
    rename, made with the columns so the package keeps a single vocabulary.
    Adapters remain render-consumers: ``title``/``body`` arrive final (post-
    template once U-4 lands) and adapters never touch templates."""

    delivery_id: int
    notification_id: int
    channel: str
    recipient_user_id: int
    org_id: int
    action: Optional[str]
    topic: Optional[str]
    nature: str
    urgency: str
    reason: str
    title: str
    body: Optional[str]
    link: Optional[str]
    data: Optional[dict]
    created_at: datetime


class ChannelAdapter(Protocol):
    def send(self, payload: DeliveryPayload) -> None:
        """Deliver one notification on this channel. Raise ``SkipDelivery`` to mark
        the row skipped; any other exception marks it failed (retried by the sweep
        up to the attempt cap)."""
        ...


_ADAPTERS: dict[str, ChannelAdapter] = {}


def register_adapter(channel: str, adapter: Optional[ChannelAdapter]) -> None:
    """Register (or, with ``None``, remove) the adapter for a channel."""
    if adapter is None:
        _ADAPTERS.pop(channel, None)
    else:
        _ADAPTERS[channel] = adapter


def adapter_for(channel: str) -> Optional[ChannelAdapter]:
    return _ADAPTERS.get(channel)


class LoggingAdapter:
    """Stub adapter: logs the delivery and succeeds. Useful in tests and as a
    placeholder wiring until a real adapter (WXL-225 email) registers over it."""

    def __init__(self) -> None:
        self.sent: list[DeliveryPayload] = []

    def send(self, payload: DeliveryPayload) -> None:
        self.sent.append(payload)
        log.info(
            "notification %s → %s [%s/%s] %r",
            payload.notification_id,
            payload.channel,
            payload.nature,
            payload.urgency,
            payload.title,
        )
