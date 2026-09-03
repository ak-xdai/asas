"""Standalone package fixtures.

Engine: SQLite temp file by default; Postgres when TEST_DATABASE_URL is set (the CI
matrix runs both). Schema always comes from the package's own migration chain via
``migrate(engine)``. The kind registry, adapters, and configured seams are
process-global — reset around every test.
"""

import os
import tempfile
import uuid

import pytest
from sqlmodel import Session, create_engine

import asas_notifications
from asas_notifications import channels, service

_TEST_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _clean_registries():
    # Bare-package default: a context resolver supplying org 1 (the library
    # stamps org_id from it; hosts with ORM tenancy listeners do it there).
    service.configure_context_resolver(lambda s: (0, 1))
    yield
    service._KINDS.clear()
    channels._ADAPTERS.clear()
    service._context_resolver = None
    service._recipient_filter = None
    service.config_cache_clear()


@pytest.fixture()
def engine():
    if _TEST_URL:
        from sqlalchemy import text

        eng = create_engine(_TEST_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    else:
        path = os.path.join(
            tempfile.gettempdir(), f"asas_notifications_{uuid.uuid4().hex}.db"
        )
        eng = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}
        )
    yield eng
    eng.dispose()
    if not _TEST_URL:
        os.unlink(path)


@pytest.fixture()
def migrated(engine):
    asas_notifications.migrate(engine)
    return engine


@pytest.fixture()
def session(migrated):
    with Session(migrated) as s:
        yield s


@pytest.fixture()
def kind(session):
    """A registered 'normal' (email-routing) kind with a unique name."""
    name = f"test.{uuid.uuid4().hex[:8]}"
    asas_notifications.register_kind(
        name, category="info", urgency="normal", reason="participant"
    )
    return name


@pytest.fixture()
def ambient_kind(session):
    """A registered 'low' (in-app only) kind with a unique name."""
    name = f"ambient.{uuid.uuid4().hex[:8]}"
    asas_notifications.register_kind(
        name, category="info", urgency="low", reason="participant"
    )
    return name


def emit(session, kind, recipients, **kw):
    kw.setdefault("title", "Hello")
    rows = asas_notifications.notify(session, recipients, kind, **kw)
    session.commit()
    return rows
