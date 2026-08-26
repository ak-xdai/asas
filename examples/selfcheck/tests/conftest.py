"""Fixtures for the self-check suite.

Engine selection mirrors the package suites exactly (SQLite temp file by
default, Postgres when ``TEST_DATABASE_URL`` is set) because the checker reads
schema state through Alembic's own version tables, and "does that read work on
both engines" is precisely the kind of thing that is true until it isn't.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import create_engine

import asas_lookups
import asas_storage

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asas_selfcheck as sc  # noqa: E402

_TEST_URL = os.environ.get("TEST_DATABASE_URL")


def _migrating_modules():
    """Every installed package the registry says owns a schema.

    Derived rather than listed, so that "a correctly migrated host" means *all*
    of them. A hardcoded list silently stops being the whole truth the moment
    another package is installed alongside, and then the fixture that is
    supposed to represent a correct host starts producing failures of its own.
    """
    modules = []
    for spec in sc.REGISTRY:
        if not spec.migrates:
            continue
        module = sc._import(spec.module)
        if module is not None:
            modules.append(module)
    return modules


@pytest.fixture()
def engine():
    if _TEST_URL:
        eng = create_engine(_TEST_URL)
        with eng.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        yield eng
        eng.dispose()
    else:
        path = os.path.join(tempfile.gettempdir(), f"asas_selfcheck_{uuid.uuid4().hex}.db")
        eng = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        yield eng
        eng.dispose()
        os.unlink(path)


@pytest.fixture()
def migrated(engine):
    """A host that ran every migrate() — the schema half done right."""
    for module in _migrating_modules():
        module.migrate(engine)
    return engine


@pytest.fixture(autouse=True)
def clean_process_state():
    """Hook state is process-global, so tests must not leak into each other.

    Snapshot and restore rather than reset-to-None: the suite may run in a
    process where something else legitimately configured a package.
    """
    storage_factory = asas_storage._factory
    storage_instance = asas_storage._instance
    lookups_resolver = asas_lookups.service._org_resolver
    yield
    asas_storage._factory = storage_factory
    asas_storage._instance = storage_instance
    asas_lookups.service._org_resolver = lookups_resolver
