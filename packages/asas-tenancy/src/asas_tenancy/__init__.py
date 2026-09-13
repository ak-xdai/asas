"""Asas tenancy — one client's data stays invisible to another, enforced by Postgres.

The isolation boundary is **row-level security in the database**, not a filter in
application code. A request binds its tenant once, from a verified credential;
every query in that transaction is then scoped by the database itself, so a
repository that forgets a ``WHERE`` clause returns nothing rather than everything.

What the package is:

* :mod:`~asas_tenancy.context` — which tenant the current work belongs to.
  :func:`current_tenant_id` raises rather than returning ``None``, because "no
  tenant" and "all tenants" must never be the same value.
* :mod:`~asas_tenancy.guc` — handing that tenant to Postgres, as a bound
  parameter, for the current transaction.
* :mod:`~asas_tenancy.policy` — four migration helpers, run inside the host's own
  chain: protect a table in one line, lift the protection safely when a data
  migration truly needs to, and add a NOT NULL column to a protected table the
  one way that works.
* :mod:`~asas_tenancy.engine` — a pool dedicated to one tenant, for work with no
  request behind it, disposed when the block ends.
* :mod:`~asas_tenancy.conformance` — the checks that say whether any of the above
  is actually in force, and refuse to pass for the wrong reason.

Host contract shape: **no tables, no routers, no chain of its own**. This package
owns nothing in the database; it gives the host the pieces to protect the host's
own tables. So there is no ``migrate`` and no ``seed``, and adoption cannot
collide with an existing schema.

Portability, stated plainly because the alternative is a false sense of safety:
the policy helpers are **no-ops outside Postgres**, so a host's chain stays
runnable on SQLite, and a suite that only ever runs on SQLite proves nothing
about isolation. That is what :mod:`~asas_tenancy.conformance` is for, and why
``ASAS_REQUIRE_RLS=1`` in CI turns "cannot check" from a skip into a failure.
"""

from asas_tenancy.conformance import (
    Enforcement,
    TableProtection,
    assert_isolated,
    assert_tables_protected,
    assert_unpinned_sees_nothing,
    enforcement_status,
    protection_report,
    require_enforced,
    unenforced_reason,
    visible_keys,
)
from asas_tenancy.context import (
    TenantContextError,
    bind_tenant,
    clear_tenant,
    current_tenant_id,
    maybe_tenant_id,
    reset,
    tenant_scope,
)
from asas_tenancy.engine import pin_engine, tenant_engine
from asas_tenancy.policy import (
    add_tenant_scoped_column,
    disable_rls,
    enable_rls,
    has_rls,
    policy_name,
    without_force,
)
from asas_tenancy.guc import (
    DEFAULT_GUC,
    async_set_tenant_guc,
    read_tenant_guc,
    set_tenant_guc,
    supports_guc,
)

__version__ = "0.1.0"

__all__ = [
    "add_tenant_scoped_column",
    "assert_isolated",
    "assert_tables_protected",
    "assert_unpinned_sees_nothing",
    "async_set_tenant_guc",
    "bind_tenant",
    "clear_tenant",
    "current_tenant_id",
    "DEFAULT_GUC",
    "disable_rls",
    "enable_rls",
    "Enforcement",
    "enforcement_status",
    "has_rls",
    "maybe_tenant_id",
    "pin_engine",
    "policy_name",
    "protection_report",
    "read_tenant_guc",
    "require_enforced",
    "reset",
    "set_tenant_guc",
    "supports_guc",
    "TableProtection",
    "tenant_engine",
    "tenant_scope",
    "TenantContextError",
    "unenforced_reason",
    "visible_keys",
    "without_force",
    "__version__",
]
