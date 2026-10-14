"""The kit that decides whether the isolation is real, rather than merely written.

Every other module here writes protection. This one checks it, and it exists
because the two most expensive lessons behind this package are both about checks
that passed while nothing was enforced:

* **A superuser ignores row-level security, ``FORCE`` included.** So a developer
  or a CI job connected as one gets a green suite on a database where the
  policies do nothing. :func:`unenforced_reason` is how a host's test setup finds
  that out and says so instead of reporting success.

* **An empty table proves nothing.** An isolation assertion over no rows passes
  for the wrong reason, which is how a broken migration reached a deployment
  after clearing CI. :func:`assert_isolated` refuses to pass vacuously.

Nothing here imports pytest. The host decides whether an unenforced database is a
skip or a failure, and :data:`REQUIRE_ENV` is the convention for saying which:
skip locally where the bypass is deliberate, fail in CI where it is not. That is
the same shape ``asas-storage`` uses to stop its Azure leg vanishing silently.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine

from asas_tenancy.guc import DEFAULT_GUC, set_tenant_guc
from asas_tenancy.policy import has_rls, policy_name

#: Set this in CI. When it is set, a database that cannot enforce isolation is a
#: failure rather than a skip, so the checks can never quietly stop running.
REQUIRE_ENV = "ASAS_REQUIRE_RLS"


def is_required() -> bool:
    """Whether enforcement is mandatory in this environment (see :data:`REQUIRE_ENV`)."""
    return os.environ.get(REQUIRE_ENV, "").strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class Enforcement:
    """What this connection can actually enforce."""

    dialect: str
    role: Optional[str]
    is_superuser: bool
    bypasses_rls: bool

    @property
    def supported(self) -> bool:
        """Whether the database has row-level security at all."""
        return has_rls(self.dialect)

    @property
    def enforced(self) -> bool:
        return self.supported and not self.is_superuser and not self.bypasses_rls


def enforcement_status(engine: Engine) -> Enforcement:
    """Inspect the connected role. Cheap: one query, no schema assumptions."""
    dialect = engine.dialect.name
    if not has_rls(dialect):
        return Enforcement(dialect=dialect, role=None, is_superuser=False,
                           bypasses_rls=False)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT current_user, rolsuper, rolbypassrls"
                " FROM pg_roles WHERE rolname = current_user"
            )
        ).one()
    return Enforcement(dialect=dialect, role=row[0], is_superuser=bool(row[1]),
                       bypasses_rls=bool(row[2]))


def unenforced_reason(engine: Engine) -> Optional[str]:
    """Why isolation is not enforced on this connection, or ``None`` if it is.

    The string is written to be read in a skip message or a CI failure, so it
    names the role and the remedy rather than just the verdict.
    """
    status = enforcement_status(engine)
    if not status.supported:
        return (
            f"{status.dialect} has no row-level security, so tenant isolation "
            f"cannot be enforced or verified here. Isolation assertions need "
            f"Postgres; run this suite against one."
        )
    if status.is_superuser:
        return (
            f"the connected role {status.role!r} is a SUPERUSER, which ignores "
            f"row-level security even with FORCE set, so these assertions would "
            f"pass on an unprotected database. Connect as a non-superuser "
            f"NOBYPASSRLS role."
        )
    if status.bypasses_rls:
        return (
            f"the connected role {status.role!r} has BYPASSRLS, so the policies "
            f"do not apply to it. Use ALTER ROLE {status.role} NOBYPASSRLS, or "
            f"connect as a role that does not have it."
        )
    return None


def require_enforced(engine: Engine) -> None:
    """Raise unless this connection really is subject to the policies."""
    reason = unenforced_reason(engine)
    if reason:
        raise RuntimeError(f"Tenant isolation is not enforced here: {reason}")


@dataclass(frozen=True)
class TableProtection:
    """What is actually on one table, read from the catalogue rather than assumed."""

    table: str
    exists: bool
    enabled: bool
    forced: bool
    policies: tuple[str, ...]

    @property
    def protected(self) -> bool:
        return self.exists and self.enabled and self.forced and bool(self.policies)

    def complaint(self) -> Optional[str]:
        """One line saying what is missing, or ``None`` when nothing is."""
        if not self.exists:
            return f"{self.table}: no such table"
        missing = []
        if not self.enabled:
            missing.append("RLS not enabled")
        if not self.forced:
            # Called out specifically because it is the half that gets left off,
            # and because the table looks protected without it.
            missing.append("RLS enabled but NOT FORCED (the table owner is exempt)")
        if not self.policies:
            missing.append("no policy (RLS with no policy denies everything)")
        return f"{self.table}: {', '.join(missing)}" if missing else None


def protection_report(engine: Engine, tables: Iterable[str]) -> tuple[TableProtection, ...]:
    """Read enabled / forced / policies for each table out of the catalogue."""
    wanted = list(tables)
    if not has_rls(engine.dialect.name):
        return tuple(
            TableProtection(table=t, exists=False, enabled=False, forced=False,
                            policies=())
            for t in wanted
        )
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,"
                " coalesce(array_agg(p.polname) FILTER (WHERE p.polname IS NOT NULL), '{}')"
                " FROM pg_class c"
                " JOIN pg_namespace n ON n.oid = c.relnamespace"
                " LEFT JOIN pg_policy p ON p.polrelid = c.oid"
                " WHERE c.relname = ANY(:tables) AND n.nspname = current_schema()"
                " GROUP BY c.relname, c.relrowsecurity, c.relforcerowsecurity"
            ),
            {"tables": wanted},
        ).all()
    found = {r[0]: r for r in rows}
    return tuple(
        TableProtection(
            table=t,
            exists=t in found,
            enabled=bool(found[t][1]) if t in found else False,
            forced=bool(found[t][2]) if t in found else False,
            policies=tuple(found[t][3]) if t in found else (),
        )
        for t in wanted
    )


def assert_tables_protected(engine: Engine, tables: Sequence[str]) -> None:
    """Every named table is enabled, forced, and carries a policy.

    This is the sweep to run over a host's whole tenant-scoped table list. The
    failure it catches is the ordinary one: somebody adds a table and forgets the
    policy, which no feature test notices because the application works
    perfectly. It just works for every tenant at once.
    """
    require_enforced(engine)
    complaints = [c for c in (p.complaint() for p in protection_report(engine, tables)) if c]
    if complaints:
        raise AssertionError(
            "Tables are not protected by tenant isolation:\n  "
            + "\n  ".join(complaints)
            + "\n\nProtect each one in the migration that creates it:"
            + "\n  asas_tenancy.enable_rls('<table>', column=..., column_type=...)"
        )


def expected_policy_name(table: str) -> str:
    """The policy name :func:`asas_tenancy.policy.enable_rls` writes, re-exported
    so a host's own assertion does not spell it a second way."""
    return policy_name(table)


