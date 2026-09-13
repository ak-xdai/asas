"""The package-owned chain: create, idempotence, and guarded adoption."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy import text

import asas_audit
from asas_audit.migrate import VERSION_TABLE


def test_migrate_creates_the_table_and_its_version_row(engine):
    asas_audit.migrate(engine)
    inspector = sa.inspect(engine)
    assert inspector.has_table("audit_event")
    assert inspector.has_table(VERSION_TABLE), (
        "the version table is package-scoped, so this chain never collides with "
        "the host's own"
    )


def test_migrate_is_idempotent(engine):
    asas_audit.migrate(engine)
    asas_audit.migrate(engine)
    assert sa.inspect(engine).has_table("audit_event")


def test_adoption_refuses_an_unrelated_table_of_the_same_name(engine):
    """A table name is not an identity, and stamping is irreversible in effect:
    it records the baseline as applied, so re-running cannot repair it."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE audit_event (id integer PRIMARY KEY)"))
    with pytest.raises(RuntimeError, match="cannot adopt"):
        asas_audit.migrate(engine)


def test_the_tail_index_exists(engine):
    """Every append reads the newest row for one tenant, so the index that
    answers it without a sort is not an optimisation, it is the append path."""
    asas_audit.migrate(engine)
    names = {ix["name"] for ix in sa.inspect(engine).get_indexes("audit_event")}
    assert "ix_audit_event_org_seq" in names


def test_seq_autoincrements_on_this_dialect(engine):
    """SQLite only auto-increments a plain ``INTEGER PRIMARY KEY``, so a bigint
    one is an ordinary column there and every insert fails its NOT NULL. The
    column uses ``with_variant`` for exactly that, and this is the test that says
    so on both engines rather than only on the one where it happened to work."""
    asas_audit.migrate(engine)
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(text("ALTER TABLE audit_event NO FORCE ROW LEVEL SECURITY"))
        for n in (1, 2):
            conn.execute(
                text(
                    "INSERT INTO audit_event"
                    " (id, org_id, actor, action, resource_type, resource_id,"
                    "  payload, occurred_at, hash_current)"
                    " VALUES (:i, 'o', 'a', 'x', 't', '1', '{}', :ts, :h)"
                ),
                {"i": f"id-{n}", "ts": "2026-01-01T00:00:00+00:00", "h": b"\x00" * 32},
            )
        rows = conn.execute(text("SELECT seq FROM audit_event ORDER BY seq")).scalars().all()
    assert rows == [1, 2]
