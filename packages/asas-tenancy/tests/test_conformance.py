"""The kit that says whether anything above is in force."""

from __future__ import annotations

import pytest
from sqlalchemy import text

import asas_tenancy
from asas_tenancy import conformance


def test_status_reports_the_role_and_its_powers(engine):
    status = conformance.enforcement_status(engine)
    if engine.dialect.name == "postgresql":
        assert status.role
        assert status.supported
        # enforced is the conjunction: supported, not superuser, no BYPASSRLS.
        assert status.enforced == (not status.is_superuser and not status.bypasses_rls)
    else:
        assert not status.supported
        assert not status.enforced


def test_sqlite_says_it_cannot_enforce_rather_than_claiming_it_does(engine):
    """The quiet failure this replaces: a suite that runs only on SQLite is
    green and proves nothing about isolation."""
    if engine.dialect.name == "postgresql":
        pytest.skip("this is the non-Postgres path")
    reason = conformance.unenforced_reason(engine)
    assert reason and "no row-level security" in reason


def test_a_bypassing_role_is_reported_with_its_remedy(engine):
    """Named roles and named fixes: this message is read in a CI failure by
    somebody who does not already know what BYPASSRLS is."""
    if engine.dialect.name != "postgresql":
        pytest.skip("Postgres roles only")
    reason = conformance.unenforced_reason(engine)
    status = conformance.enforcement_status(engine)
    if status.enforced:
        assert reason is None
    else:
        assert status.role in reason
        assert "NOBYPASSRLS" in reason or "non-superuser" in reason


def test_require_env_is_read_from_the_environment(monkeypatch):
    """``ASAS_REQUIRE_RLS=1`` in CI is what turns "cannot check" from a skip into
    a failure, so the checks can never silently stop running. Same shape
    ``asas-storage`` uses to stop its Azure leg vanishing."""
    monkeypatch.delenv(conformance.REQUIRE_ENV, raising=False)
    assert conformance.is_required() is False
    monkeypatch.setenv(conformance.REQUIRE_ENV, "1")
    assert conformance.is_required() is True
    monkeypatch.setenv(conformance.REQUIRE_ENV, "true")
    assert conformance.is_required() is True
    monkeypatch.setenv(conformance.REQUIRE_ENV, "0")
    assert conformance.is_required() is False


def test_report_names_exactly_what_is_missing(engine):
    """The sweep's value is the message. "not protected" sends somebody reading
    catalogue tables; naming the missing half does not."""
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE bare (id integer PRIMARY KEY, tenant_id uuid)"))
        conn.execute(text("CREATE TABLE half (id integer PRIMARY KEY, tenant_id uuid)"))
        conn.execute(text("ALTER TABLE half ENABLE ROW LEVEL SECURITY"))

    bare, half, absent = asas_tenancy.protection_report(engine, ["bare", "half", "nope"])

    assert "RLS not enabled" in bare.complaint()
    assert "NOT FORCED" in half.complaint()
    assert "no policy" in half.complaint()
    assert "no such table" in absent.complaint()


def test_the_sweep_fails_for_an_unprotected_table(engine, requires_enforcement):
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE bare (id integer PRIMARY KEY, tenant_id uuid)"))
    with pytest.raises(AssertionError, match="not protected"):
        asas_tenancy.assert_tables_protected(engine, ["bare"])


def test_the_sweep_passes_for_a_protected_one(widgets, requires_enforcement):
    asas_tenancy.assert_tables_protected(widgets, ["widget"])


def test_the_sweep_refuses_to_run_unenforced(widgets):
    """It must not report success on a connection the policies do not apply to,
    which is the whole reason this module exists."""
    if conformance.unenforced_reason(widgets) is None:
        pytest.skip("this connection enforces; the refusal path is the point here")
    with pytest.raises(RuntimeError, match="not enforced"):
        asas_tenancy.assert_tables_protected(widgets, ["widget"])
