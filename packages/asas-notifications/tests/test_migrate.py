"""The migrate(engine) contract: fresh-create, adopt-by-stamp, idempotence."""

import os

import pytest
import sqlalchemy as sa
from alembic import command

import asas_notifications
from asas_notifications.migrate import _SENTINEL_COLUMNS, _SENTINEL_TABLE, _BASELINE, VERSION_TABLE, _config

_TABLES = ("notification", "notification_delivery")


def test_fresh_create(engine):
    asas_notifications.migrate(engine)
    inspector = sa.inspect(engine)
    for t in _TABLES + (VERSION_TABLE,):
        assert inspector.has_table(t), t
    cols = {c["name"] for c in inspector.get_columns("notification_delivery")}
    assert "claimed_at" in cols  # the TEAMY-475 claim column is in the baseline
    cols = {c["name"] for c in inspector.get_columns("notification")}
    assert "archived_at" in cols  # the TEAMY-693 archive axis, added by 0002


def test_idempotent(engine):
    asas_notifications.migrate(engine)
    asas_notifications.migrate(engine)  # second run is a no-op, not an error


def test_adopts_existing_tables(engine):
    """A host whose own historical chain already created the tables (Teamy) must be
    stamped, not re-created: migrate() stamps the baseline and then runs the rest
    of the chain over the adopted tables.

    The adopted schema is built by running the chain *to the baseline* — not from
    today's SQLModel metadata, which drifts ahead of it with every migration the
    chain gains. An adopting host is by definition at the baseline shape.
    """
    command.upgrade(_config(engine), _BASELINE)
    with engine.begin() as conn:
        conn.execute(sa.text(f"DROP TABLE {VERSION_TABLE}"))
    inspector = sa.inspect(engine)
    assert inspector.has_table("notification")
    assert not inspector.has_table(VERSION_TABLE)
    assert "archived_at" not in {c["name"] for c in inspector.get_columns("notification")}

    asas_notifications.migrate(engine)

    inspector = sa.inspect(engine)
    assert inspector.has_table(VERSION_TABLE)
    with engine.connect() as conn:
        version = conn.execute(
            sa.text(f"SELECT version_num FROM {VERSION_TABLE}")  # noqa: S608
        ).scalar()
    assert version is not None
    # Adoption is not just a stamp — the post-baseline chain ran over the
    # adopted tables, so the host lands on the current schema.
    assert "archived_at" in {c["name"] for c in inspector.get_columns("notification")}


def test_rejects_a_foreign_table_of_the_same_name(engine):
    """A host that already owns an unrelated table called ``notification`` must get a
    loud error, not a silent adoption.

    Adoption keys on a table *name*, and a name is not an identity. Without this
    guard asas-notifications stamps the baseline as applied, therefore skips it entirely —
    leaving the baseline's sibling tables uncreated — and returns success, only
    to fail much later at runtime with no way to repair by re-running.
    """
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE notification ("
                "  id INTEGER PRIMARY KEY, candidate_id INTEGER, headline VARCHAR"
                ")"
            )
        )

    with pytest.raises(RuntimeError) as excinfo:
        asas_notifications.migrate(engine)

    message = str(excinfo.value)
    assert "notification" in message
    assert "asas-notifications" in message
    # Nothing was stamped, so a later run against a corrected database still works.
    assert not sa.inspect(engine).has_table(VERSION_TABLE)


def test_rejects_a_partial_baseline_schema(engine):
    """Sentinel present and correctly shaped, a sibling baseline table missing.

    Reported by CodeRabbit on asas#18 and reproduced before fixing: the sentinel
    check alone passed, migrate() stamped the baseline as applied, and
    'notification_delivery' was never created — silently, with the stamp meaning a re-run
    could not repair it. Same failure class as the foreign-table case, one layer
    down.
    """
    with engine.begin() as conn:
        coldefs = ", ".join(
            f"{c} INTEGER" if c == "id" else f"{c} VARCHAR"
            for c in sorted(_SENTINEL_COLUMNS)
        )
        conn.execute(sa.text(f"CREATE TABLE {_SENTINEL_TABLE} ({coldefs})"))

    with pytest.raises(RuntimeError) as excinfo:
        asas_notifications.migrate(engine)

    message = str(excinfo.value)
    assert 'notification_delivery' in message
    assert not sa.inspect(engine).has_table(VERSION_TABLE)


def test_upgrade_tolerates_missing_baseline_index_names(engine):
    """An adopting host was stamped, never having run 0001 — its historical
    chain may have named (or omitted) the baseline's indexes differently. 0003
    drops the subsumed single-column indexes only if they exist under the
    baseline names, so their absence must not wedge the boot migration."""
    command.upgrade(_config(engine), "0002")
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX ix_notification_user_id"))
        conn.execute(sa.text("DROP INDEX ix_notification_delivery_status"))

    asas_notifications.migrate(engine)  # must not raise

    names = {ix["name"] for ix in sa.inspect(engine).get_indexes("notification")}
    assert "ix_notification_user_org_archived_created" in names
    assert "ix_notification_user_org_read_archived" in names
    assert "ix_notification_user_id" not in names


def test_migration_0003_is_retry_safe(engine):
    """A partially-applied 0003 (e.g. an interrupted run on an engine without
    transactional DDL) must be retryable: creates skip indexes that already
    exist, drops skip ones already gone."""
    command.upgrade(_config(engine), "0002")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE INDEX ix_notification_user_org_read_archived "
                "ON notification (user_id, org_id, read_at, archived_at)"
            )
        )

    asas_notifications.migrate(engine)  # must not raise on the existing index

    names = {ix["name"] for ix in sa.inspect(engine).get_indexes("notification")}
    assert "ix_notification_user_org_archived_created" in names
    assert "ix_notification_user_id" not in names
    delivery = {
        ix["name"] for ix in sa.inspect(engine).get_indexes("notification_delivery")
    }
    assert "ix_notification_delivery_status_claimed" in delivery
    assert "ix_notification_delivery_status" not in delivery


@pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="pg_index.indisvalid is a Postgres catalog state",
)
def test_migration_0003_rebuilds_an_invalid_concurrent_index(engine):
    """An interrupted CREATE INDEX CONCURRENTLY leaves an INVALID index that
    the inspector reports like any other — the existence guard must not skip
    it. 0003 checks pg_index.indisvalid and drops + rebuilds such an index.

    The invalid state is simulated by flipping the catalog flag directly (the
    documented shape of an interrupted concurrent build), since a real
    interruption cannot be produced deterministically in a test. Writing
    pg_catalog needs a superuser role — CI's service user is one — so a run
    against an unprivileged TEST_DATABASE_URL skips rather than errors."""
    command.upgrade(_config(engine), "0002")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE INDEX ix_notification_user_org_archived_created "
                "ON notification (user_id, org_id, archived_at, created_at, id)"
            )
        )
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "UPDATE pg_catalog.pg_index SET indisvalid = false "
                    "WHERE indexrelid = 'ix_notification_user_org_archived_created'::regclass"
                )
            )
    except sa.exc.ProgrammingError:
        pytest.skip("simulating an INVALID index needs a superuser role")

    asas_notifications.migrate(engine)  # must drop + rebuild, not skip

    with engine.connect() as conn:
        valid = conn.execute(
            sa.text(
                "SELECT i.indisvalid FROM pg_catalog.pg_index i "
                "WHERE i.indexrelid = 'ix_notification_user_org_archived_created'::regclass"
            )
        ).scalar()
    assert valid is True
