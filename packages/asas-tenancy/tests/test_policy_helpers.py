"""The migration helpers, run inside a real Alembic operations context.

Not a string comparison against the SQL they emit: that would pass while the
statements did nothing. Each test runs the helper against a live database and
then reads the catalogue back, or observes the behaviour the helper exists to
produce.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

import asas_tenancy
from conftest import TENANT_A, TENANT_B


@contextmanager
def migration(engine):
    """An Alembic operations context bound to a real connection, so ``op`` works
    the way it does inside a revision."""
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            yield conn


def _protection(engine, table):
    return asas_tenancy.protection_report(engine, [table])[0]


def test_enable_rls_enables_forces_and_writes_a_policy(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with migration(engine) as conn:
        conn.execute(
            text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id uuid NOT NULL)")
        )
        asas_tenancy.enable_rls("thing", column="tenant_id", column_type="uuid")

    report = _protection(engine, "thing")
    assert report.enabled
    # The half that gets left off, and the reason the whole helper exists: without
    # FORCE the table's owner is exempt, and the application is usually the owner.
    assert report.forced
    assert asas_tenancy.policy_name("thing") in report.policies
    assert report.protected


def test_the_emitted_policy_actually_isolates(engine, requires_enforcement):
    """End to end: build a table with the helper, then prove two tenants cannot
    see each other's rows through it."""
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with migration(engine) as conn:
        conn.execute(
            text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id uuid NOT NULL)")
        )
        asas_tenancy.enable_rls("thing", column="tenant_id", column_type="uuid")
        conn.execute(text("ALTER TABLE thing NO FORCE ROW LEVEL SECURITY"))
        conn.execute(text("INSERT INTO thing VALUES (1, :a), (2, :b)"),
                     {"a": TENANT_A, "b": TENANT_B})
        conn.execute(text("ALTER TABLE thing FORCE ROW LEVEL SECURITY"))

    asas_tenancy.assert_isolated(engine, "thing", TENANT_A, TENANT_B)
    asas_tenancy.assert_unpinned_sees_nothing(engine, "thing")


def test_shared_rows_are_readable_by_everyone_and_writable_by_none(engine, requires_enforcement):
    """A config table carrying platform rows with no tenant. Readable by all,
    which is the point; still not insertable by ordinary traffic, because a
    request that could create a NULL-tenant row could publish data to every
    tenant at once."""
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with migration(engine) as conn:
        conn.execute(text("CREATE TABLE cat (id integer PRIMARY KEY, tenant_id uuid)"))
        asas_tenancy.enable_rls("cat", column="tenant_id", column_type="uuid",
                                shared_rows=True)
        conn.execute(text("ALTER TABLE cat NO FORCE ROW LEVEL SECURITY"))
        conn.execute(text("INSERT INTO cat VALUES (1, NULL), (2, :a)"), {"a": TENANT_A})
        conn.execute(text("ALTER TABLE cat FORCE ROW LEVEL SECURITY"))

    assert asas_tenancy.visible_keys(engine, "cat", TENANT_A) == {1, 2}
    assert asas_tenancy.visible_keys(engine, "cat", TENANT_B) == {1}

    with pytest.raises(sa.exc.ProgrammingError):
        with engine.begin() as conn:
            asas_tenancy.set_tenant_guc(conn, TENANT_A)
            conn.execute(text("INSERT INTO cat VALUES (3, NULL)"))


