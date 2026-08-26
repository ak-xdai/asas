"""SQL pagination + org defense-in-depth (evolution plan §15.1/§15.2).

The feed pages in SQL now; `total` must still be the full filtered count while
`items` is one page. And when the context resolver supplies an org, every
recipient-facing query constrains org_id in addition to user_id — a cross-org id
probe answers exactly like a missing row.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

import asas_notifications as notifications
from asas_notifications import service
from asas_notifications.models import Notification

from conftest import emit


def _client(migrated):
    app = FastAPI()

    def get_session():
        with Session(migrated) as s:
            yield s

    app.include_router(notifications.build_router(get_session))
    return TestClient(app)


def test_feed_pages_in_sql(migrated, session, kind):
    notifications.configure_context_resolver(lambda s: (1, 1))
    for i in range(5):
        emit(session, kind, [1], title=f"n{i}")
    client = _client(migrated)

    page1 = client.get("/me/notifications?page_size=2").json()
    assert page1["total"] == 5  # the filtered count, not the page length
    assert len(page1["items"]) == 2
    page3 = client.get("/me/notifications?page=3&page_size=2").json()
    assert len(page3["items"]) == 1
    beyond = client.get("/me/notifications?page=4&page_size=2").json()
    assert beyond["items"] == [] and beyond["total"] == 5

    # newest-first across pages, no overlap, nothing skipped
    ids = [n["id"] for p in (page1, client.get("/me/notifications?page=2&page_size=2").json(), page3) for n in p["items"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 5


def test_feed_is_org_scoped(migrated, session, kind):
    # rows created (and stamped) in org 1
    notifications.configure_context_resolver(lambda s: (0, 1))
    row = emit(session, kind, [1])[0]

    # the same user id asking from org 2 sees nothing — and cannot probe by id
    notifications.configure_context_resolver(lambda s: (1, 2))
    client = _client(migrated)
    feed = client.get("/me/notifications").json()
    assert feed["total"] == 0 and feed["unread_count"] == 0
    assert client.post(f"/me/notifications/{row.id}/read").status_code == 404
    assert client.post(f"/me/notifications/{row.id}/archive").status_code == 404
    assert client.post("/me/notifications/read-all").json()["updated"] == 0
    assert client.post("/me/notifications/archive-read").json()["updated"] == 0

    # back in org 1 the row is untouched and fully reachable
    notifications.configure_context_resolver(lambda s: (1, 1))
    client = _client(migrated)
    feed = client.get("/me/notifications").json()
    assert feed["total"] == 1 and feed["unread_count"] == 1
    assert client.post(f"/me/notifications/{row.id}/read").status_code == 200


def test_service_calls_without_context_are_unscoped(session, kind):
    """No resolver (or outside a request) keeps the single-tenant behavior:
    user_id alone is the scope."""
    emit(session, kind, [1])
    service.configure_context_resolver(None)
    assert service.unread_count(session, 1) == 1
    assert service.mark_all_read(session, 1) == 1


def test_coalesce_never_crosses_orgs(session, ambient_kind):
    """An unread row for the same (user, kind, entity) in another org must not
    become the coalescing target — entity ids carry no cross-org meaning."""
    service.configure_context_resolver(lambda s: (0, 1))
    first = emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=5, coalesce_unread=True, title="org1",
    )[0]
    service.configure_context_resolver(lambda s: (0, 2))
    second = emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=5, coalesce_unread=True, title="org2",
    )[0]
    assert second.id != first.id
    assert second.org_id == 2 and first.org_id == 1
    assert first.title == "org1"  # untouched


def test_coalesce_requires_org_context(session, ambient_kind):
    """Without an org context the lookup cannot tell orgs apart, so an org-less
    emit (a background job in a host that stamps org_id via its own ORM
    listener) inserts a fresh row instead of merging into one whose org it
    cannot verify."""
    from sqlalchemy import event

    service.configure_context_resolver(lambda s: (0, 1))
    first = emit(
        session, ambient_kind, [1],
        entity_type="work_item", entity_id=9, coalesce_unread=True, title="org1",
    )[0]

    # background emit: no request context; org stamped host-side at flush,
    # the listener pattern the insert-path comment explicitly endorses
    service.configure_context_resolver(lambda s: None)

    @event.listens_for(session, "before_flush")
    def _stamp(sess, ctx, instances):
        for obj in sess.new:
            if isinstance(obj, Notification) and obj.org_id is None:
                obj.org_id = 2

    try:
        second = emit(
            session, ambient_kind, [1],
            entity_type="work_item", entity_id=9, coalesce_unread=True, title="org2",
        )[0]
    finally:
        event.remove(session, "before_flush", _stamp)

    assert second.id != first.id  # inserted, not merged
    assert second.org_id == 2
    refreshed = session.get(Notification, first.id)
    assert refreshed.title == "org1" and refreshed.org_id == 1  # untouched
