"""Migration helpers: protect a tenant-scoped table in one line.

These run inside the **host's own** Alembic chain, next to the ``create_table``
that made the table. This package owns no tables and has no chain of its own.

Why a library and not a snippet: the four helpers below encode four things that
are individually easy to get wrong and collectively decide whether the isolation
exists at all.

1. **``FORCE`` as well as ``ENABLE``.** ``ENABLE ROW LEVEL SECURITY`` exempts the
   table's OWNER from the policy, and in most deployments the application
   connects as the owner. So a table that is enabled but not forced looks
   protected in ``psql`` and protects nothing in production. This is the half
   people leave out.

2. **The empty-string cast.** ``current_setting('app.tenant_id', true)`` returns
   ``NULL`` when unset, and ``NULL`` compares false, so an unbound request sees
   nothing: fail-closed, which is the point. But a GUC set to the empty string
   is a *different* state, and ``''::uuid`` raises rather than filtering.
   ``NULLIF(..., '')`` folds the two together.

3. **The ``UPDATE`` that matches nothing.** See
   :func:`add_tenant_scoped_column`.

4. **Lifting ``FORCE`` safely.** See :func:`without_force`.

Everything here is a no-op on a database that has no row-level security (SQLite),
so a host's chain stays portable. That portability is also the trap this package
is loudest about: **a suite that runs only on SQLite proves nothing about
isolation**, which is what :mod:`asas_tenancy.conformance` exists to say out loud.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Iterator, Optional

from asas_tenancy.guc import DEFAULT_GUC, validate_guc_name

#: Dialects that have row-level security. Everything else gets a no-op.
_RLS_DIALECTS = frozenset({"postgresql"})


def _op():
    """Alembic's ``op`` proxy, imported lazily.

    Alembic is an optional dependency: it is needed by these four helpers and by
    nothing else in the package, so a host that only wants the context and the
    GUC pin does not have to install it.
    """
    try:
        from alembic import op
    except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
        raise ModuleNotFoundError(
            "asas_tenancy.policy needs Alembic, which is an optional extra "
            "(`pip install 'asas-tenancy[migrations]'`). These helpers only make "
            "sense inside a migration; the tenant context and the GUC pin do not "
            "need it."
        ) from exc
    return op


def has_rls(dialect_name: Optional[str] = None) -> bool:
    """Whether the database in this migration supports row-level security."""
    if dialect_name is None:
        dialect_name = _op().get_bind().dialect.name
    return dialect_name in _RLS_DIALECTS


def _identifier(name: str, what: str) -> str:
    """Validate a table or column name.

    These are constants written by a migration author, never input, but they are
    interpolated into DDL that cannot take binds, so they are checked rather
    than trusted. Refusing anything outside ``[A-Za-z_][A-Za-z0-9_]*`` also
    keeps the emitted SQL readable, which matters when someone reads it back out
    of ``pg_policies`` two years later.
    """
    if not name or not name.replace("_", "").isalnum() or name[0].isdigit():
        raise ValueError(f"Not a usable {what} name for a policy: {name!r}")
    return name


def _type_name(name: str) -> str:
    """Validate a SQL type spelling for the policy's cast.

    Looser than :func:`_identifier` because real type names carry spaces
    ("timestamp with time zone"), and strict about everything else: no quotes,
    no parentheses, no semicolons, so the cast cannot become a second statement.
    """
    if not name or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_ ]*", name):
        raise ValueError(f"Not a usable SQL type for a policy cast: {name!r}")
    return name


def _predicate(column: str, column_type: str, guc: str) -> str:
    return f"{column} = NULLIF(current_setting('{guc}', true), '')::{column_type}"


def policy_name(table: str) -> str:
    """The policy name this package writes for ``table``.

    Exposed because a downgrade has to drop it and the conformance kit has to
    look for it, and three spellings of one name is how those drift apart.
    """
    return f"{table}_tenant_isolation"


def enable_rls(
    table: str,
    *,
    column: str = "org_id",
    column_type: str = "bigint",
    guc: str = DEFAULT_GUC,
    shared_rows: bool = False,
) -> None:
    """Enable, force, and write the isolation policy for ``table``. One line.

    ``column`` and ``column_type`` are the host's tenant column and its cast
    (``org_id``/``bigint`` in Asas' own packages, ``tenant_id``/``uuid`` in a
    host that keys tenants by UUID). The cast has to be right: the GUC is text,
    and comparing text to the column without it either fails or, worse, does a
    silent implicit conversion.

    ``shared_rows=True`` is for a CONFIG table that carries platform rows
    belonging to no tenant (a catalogue seeded once, with tenant overrides on
    top). Those rows become readable by everyone, and deliberately **not
    writable** by ordinary traffic: ``WITH CHECK`` still demands a matching
    tenant, so a request cannot insert a row that every tenant would then see.
    Seeding those rows is boot or migration work, done with
    :func:`without_force` or as a role that bypasses.
    """
    if not has_rls():
        return
    op = _op()
    table = _identifier(table, "table")
    column = _identifier(column, "column")
    column_type = _type_name(column_type)
    validate_guc_name(guc)

    read = _predicate(column, column_type, guc)
    if shared_rows:
        read = f"{column} IS NULL OR {read}"

    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    # The half that is usually missing: without FORCE the owner is exempt, and
    # the application usually IS the owner.
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {policy_name(table)} ON {table}"
        f" USING ({read})"
        f" WITH CHECK ({_predicate(column, column_type, guc)})"
    )


def disable_rls(table: str) -> None:
    """Undo :func:`enable_rls`, for a downgrade. Drops the policy first, since a
    policy cannot outlive the switch it hangs off."""
    if not has_rls():
        return
    op = _op()
    table = _identifier(table, "table")
    op.execute(f"DROP POLICY IF EXISTS {policy_name(table)} ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


@contextmanager
def without_force(table: str) -> Iterator[None]:
    """Lift ``FORCE`` for the block, then put it back.

    Some migrations genuinely need to rewrite rows across every tenant: a
    vocabulary change, a data repair, a column split. A migration binds no
    tenant, so under ``FORCE`` those statements match nothing.

    **Why a context manager and not two ``op.execute`` calls.** Written by hand
    it is a bare ``NO FORCE`` ... ``FORCE`` pair, and if anything between them
    raises inside a non-transactional DDL context, ``FORCE`` stays off and the
    table has silently lost its isolation with nothing in any log to say so.

    Two paths, both safe, and it is worth knowing which is which. Inside a
    transactional migration (the usual case) the whole thing rolls back on
    failure, so ``FORCE`` was never really lifted. Outside one, the ``finally``
    is what restores it. The restore is attempted quietly in the failure path so
    that it can never replace the original exception with a confusing one about
    an aborted transaction: losing the real error is worse, and the rollback has
    already dealt with the switch.

    Prefer :func:`add_tenant_scoped_column` over this for the common case of
    backfilling a new column, because that needs no DML at all.
    """
    if not has_rls():
        yield
        return
    op = _op()
    table = _identifier(table, "table")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    try:
        yield
    except BaseException:
        try:
            op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        except Exception:  # noqa: BLE001 - see the docstring
            pass
        raise
    else:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def add_tenant_scoped_column(
    table: str,
    column: str,
    type_,
    *,
    default: str,
    keep_default: bool = False,
) -> None:
    """Add a NOT NULL column to a table that already holds rows, correctly.

    The obvious way is ``ADD COLUMN`` nullable, ``UPDATE`` to backfill, then
    ``SET NOT NULL``. **On a table with FORCE ROW LEVEL SECURITY that UPDATE
    matches nothing**, because a migration binds no tenant, so the policy filters
    every row: it reports ``UPDATE 0`` and the ``SET NOT NULL`` then dies with
    "column contains null values".

    What makes it expensive to find is where it does *not* fail. It passes on an
    empty database, so CI and a fresh clone are both green, and a developer
    connected as a superuser bypasses the policy regardless of ``FORCE``, so
    manual testing looks fine too. The first thing that sees it is a deployment
    against real data.

    So the value comes from the column DEFINITION, which no policy can filter:
    ``ADD COLUMN ... NOT NULL DEFAULT x``, then drop the default. Since Postgres
    11 that is metadata only, no table rewrite, and it also leaves no window in
    which the column exists and is NULL.

    ``default`` is raw SQL for the server default (``"'chapter_2'"``, ``"0"``,
    ``"now()"``). Pass ``keep_default=True`` when the column should go on
    defaulting for future inserts; the default is to drop it, so that a later
    insert has to state the value.
    """
    op = _op()
    table_id = _identifier(table, "table")
    column_id = _identifier(column, "column")
    from sqlalchemy import Column, text as sa_text

    op.add_column(
        table,
        Column(column, type_, nullable=False, server_default=sa_text(default)),
    )
    if not keep_default:
        op.execute(f"ALTER TABLE {table_id} ALTER COLUMN {column_id} DROP DEFAULT")
