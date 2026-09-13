"""Standalone package fixtures.

Engine: SQLite temp file by default; Postgres when ``TEST_DATABASE_URL`` is set
(the CI matrix runs both). Schema always comes from the package's own migration
chain via ``migrate(engine)``, so the chain is exercised on every run.

Two guards, because two of this package's three guarantees can only be checked on
Postgres and would otherwise pass for the wrong reason:

* ``requires_postgres`` for the trigger and the advisory lock, which SQLite has
  neither of;
* ``requires_enforcement`` for the isolation assertions, because a superuser
  ignores row-level security with FORCE set. It honours ``ASAS_REQUIRE_RLS``:
  skip locally where a bypassing role is a deliberate convenience, fail in CI
  where it means the coverage silently stopped running.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

import asas_audit
import asas_tenancy
from asas_tenancy import conformance

_TEST_URL = os.environ.get("TEST_DATABASE_URL")

ORG_A = "org-a"
ORG_B = "org-b"


@pytest.fixture()
def engine():
    if _TEST_URL:
        eng = create_engine(_TEST_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
            conn.execute(text("GRANT ALL ON SCHEMA public TO current_user"))
    else:
        path = os.path.join(tempfile.gettempdir(), f"asas_audit_{uuid.uuid4().hex}.db")
        eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    yield eng
    eng.dispose()
    if not _TEST_URL and os.path.exists(path):
        os.unlink(path)


@pytest.fixture()
def migrated(engine):
    asas_audit.migrate(engine)
    return engine


@pytest.fixture()
def session(migrated):
    """A session with the tenant pinned to ORG_A, which is what a request has.

    ``expire_on_commit=False`` is the host guidance, not test convenience: with
    the default, reading any attribute of a row after ``commit()`` triggers a
    refresh, that refresh runs in a NEW transaction which has no tenant pinned
    (the pin is transaction-local, deliberately), the policy filters the row out,
    and SQLAlchemy reports ``ObjectDeletedError`` on a row that is sitting right
    there. ``test_reading_after_commit_needs_expire_on_commit_false`` pins that
    behaviour so the next reader meets it as a test rather than as a mystery.
    """
    with Session(migrated, expire_on_commit=False) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        yield s


@pytest.fixture()
def requires_postgres(migrated):
    if migrated.dialect.name != "postgresql":
        pytest.skip("needs Postgres: SQLite has neither triggers of this shape "
                    "nor advisory locks, and serialises writers anyway")
    return migrated


@pytest.fixture()
def requires_enforcement(migrated):
    reason = conformance.unenforced_reason(migrated)
    if reason:
        message = f"tenant isolation cannot be verified: {reason}"
        if conformance.is_required():
            pytest.fail(f"{conformance.REQUIRE_ENV} is set but {message}")
        pytest.skip(message)
    return migrated


@pytest.fixture(autouse=True)
def _clean_context():
    asas_tenancy.clear_tenant()
    yield
    asas_tenancy.clear_tenant()


def append_and_commit(engine, org_id, action="thing.done", **kwargs):
    """Write one entry the way a host would: pin, append, commit, in one go.

    ``expire_on_commit=False`` for the reason the ``session`` fixture gives.
    """
    with Session(engine, expire_on_commit=False) as s:
        asas_tenancy.set_tenant_guc(s, org_id)
        row = asas_audit.append(
            s, org_id=org_id, actor="alice", action=action,
            resource_type="thing", resource_id="1", **kwargs
        )
        s.commit()
        return row.id
