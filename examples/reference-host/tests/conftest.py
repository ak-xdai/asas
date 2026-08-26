"""Fixtures for the reference host's suite.

Engine selection mirrors the package suites (SQLite by default, Postgres when
``TEST_DATABASE_URL`` is set), because "the host contract holds" is a claim
about both engines or it is not worth making.

The app is built per-test against a fresh database. That is slower than a
session-scoped client and it is the right trade here: the boot *sequence* is
half of what this suite verifies, so it has to actually run each time.
"""

from __future__ import annotations

import importlib
import os
import tempfile
import uuid

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

_TEST_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture()
def database_url(tmp_path):
    if _TEST_URL:
        eng = create_engine(_TEST_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        eng.dispose()
        return _TEST_URL
    return f"sqlite:///{tmp_path / 'helpdesk.db'}"


@pytest.fixture()
def app_module(database_url, tmp_path, monkeypatch):
    """Re-import the app against this test's database.

    The reload is load-bearing. ``app.config.settings`` and ``app.db.engine`` are
    module-level singletons — as they are in most real hosts — so pointing the
    app at a different database means re-importing, not mutating. Reloading also
    re-runs the registrations, which is what makes the boot-order tests honest.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    import app.config
    import app.db
    import app.main

    importlib.reload(app.config)
    importlib.reload(app.db)
    return importlib.reload(app.main)


@pytest.fixture()
def client(app_module):
    from fastapi.testclient import TestClient

    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture()
def session(app_module):
    with Session(app_module.engine) as s:
        yield s


@pytest.fixture()
def agents(client, app_module):
    """Three agents spanning the roles the policy distinguishes.

    Created through the session rather than an endpoint because agent
    provisioning is not what any of these tests are about.
    """
    from app.models import Agent

    rows = [
        Agent(name="Ada", email="admin@example.invalid", role="admin"),
        Agent(name="Lin", email="lead@example.invalid", role="member"),
        Agent(name="Sam", email="agent@example.invalid", role="member"),
        Agent(name="Vic", email="viewer@example.invalid", role="viewer"),
    ]
    with Session(app_module.engine) as s:
        for row in rows:
            s.add(row)
        s.commit()
        for row in rows:
            s.refresh(row)
        return {r.email.split("@")[0]: r for r in rows}
