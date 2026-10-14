"""The read surface. Two routes, and deliberately no route that writes."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

import asas_audit
import asas_tenancy
from conftest import ORG_A, ORG_B, append_and_commit


def client(engine, org_id=ORG_A):
    def get_session():
        with Session(engine) as s:
            asas_tenancy.set_tenant_guc(s, org_id)
            yield s

    app = FastAPI()
    # Auth is composition-time: the host applies its own guards here. This
    # package has no way to know what an administrator is in the host's model,
    # and guessing would be worse than leaving it to the caller.
    app.include_router(asas_audit.build_router(get_session, tenant=lambda: org_id))
    return TestClient(app)


def test_events_are_listed_newest_first(migrated):
    for i in range(3):
        append_and_commit(migrated, ORG_A, action=f"step.{i}")
    body = client(migrated).get("/audit/events").json()
    assert [e["action"] for e in body] == ["step.2", "step.1", "step.0"]


def test_the_fingerprint_is_on_the_wire_as_hex(migrated):
    """Exposed so a reader can check the chain themselves rather than trust the
    verify endpoint's verdict."""
    append_and_commit(migrated, ORG_A)
    event = client(migrated).get("/audit/events").json()[0]
    assert len(event["hash_current"]) == 64
    int(event["hash_current"], 16)


def test_filters_are_honoured(migrated):
    append_and_commit(migrated, ORG_A, action="kept")
    append_and_commit(migrated, ORG_A, action="dropped")
    body = client(migrated).get("/audit/events", params={"action": "kept"}).json()
    assert [e["action"] for e in body] == ["kept"]


def test_verify_reports_intact(migrated):
    for i in range(3):
        append_and_commit(migrated, ORG_A, action=f"step.{i}")
    body = client(migrated).get("/audit/verify").json()
    assert body["is_intact"] is True
    assert body["events_checked"] == 3
    assert body["breaks"] == []


def test_the_tenant_is_not_a_parameter(migrated):
    """A caller cannot ask for another tenant's history by naming it: the tenant
    comes from the host's dependency, which is what the policy reads."""
    append_and_commit(migrated, ORG_A, action="a.thing")
    append_and_commit(migrated, ORG_B, action="b.thing")

    as_b = client(migrated, ORG_B).get("/audit/events").json()
    assert [e["action"] for e in as_b] == ["b.thing"]

    forced = client(migrated, ORG_B).get(
        "/audit/events", params={"org_id": ORG_A}
    ).json()
    assert [e["action"] for e in forced] == ["b.thing"], (
        "an org_id query parameter must be ignored, not honoured"
    )


def test_there_is_no_write_route(migrated):
    """Appending is something a host's service does inside its own transaction.
    An HTTP route for it would be a way to write history that did not happen."""
    routes = asas_audit.build_router(lambda: None, tenant=lambda: ORG_A).routes
    methods = {m for r in routes for m in getattr(r, "methods", set())}
    assert methods <= {"GET", "HEAD"}, f"unexpected write methods: {methods}"
