"""Emit semantics, routing, coalescing, suppression, the duplicate-safe
dispatcher, feed read-state, and the router factory. Engine behaviors ported
from Teamy's tests/test_notifications.py (WXL-222/TEAMY-475) with the
extraction (TEAMY-479)."""

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

import asas_notifications as notifications
from asas_notifications.models import (
    DeliveryStatus,
    Notification,
    NotificationDelivery,
)
from asas_notifications.service import MAX_ATTEMPTS

from conftest import emit


def test_unregistered_kind_fails_loud(session):
    with pytest.raises(LookupError):
        notifications.notify(session, [1], "never.registered", title="x")


def test_normal_kind_routes_email_low_stays_in_app(session, kind, ambient_kind):
    emit(session, kind, [1])
    emit(session, ambient_kind, [2])
    deliveries = session.exec(select(NotificationDelivery)).all()
    assert {d.channel for d in deliveries} == {"email"}
    notes = session.exec(select(Notification)).all()
    assert {n.user_id for n in notes} == {1, 2}
    emailed = {d.notification_id for d in deliveries}
    assert all(
        session.get(Notification, nid).user_id == 1 for nid in emailed
    )


def test_actor_exclusion_and_dedupe(session, kind):
    rows = emit(session, kind, [1, 1, 2], actor_user_id=2)
    assert [n.user_id for n in rows] == [1]


def test_recipient_filter_applies_with_record(session, kind):
    notifications.configure_recipient_filter(
        lambda s, ids, entity_type, record: [u for u in ids if u != 3]
    )
    rows = emit(
        session, kind, [1, 3], record=object(), entity_type="team", entity_id=9
    )
    assert [n.user_id for n in rows] == [1]


def test_coalesce_unread_updates_in_place(session, ambient_kind):
    first = emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=5, coalesce_unread=True, title="v1",
    )[0]
    again = emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=5, coalesce_unread=True, title="v2",
    )[0]
    assert again.id == first.id
    assert again.title == "v2"
    assert len(session.exec(select(Notification)).all()) == 1


def test_suppressed_context_no_ops_but_validates(session, kind):
    with notifications.suppressed():
        assert notifications.notify(session, [1], kind, title="x") == []
        with pytest.raises(LookupError):
            notifications.notify(session, [1], "never.registered", title="x")
    assert session.exec(select(Notification)).all() == []


def test_dispatch_sends_through_adapter(migrated, session, kind):
    adapter = notifications.LoggingAdapter()
    notifications.register_adapter("email", adapter)
    emit(session, kind, [1])
    handled = notifications.dispatch_pending(migrated)
    assert handled == 1
    assert len(adapter.sent) == 1
    d = session.exec(select(NotificationDelivery)).one()
    assert d.status == DeliveryStatus.sent


def test_dispatch_without_adapter_skips(migrated, session, kind):
    emit(session, kind, [1])
    notifications.dispatch_pending(migrated)
    d = session.exec(select(NotificationDelivery)).one()
    assert d.status == DeliveryStatus.skipped


def test_dispatch_failure_retries_then_dead_letters(migrated, session, kind):
    class Boom:
        def send(self, payload):
            raise RuntimeError("smtp down")

    notifications.register_adapter("email", Boom())
    emit(session, kind, [1])
    for _ in range(MAX_ATTEMPTS):
        notifications.dispatch_pending(migrated)
        session.expire_all()
    d = session.exec(select(NotificationDelivery)).one()
    assert d.status == DeliveryStatus.failed
    assert d.attempts == MAX_ATTEMPTS
    assert "smtp down" in d.last_error


def test_skip_delivery_marks_skipped(migrated, session, kind):
    class NotConfigured:
        def send(self, payload):
            raise notifications.SkipDelivery("email not configured")

    notifications.register_adapter("email", NotConfigured())
    emit(session, kind, [1])
    notifications.dispatch_pending(migrated)
    d = session.exec(select(NotificationDelivery)).one()
    assert d.status == DeliveryStatus.skipped


def test_stale_sending_claim_reclaims(migrated, session, kind):
    """A crashed pass's claim reclaims to pending after STALE_CLAIM_SECONDS and
    then dispatches normally (TEAMY-475)."""
    adapter = notifications.LoggingAdapter()
    notifications.register_adapter("email", adapter)
    emit(session, kind, [1])
    d = session.exec(select(NotificationDelivery)).one()
    d.status = DeliveryStatus.sending
    d.claimed_at = datetime.utcnow() - timedelta(minutes=10)
    session.add(d)
    session.commit()
    notifications.dispatch_pending(migrated)
    session.expire_all()
    d = session.exec(select(NotificationDelivery)).one()
    assert d.status == DeliveryStatus.sent
    assert len(adapter.sent) == 1


