"""Standalone package fixtures.

Engine: SQLite temp file by default; Postgres when ``TEST_DATABASE_URL`` is set
(the CI matrix runs both). This package owns no tables, so there is no chain to
run: the fixtures create a small two-tenant table of their own, which is also
what makes the isolation assertions non-vacuous.

**The isolation suites need more than Postgres, they need a role the policies
apply to.** A superuser ignores row-level security with ``FORCE`` set, so those
tests would pass on an unprotected database. :func:`requires_enforcement` is the
one guard they all go through, and it honours ``ASAS_REQUIRE_RLS``: skip locally
where a bypassing role is a deliberate convenience, fail in CI where it means the
checks have silently stopped running.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from sqlalchemy import create_engine, text

import asas_tenancy
from asas_tenancy import conformance

_TEST_URL = os.environ.get("TEST_DATABASE_URL")

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture()
def engine():
    if _TEST_URL:
        eng = create_engine(_TEST_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            # The test role has to own the new schema, or it cannot create the
            # fixture table in it. Granting rather than assuming, because the
            # role is deliberately not a superuser.
            conn.execute(text("GRANT ALL ON SCHEMA public TO current_user"))
    else:
        path = os.path.join(tempfile.gettempdir(), f"asas_tenancy_{uuid.uuid4().hex}.db")
        eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    yield eng
    eng.dispose()
    if not _TEST_URL and os.path.exists(path):
        # SQLite creates the file lazily on first connect, so a test that skips
        # before touching the database leaves nothing to remove.
        os.unlink(path)


@pytest.fixture()
def requires_enforcement(engine):
    """Skip (or fail, under ASAS_REQUIRE_RLS) unless the policies apply here."""
    reason = conformance.unenforced_reason(engine)
    if reason:
        message = f"tenant isolation cannot be verified: {reason}"
        if conformance.is_required():
            pytest.fail(f"{conformance.REQUIRE_ENV} is set but {message}")
        pytest.skip(message)
    return engine


@pytest.fixture()
def widgets(engine):
    """A protected two-tenant table, built the way a host's migration would.

    Deliberately created with raw DDL plus the package's own emitted policy SQL
    rather than through Alembic: the helpers are exercised by
    ``test_policy_helpers.py`` inside a real migration, and these fixtures want a
    table without that machinery.
    """
    is_pg = engine.dialect.name == "postgresql"
    tenant_type = "uuid" if is_pg else "text"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"CREATE TABLE widget (id integer PRIMARY KEY,"
                f" tenant_id {tenant_type} NOT NULL, label text NOT NULL)"
            )
        )
        if is_pg:
            conn.execute(text("ALTER TABLE widget ENABLE ROW LEVEL SECURITY"))
            conn.execute(text("ALTER TABLE widget FORCE ROW LEVEL SECURITY"))
            conn.execute(
                text(
                    "CREATE POLICY widget_tenant_isolation ON widget"
                    " USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
                    " WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)"
                )
            )
    return engine


@pytest.fixture()
def seeded(widgets):
    """One row per tenant. Inserted with FORCE lifted, which is what a migration
    seeding across tenants has to do, and is itself a small exercise of the rule
    :func:`asas_tenancy.without_force` exists for."""
    engine = widgets
    is_pg = engine.dialect.name == "postgresql"
    with engine.begin() as conn:
        if is_pg:
            conn.execute(text("ALTER TABLE widget NO FORCE ROW LEVEL SECURITY"))
        conn.execute(
            text("INSERT INTO widget (id, tenant_id, label) VALUES (1, :a, 'a-one')"),
            {"a": TENANT_A},
        )
        conn.execute(
            text("INSERT INTO widget (id, tenant_id, label) VALUES (2, :b, 'b-one')"),
            {"b": TENANT_B},
        )
        if is_pg:
            conn.execute(text("ALTER TABLE widget FORCE ROW LEVEL SECURITY"))
    return engine


@pytest.fixture(autouse=True)
def _clean_context():
    """The tenant context is a ContextVar, so it leaks between tests otherwise."""
    asas_tenancy.clear_tenant()
    yield
    asas_tenancy.clear_tenant()
