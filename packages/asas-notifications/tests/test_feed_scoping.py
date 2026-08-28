"""SQL pagination + org defense-in-depth on the read paths (PR #20, re-landed).

The feed pages in SQL now; `total` must still be the full filtered count while
`items` is one page. And when the context resolver supplies an org, every
recipient-facing query constrains org_id in addition to user_id — a cross-org id
probe answers exactly like a missing row.

The emit side's org rules (org-less emits fail loud, coalescing never crosses
orgs) landed in 0.13.0 and are pinned by ``test_engine.py``; this file covers
the read side.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

import asas_notifications as notifications
from asas_notifications import service

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
    page2 = client.get("/me/notifications?page=2&page_size=2").json()
    ids = [n["id"] for p in (page1, page2, page3) for n in p["items"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 5


def test_feed_total_follows_filters(migrated, session, kind, ambient_kind):
    """`total` tracks the composed filters while `unread_count` ignores them —
    the response contract the router documents, now produced by two different
    SQL statements instead of one Python slice."""
    notifications.configure_context_resolver(lambda s: (0, 1))
    read_row = emit(session, kind, [1], title="seen")[0]
    emit(session, ambient_kind, [1], title="unseen")
    service.mark_read(session, 1, read_row.id)

    notifications.configure_context_resolver(lambda s: (1, 1))
    client = _client(migrated)
    unread = client.get("/me/notifications?unread_only=true").json()
    assert unread["total"] == 1 and unread["unread_count"] == 1
    everything = client.get("/me/notifications").json()
    assert everything["total"] == 2 and everything["unread_count"] == 1


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
    assert client.post(f"/me/notifications/{row.id}/unarchive").status_code == 404
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
    rows, total = service.list_feed(session, 1)
    assert total == 1 and [n.user_id for n in rows] == [1]
    assert service.mark_all_read(session, 1) == 1
    assert service.archive_read(session, 1) == 1
