"""Teams meetings: the payloads that reach Graph and the errors that come back."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from asas_graph import (
    Attendee,
    GraphConfigError,
    GraphRequestError,
    Meeting,
    MeetingCancelError,
    MeetingCreationError,
    MeetingUpdateError,
    TeamsMeetings,
)

GST = timezone(timedelta(hours=4))  # Gulf Standard Time, no DST
START = datetime(2026, 9, 14, 10, 0, tzinfo=GST)  # 06:00 UTC
END = START + timedelta(hours=1)

CREATED = {
    "id": "AAMkAD-evt-1",
    "subject": "Cloud Architect interview",
    "webLink": "https://outlook.office365.com/owa/?itemid=AAMkAD-evt-1",
    "onlineMeeting": {"joinUrl": "https://teams.microsoft.com/l/meetup-join/abc"},
}


@pytest.fixture
def meetings(client):
    return TeamsMeetings(client, organizer="interviews@example.gov")


def test_organizer_is_required():
    with pytest.raises(GraphConfigError):
        TeamsMeetings(object(), organizer="")  # type: ignore[arg-type]


def test_create_books_an_online_event_in_utc_with_attendees(meetings, graph):
    graph.status, graph.body = 201, CREATED
    meeting = asyncio.run(
        meetings.create(
            "Cloud Architect interview", START, END,
            attendees=[Attendee("chen.wei@example.com", "Chen Wei"),
                       Attendee("panel@example.gov", required=False)],
            body_html="<p>Agenda</p>",
        )
    )

    call = graph.only
    assert call.method == "POST"
    assert call.url.endswith("/users/interviews@example.gov/events")
    assert call.json == {
        "subject": "Cloud Architect interview",
        "start": {"dateTime": "2026-09-14T06:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-14T07:00:00", "timeZone": "UTC"},
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "body": {"contentType": "HTML", "content": "<p>Agenda</p>"},
        "attendees": [
            {"emailAddress": {"address": "chen.wei@example.com", "name": "Chen Wei"}, "type": "required"},
            {"emailAddress": {"address": "panel@example.gov", "name": "panel@example.gov"}, "type": "optional"},
        ],
    }
    assert meeting == Meeting(
        id="AAMkAD-evt-1",
        join_url="https://teams.microsoft.com/l/meetup-join/abc",
        subject="Cloud Architect interview",
        start=datetime(2026, 9, 14, 6, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 14, 7, 0, tzinfo=timezone.utc),
        attendees=(Attendee("chen.wei@example.com", "Chen Wei"), Attendee("panel@example.gov", required=False)),
        web_link=CREATED["webLink"],
    )


def test_create_without_online_omits_the_teams_flags_and_tolerates_no_join_url(meetings, graph):
    graph.status, graph.body = 201, {"id": "evt-2", "subject": "Room booking"}
    meeting = asyncio.run(meetings.create("Room booking", START, END, online=False))
    payload = graph.only.json
    assert "isOnlineMeeting" not in payload and "onlineMeetingProvider" not in payload
    assert "attendees" not in payload and "body" not in payload
    assert meeting.join_url is None and meeting.id == "evt-2"


def test_create_online_without_a_join_url_returns_the_meeting_not_an_orphan(meetings, graph):
    """A 201 without onlineMeeting.joinUrl is Teams provisioning lag, not a
    failure: the invitations are already sent, so raising would orphan a real
    event and a retrying host would double-invite everyone. The typed Meeting
    comes back with join_url=None; the link appears on a later get()."""
    graph.status, graph.body = 201, {"id": "evt-3"}
    meeting = asyncio.run(meetings.create("x", START, END))
    assert meeting.id == "evt-3" and meeting.join_url is None


def test_create_maps_a_graph_failure_and_chains_the_cause(meetings, graph):
    graph.status = 403
    graph.body = {"error": {"code": "ErrorAccessDenied", "message": "no"}}
    with pytest.raises(MeetingCreationError) as exc:
        asyncio.run(meetings.create("x", START, END))
    assert exc.value.reason == "Graph returned 403"
    assert exc.value.detail == graph.body
    assert isinstance(exc.value.__cause__, GraphRequestError)
    assert exc.value.__cause__.code == "ErrorAccessDenied"


def test_naive_datetimes_are_refused(meetings, graph):
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(meetings.create("x", START.replace(tzinfo=None), END))
    with pytest.raises(ValueError, match="timezone-aware"):
        asyncio.run(meetings.reschedule("e", START, END.replace(tzinfo=None)))
    assert graph.calls == []


def test_end_must_be_after_start(meetings, graph):
    with pytest.raises(ValueError, match="after start"):
        asyncio.run(meetings.create("x", START, START))
    assert graph.calls == []


def test_attendee_needs_an_email_address():
    with pytest.raises(ValueError):
        Attendee("not-an-address")


def test_reschedule_patches_only_the_window_by_default(meetings, graph):
    graph.status = 204
    asyncio.run(meetings.reschedule("evt-1", START + timedelta(days=1), END + timedelta(days=1)))
    call = graph.only
    assert call.method == "PATCH"
    assert call.url.endswith("/users/interviews@example.gov/events/evt-1")
    assert call.json == {
        "start": {"dateTime": "2026-09-15T06:00:00", "timeZone": "UTC"},
        "end": {"dateTime": "2026-09-15T07:00:00", "timeZone": "UTC"},
    }


def test_reschedule_can_replace_attendees_and_subject(meetings, graph):
    graph.status = 204
    asyncio.run(meetings.reschedule("evt-1", START, END, attendees=[], subject="Moved"))
    assert graph.only.json["attendees"] == []  # an explicit empty list clears them
    assert graph.only.json["subject"] == "Moved"


def test_reschedule_maps_a_graph_failure(meetings, graph):
    graph.status, graph.body = 404, {"error": {"code": "ErrorItemNotFound", "message": "gone"}}
    with pytest.raises(MeetingUpdateError) as exc:
        asyncio.run(meetings.reschedule("evt-x", START, END))
    assert exc.value.reason == "Graph returned 404"
    assert exc.value.__cause__.is_not_found


def test_cancel_deletes_the_organizers_event(meetings, graph):
    graph.status = 204
    asyncio.run(meetings.cancel("evt-1"))
    call = graph.only
    assert call.method == "DELETE"
    assert call.url.endswith("/users/interviews@example.gov/events/evt-1")


def test_cancel_maps_a_graph_failure(meetings, graph):
    graph.status, graph.body = 500, {"error": {"code": "InternalServerError", "message": "boom"}}
    with pytest.raises(MeetingCancelError) as exc:
        asyncio.run(meetings.cancel("evt-1"))
    assert exc.value.reason == "Graph returned 500"
    assert isinstance(exc.value.__cause__, GraphRequestError)
