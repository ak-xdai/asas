"""Teams meetings on an organiser's calendar: create, reschedule, cancel.

The meeting is a calendar **event** with ``isOnlineMeeting=true``
(``POST /users/{organizer}/events``), not an ``onlineMeetings`` resource,
for two reasons that matter to a host:

- It needs only the ``Calendars.ReadWrite`` application permission, which a
  tenant administrator will grant; ``OnlineMeetings.ReadWrite.All`` needs an
  application access policy on top.
- Exchange sends the invitations, updates and cancellations itself, off the
  calendar write. The library sends no mail and needs no ``Mail.Send``.

Datetimes must be timezone-aware. They are converted to UTC and sent with
``timeZone: "UTC"`` — Graph then renders each attendee's copy in *their*
calendar's zone. A naive datetime is refused rather than guessed at, because
"the server's local time" has booked a lot of interviews an hour off.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .client import GraphClient
from .errors import (
    GraphConfigError,
    GraphRequestError,
    MeetingCancelError,
    MeetingCreationError,
    MeetingUpdateError,
)

_GRAPH_DATETIME = "%Y-%m-%dT%H:%M:%S"
_TEAMS_PROVIDER = "teamsForBusiness"


@dataclass(frozen=True)
class Attendee:
    """Someone invited to the meeting. ``name`` falls back to the address."""

    email: str
    name: str | None = None
    required: bool = True

    def __post_init__(self) -> None:
        if not self.email or "@" not in self.email:
            raise ValueError(f"Attendee.email must be an email address, got {self.email!r}")


@dataclass(frozen=True)
class Meeting:
    """What a host stores after creating a meeting.

    ``id`` is the calendar **event** id — the handle for reschedule/cancel.
    ``join_url`` is empty for an event created with ``online=False``.
    ``start``/``end`` are UTC.
    """

    id: str
    join_url: str | None  # None until Teams finishes provisioning; re-get() to refresh
    subject: str
    start: datetime
    end: datetime
    attendees: tuple[Attendee, ...] = ()
    web_link: str | None = None


class TeamsMeetings:
    """Book, move and cancel Teams meetings in one organiser's calendar.

    ``organizer`` is the user id or UPN whose calendar owns the events —
    typically a shared mailbox such as ``interviews@example.gov``. It must be a
    real mailbox the application permission covers.
    """

    def __init__(self, client: GraphClient, organizer: str) -> None:
        if not organizer:
            raise GraphConfigError("TeamsMeetings needs an organizer (user id or UPN)")
        self._client = client
        self._organizer = organizer

    @property
    def organizer(self) -> str:
        return self._organizer

    def _events_path(self, event_id: str | None = None) -> str:
        base = f"/users/{self._organizer}/events"
        return f"{base}/{event_id}" if event_id else base

    async def create(
        self,
        subject: str,
        start: datetime,
        end: datetime,
        *,
        attendees: Iterable[Attendee] = (),
        body_html: str | None = None,
        online: bool = True,
    ) -> Meeting:
        """Create the event. ``online=True`` mints a Teams meeting and Graph
        appends its "Join" block to whatever ``body_html`` you supply."""
        start_utc, end_utc = _validated_window(start, end)
        invitees = tuple(attendees)
        payload: dict[str, Any] = {
            "subject": subject,
            "start": _graph_datetime(start_utc),
            "end": _graph_datetime(end_utc),
        }
        if online:
            payload["isOnlineMeeting"] = True
            payload["onlineMeetingProvider"] = _TEAMS_PROVIDER
        if body_html:
            payload["body"] = {"contentType": "HTML", "content": body_html}
        if invitees:
            payload["attendees"] = _attendees_payload(invitees)

        try:
            body = await self._client.post(self._events_path(), payload)
        except GraphRequestError as exc:
            raise MeetingCreationError(f"Graph returned {exc.status}", detail=exc.detail) from exc

        body = body or {}
        join_url = (body.get("onlineMeeting") or {}).get("joinUrl")
        if not body.get("id"):
            raise MeetingCreationError(
                "Graph event response missing id", detail=body
            )
        # A 201 without onlineMeeting.joinUrl is Teams provisioning lag, not a
        # failure: the invitations are ALREADY SENT, so raising here orphans a
        # real event (and a retrying host double-invites everyone). Return the
        # typed Meeting with join_url=None; the link appears on a later get().
        return Meeting(
            id=body["id"],
            join_url=join_url or None,
            subject=body.get("subject") or subject,
            start=start_utc,
            end=end_utc,
            attendees=invitees,
            web_link=body.get("webLink"),
        )

    async def reschedule(
        self,
        event_id: str,
        start: datetime,
        end: datetime,
        *,
        attendees: Iterable[Attendee] | None = None,
        subject: str | None = None,
    ) -> None:
        """Move the event (and optionally replace its attendees/subject).

        Every PATCH is an update as far as Exchange is concerned: attendees
        are notified and their responses reset. Callers should not PATCH
        when nothing changed — compare first.
        """
        start_utc, end_utc = _validated_window(start, end)
        payload: dict[str, Any] = {
            "start": _graph_datetime(start_utc),
            "end": _graph_datetime(end_utc),
        }
        if subject is not None:
            payload["subject"] = subject
        if attendees is not None:
            payload["attendees"] = _attendees_payload(tuple(attendees))

        try:
            await self._client.patch(self._events_path(event_id), payload)
        except GraphRequestError as exc:
            raise MeetingUpdateError(f"Graph returned {exc.status}", detail=exc.detail) from exc

    async def cancel(self, event_id: str) -> None:
        """Delete the organiser's event; Exchange sends the cancellation."""
        try:
            await self._client.delete(self._events_path(event_id))
        except GraphRequestError as exc:
            raise MeetingCancelError(f"Graph returned {exc.status}", detail=exc.detail) from exc


def _validated_window(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start_utc, end_utc = _to_utc(start, "start"), _to_utc(end, "end")
    if end_utc <= start_utc:
        raise ValueError(f"meeting end ({end_utc.isoformat()}) must be after start ({start_utc.isoformat()})")
    return start_utc, end_utc


def _to_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must be a timezone-aware datetime; a naive one would be sent "
            f"as UTC whatever the caller meant"
        )
    return value.astimezone(timezone.utc)


def _graph_datetime(value_utc: datetime) -> dict[str, str]:
    return {"dateTime": value_utc.strftime(_GRAPH_DATETIME), "timeZone": "UTC"}


def _attendees_payload(attendees: Sequence[Attendee]) -> list[dict[str, Any]]:
    return [
        {
            "emailAddress": {"address": a.email, "name": a.name or a.email},
            "type": "required" if a.required else "optional",
        }
        for a in attendees
    ]
