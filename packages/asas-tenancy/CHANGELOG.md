# Changelog — `asas-tenancy`

Versions follow semver, and the git tag matches this file: `asas-tenancy/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 — unreleased

First release. Extracted from a host application's working implementation rather
than written from scratch, so the shapes below are the ones that survived
production rather than a first guess at them.

- **The tenant context** (`bind_tenant`, `current_tenant_id`, `maybe_tenant_id`,
  `tenant_scope`). `current_tenant_id()` raises on an unbound context rather than
  returning `None`, because "no tenant" and "all tenants" must never be the same
  value. `tenant_scope` restores rather than clears, so a nested scope cannot wipe
  its parent's tenant.
- **The GUC pin** (`set_tenant_guc`, `async_set_tenant_guc`, `read_tenant_guc`,
  `supports_guc`). A bound parameter through `set_config`, transaction-local by
  default, with the statement built once per GUC name. The async variant imports
  no async session type, so the package installs without greenlet or an async
  driver. All three are **no-ops off Postgres**, the same rule the policy helpers
  follow, so a dual-engine host runs one code path: `set_config` is a Postgres
  function, and without this a SQLite suite fails on the *pin* rather than on the
  absent isolation, which sends the reader looking in the wrong place. The pin
  going quiet is honest, since there is no policy to feed either; what never goes
  quiet is the claim that isolation exists, which the conformance kit refuses to
  make.
- **Migration helpers** (`enable_rls`, `disable_rls`, `without_force`,
  `add_tenant_scoped_column`), which run inside the host's own chain. `enable_rls`
  emits `ENABLE`, `FORCE` **and** the policy: without `FORCE` the table owner is
  exempt, and the application is usually the owner. The policy's cast is
  `NULLIF(current_setting(...), '')::<type>`, so an empty pin filters rather than
  raising. `shared_rows=True` makes platform rows with a NULL tenant readable by
  every tenant and writable by none.
- **`add_tenant_scoped_column`** backfills from the column definition
  (`ADD COLUMN ... NOT NULL DEFAULT x`, then drop the default) rather than with a
  DML `UPDATE`, which a forced policy filters to zero rows from a migration that
  binds no tenant. Metadata-only since PG 11, and it leaves no window in which the
  column is NULL. The naive spelling's failure is pinned as a test.
- **`tenant_engine`** for work with no request behind it: every connection pinned
  to one tenant, pool disposed on exit.
- **The conformance kit** (`unenforced_reason`, `require_enforced`,
  `protection_report`, `assert_tables_protected`, `assert_isolated`,
  `assert_unpinned_sees_nothing`). Every assertion refuses to run on a connection
  the policies do not apply to, `assert_isolated` refuses to pass over an empty
  table, and `ASAS_REQUIRE_RLS=1` promotes "cannot check" from a skip to a
  failure.
- Alembic is an optional extra (`[migrations]`), needed by the four helpers and by
  nothing else. Importing the package without it works; calling a helper raises
  with that instruction.
- Licensed **Apache 2.0**, with `LICENSE` and `NOTICE` in the wheel and
  `License-Expression: Apache-2.0` in the metadata.
- `tests/test_host_contract.py` per TEAMY-798, plus a test asserting this package
  exposes **no** `migrate` and no `seed`: owning nothing is what makes it the
  cheapest package in the family to adopt.
