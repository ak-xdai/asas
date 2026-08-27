"""Standalone package fixtures.

Engine: SQLite temp file by default; Postgres when TEST_DATABASE_URL is set (the CI
matrix runs both). Schema always comes from the package's own migration chain via
``migrate(engine)``. The registries (specs, handlers, resolvers, callbacks,
renderers, floor, event subscribers) are process-global — reset around every test.
"""

import os
import tempfile
import uuid

import pytest
from sqlmodel import Session, create_engine

import asas_workflow
from asas_workflow import definitions, engine, events, registry

_TEST_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _clean_registries():
    yield
    definitions.clear_registered_specs()
    events.clear_subscribers()
    registry._SYSTEM_HANDLERS.clear()
    registry._COMPLETION_CALLBACKS.clear()
    registry._SUBJECT_RENDERERS.clear()
    registry._ASSIGNEE_RESOLVERS.clear()
    registry._NAMESPACE_RESOLVERS.clear()
    registry._BINDABLE_PURPOSES.clear()
    registry._FLOOR_RESOLVER = None
    engine._org_resolver = None


@pytest.fixture()
def engine():
    if _TEST_URL:
        from sqlalchemy import text

        eng = create_engine(_TEST_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        path = os.path.join(tempfile.gettempdir(), f"asas_workflow_{uuid.uuid4().hex}.db")
        eng = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )
    yield eng
    eng.dispose()
    if not _TEST_URL:
        os.unlink(path)


@pytest.fixture()
def migrated(engine):
    asas_workflow.migrate(engine)
    return engine


@pytest.fixture()
def session(migrated):
    with Session(migrated) as s:
        yield s


@pytest.fixture()
def floor():
    """A registered fail-closed floor (member 999) — the engine requires one."""
    registry.register_floor_resolver(lambda session, org_id=None: {999})
    return 999