def test_disable_rls_undoes_it(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with migration(engine) as conn:
        conn.execute(text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id uuid NOT NULL)"))
        asas_tenancy.enable_rls("thing", column="tenant_id", column_type="uuid")
        asas_tenancy.disable_rls("thing")
    report = _protection(engine, "thing")
    assert not report.enabled and not report.forced and not report.policies


def test_without_force_restores_force(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with migration(engine) as conn:
        conn.execute(text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id uuid NOT NULL)"))
        asas_tenancy.enable_rls("thing", column="tenant_id", column_type="uuid")
        with asas_tenancy.without_force("thing"):
            assert not _forced(conn, "thing")
            conn.execute(text("INSERT INTO thing VALUES (1, :a)"), {"a": TENANT_A})
        assert _forced(conn, "thing")
    assert _protection(engine, "thing").forced


def test_without_force_does_not_swallow_the_bodys_error(engine):
    """The restore is attempted quietly on the failure path so it can never
    replace the real exception with a confusing one about an aborted
    transaction. Losing the original error is the worse outcome, and the
    transaction rollback deals with the switch anyway."""
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with pytest.raises(ValueError, match="backfill blew up"):
        with migration(engine) as conn:
            conn.execute(text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id uuid NOT NULL)"))
            asas_tenancy.enable_rls("thing", column="tenant_id", column_type="uuid")
            with asas_tenancy.without_force("thing"):
                raise ValueError("backfill blew up")


def test_add_tenant_scoped_column_backfills_under_force(engine, requires_enforcement):
    """The D216 lesson, as a test.

    The obvious spelling (add nullable, UPDATE, SET NOT NULL) is asserted right
    below this to fail, because a migration binds no tenant and FORCE filters the
    UPDATE to nothing. This helper takes the value from the column definition,
    which no policy can filter.
    """
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with migration(engine) as conn:
        conn.execute(text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id uuid NOT NULL)"))
        asas_tenancy.enable_rls("thing", column="tenant_id", column_type="uuid")
        conn.execute(text("ALTER TABLE thing NO FORCE ROW LEVEL SECURITY"))
        conn.execute(text("INSERT INTO thing VALUES (1, :a), (2, :b)"),
                     {"a": TENANT_A, "b": TENANT_B})
        conn.execute(text("ALTER TABLE thing FORCE ROW LEVEL SECURITY"))

        asas_tenancy.add_tenant_scoped_column(
            "thing", "chapter", sa.String(16), default="'chapter_2'"
        )

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE thing NO FORCE ROW LEVEL SECURITY"))
        rows = conn.execute(text("SELECT chapter FROM thing")).scalars().all()
        default = conn.execute(
            text(
                "SELECT column_default FROM information_schema.columns"
                " WHERE table_name='thing' AND column_name='chapter'"
            )
        ).scalar()
        conn.execute(text("ALTER TABLE thing FORCE ROW LEVEL SECURITY"))

    assert rows == ["chapter_2", "chapter_2"], "every existing row was backfilled"
    assert default is None, "the default is dropped, so a later insert must state it"


def test_the_naive_backfill_really_does_fail(engine, requires_enforcement):
    """The bug this package exists to stop, reproduced.

    ``UPDATE`` on a forced table from a migration matches zero rows, silently, so
    the following ``SET NOT NULL`` is what fails, with an error that blames the
    data rather than the policy. It passes on an empty database, which is why CI
    and a fresh clone both stay green.
    """
    if engine.dialect.name != "postgresql":
        pytest.skip("row-level security is a Postgres feature")
    with pytest.raises(sa.exc.IntegrityError):
        with migration(engine) as conn:
            conn.execute(text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id uuid NOT NULL)"))
            asas_tenancy.enable_rls("thing", column="tenant_id", column_type="uuid")
            conn.execute(text("ALTER TABLE thing NO FORCE ROW LEVEL SECURITY"))
            conn.execute(text("INSERT INTO thing VALUES (1, :a)"), {"a": TENANT_A})
            conn.execute(text("ALTER TABLE thing FORCE ROW LEVEL SECURITY"))

            conn.execute(text("ALTER TABLE thing ADD COLUMN chapter varchar(16)"))
            result = conn.execute(text("UPDATE thing SET chapter = 'chapter_2'"))
            assert result.rowcount == 0, "the policy filtered every row, silently"
            conn.execute(text("ALTER TABLE thing ALTER COLUMN chapter SET NOT NULL"))


def test_helpers_are_no_ops_off_postgres(engine):
    """A host's chain has to stay runnable on SQLite. It just is not protected
    there, which is what the conformance kit says out loud."""
    if engine.dialect.name == "postgresql":
        pytest.skip("this is the non-Postgres path")
    with migration(engine) as conn:
        conn.execute(text("CREATE TABLE thing (id integer PRIMARY KEY, tenant_id text NOT NULL)"))
        asas_tenancy.enable_rls("thing", column="tenant_id", column_type="text")
        with asas_tenancy.without_force("thing"):
            conn.execute(text("INSERT INTO thing VALUES (1, 'a')"))
        asas_tenancy.disable_rls("thing")
        assert conn.execute(text("SELECT count(*) FROM thing")).scalar() == 1


def _forced(conn, table):
    return bool(
        conn.execute(
            text("SELECT relforcerowsecurity FROM pg_class WHERE relname = :t"),
            {"t": table},
        ).scalar()
    )


@pytest.mark.parametrize("bad", ["", "1thing", "thing; DROP TABLE x", "thing-name"])
def test_bad_identifiers_are_refused(engine, bad):
    """Table names are migration constants rather than input, but they are
    interpolated into DDL that cannot take binds, so they are checked."""
    if engine.dialect.name != "postgresql":
        pytest.skip("the identifier guard runs on the Postgres path")
    with pytest.raises(ValueError):
        with migration(engine):
            asas_tenancy.enable_rls(bad, column="tenant_id", column_type="uuid")


def test_bad_cast_type_is_refused(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("the identifier guard runs on the Postgres path")
    with pytest.raises(ValueError):
        with migration(engine):
            asas_tenancy.enable_rls("thing", column="tenant_id",
                                    column_type="uuid); DROP TABLE x; --")