def visible_keys(engine: Engine, table: str, tenant_id: Any, *, key: str = "id",
                 guc: str = DEFAULT_GUC) -> set:
    """The keys of ``table`` visible to ``tenant_id``, read under a real pin.

    Uses a transaction and a transaction-local pin, so it leaves nothing behind
    on the pooled connection.
    """
    with engine.begin() as conn:
        set_tenant_guc(conn, tenant_id, guc=guc)
        rows = conn.execute(text(f"SELECT {key} FROM {table}")).scalars().all()
    return set(rows)


def assert_isolated(engine: Engine, table: str, tenant_a: Any, tenant_b: Any, *,
                    key: str = "id", guc: str = DEFAULT_GUC) -> None:
    """Two tenants see disjoint rows of ``table``, and at least one sees something.

    **The second half is the point.** An isolation assertion over an empty table
    passes without testing anything, which is exactly how a broken protection
    reaches production with a green suite behind it. So a vacuous pass is an
    error here, and the message says to seed a row for each tenant first.
    """
    require_enforced(engine)
    a = visible_keys(engine, table, tenant_a, key=key, guc=guc)
    b = visible_keys(engine, table, tenant_b, key=key, guc=guc)
    if not a and not b:
        raise AssertionError(
            f"Vacuous isolation check on {table!r}: neither tenant can see any "
            f"row, so this would pass even with no policy at all. Seed a row for "
            f"each tenant before asserting."
        )
    overlap = a & b
    if overlap:
        raise AssertionError(
            f"Tenant isolation is broken on {table!r}: {len(overlap)} row(s) are "
            f"visible to both tenants ({sorted(overlap)[:5]}). Check the policy's "
            f"USING clause and that the tenant column is the one it compares."
        )


def assert_unpinned_sees_nothing(engine: Engine, table: str) -> None:
    """With no tenant pinned, ``table`` yields nothing. The fail-closed half.

    Worth asserting separately from :func:`assert_isolated`: a policy can scope
    correctly between two bound tenants and still leak to a connection that bound
    none, which is precisely the state every background job starts in.
    """
    require_enforced(engine)
    with engine.begin() as conn:
        count = conn.execute(text(f"SELECT count(*) FROM {table}")).scalar()
    if count:
        raise AssertionError(
            f"{table!r} returned {count} row(s) with no tenant pinned. Isolation "
            f"must fail closed: an unbound connection is what a boot sweep, a "
            f"background job and a misconfigured request all look like."
        )