def test_fresh_sending_claim_is_left_alone(migrated, session, kind):
    """A live claim (another pass mid-send) must not be re-sent."""
    adapter = notifications.LoggingAdapter()
    notifications.register_adapter("email", adapter)
    emit(session, kind, [1])
    d = session.exec(select(NotificationDelivery)).one()
    d.status = DeliveryStatus.sending
    d.claimed_at = datetime.utcnow()
    session.add(d)
    session.commit()
    assert notifications.dispatch_pending(migrated) == 0
    assert adapter.sent == []


def test_feed_read_state(session, kind):
    emit(session, kind, [1])
    emit(session, kind, [1])
    from asas_notifications import service

    assert service.unread_count(session, 1) == 2
    first = session.exec(select(Notification)).first()
    service.mark_read(session, 1, first.id)
    assert service.unread_count(session, 1) == 1
    assert service.mark_all_read(session, 1) == 1
    assert service.unread_count(session, 1) == 0


def test_router_factory_serves_the_feed(migrated, session, kind):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    notifications.configure_context_resolver(lambda s: (1, 1))
    emit(session, kind, [1])

    app = FastAPI()

    def get_session():
        with Session(migrated) as s:
            yield s

    app.include_router(notifications.build_router(get_session))
    client = TestClient(app)
    feed = client.get("/me/notifications").json()
    assert feed["total"] == 1
    assert feed["unread_count"] == 1
    nid = feed["items"][0]["id"]
    assert client.post(f"/me/notifications/{nid}/read").status_code == 200
    assert client.get("/me/notifications").json()["unread_count"] == 0


def test_record_without_entity_type_fails_loud_not_unfiltered(session, kind):
    """A record whose entity_type is missing used to skip the visibility
    filter silently — every recipient got the notification, including ones
    the filter would have dropped. "Must never leak a private record" means
    the producer bug fails loud instead."""
    notifications.configure_recipient_filter(
        lambda s, ids, entity_type, record: [u for u in ids if u != 3]
    )
    with pytest.raises(ValueError, match="entity_type"):
        emit(session, kind, [1, 3], record=object(), entity_id=9)
    assert session.exec(select(Notification)).all() == []
    # without a configured filter the single-host default still applies:
    # record alone is fine, nothing to run
    notifications.configure_recipient_filter(None)
    rows = emit(session, kind, [1, 3], record=object(), entity_id=9)
    assert [n.user_id for n in rows] == [1, 3]


def test_entity_type_without_record_fails_loud_not_unfiltered(session, kind):
    """The symmetric hole, and the one that actually leaked in a real host.

    Naming an entity_type without its record skipped the visibility filter just
    as silently as the reverse. A host that configured a filter has declared
    some records restricted; a notification about one of those entities that
    cannot be filtered is precisely what the filter exists for, and by the time
    anyone could redact it the title is already in an inbox.
    """
    notifications.configure_recipient_filter(
        lambda s, ids, entity_type, record: [u for u in ids if u != 3]
    )
    with pytest.raises(ValueError, match="requires record="):
        emit(session, kind, [1, 3], entity_type="project", entity_id=9)
    assert session.exec(select(Notification)).all() == []


def test_a_subjectless_notification_needs_no_record(session, kind):
    """No entity_type at all is a system announcement — nothing to filter on.

    The guard must not make those impossible, or hosts will start passing a
    meaningless entity_type to satisfy it.
    """
    notifications.configure_recipient_filter(
        lambda s, ids, entity_type, record: [u for u in ids if u != 3]
    )
    rows = emit(session, kind, [1, 3])
    assert [n.user_id for n in rows] == [1, 3]


def test_the_guard_is_inert_without_a_configured_filter(session, kind):
    """A single-tenant host that never configured a filter is not doing anything
    wrong by omitting record — there is no filter to skip."""
    notifications.configure_recipient_filter(None)
    rows = emit(session, kind, [1, 3], entity_type="project", entity_id=9)
    assert [n.user_id for n in rows] == [1, 3]
