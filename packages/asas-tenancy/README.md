# asas-tenancy

**One client's data stays invisible to another, and the database is what enforces
it.** The isolation boundary is Postgres row-level security, not a `WHERE` clause
in application code: a request binds its tenant once, from a verified credential,
and every query in that transaction is scoped by the database. A repository that
forgets to filter returns nothing rather than everything.

That inversion is the whole point. Application-level filtering fails open, so the
bug is a cross-tenant leak. Database-level filtering fails closed, so the bug is
an empty page.

```python
import asas_tenancy as tenancy

# per request (middleware), from the verified credential and never the body
tenancy.bind_tenant(claims["tid"])

# first thing in the unit of work, before any query
tenancy.set_tenant_guc(session, tenancy.current_tenant_id())
await tenancy.async_set_tenant_guc(session, tenant_id)   # async hosts

# in the migration that creates the table
tenancy.enable_rls("invoice", column="tenant_id", column_type="uuid")

# background work, no request behind it
with tenancy.tenant_engine(dsn, tenant_id) as engine:
    dispatch_outbox(engine)

# in the test suite
tenancy.assert_tables_protected(engine, TENANT_SCOPED_TABLES)
tenancy.assert_isolated(engine, "invoice", tenant_a, tenant_b)
```

Table-less **and** router-less variant of the Asas host contract, and the only
package in the family that owns nothing at all: no `migrate`, no `seed`, no
`build_routers`, no chain, no version table. It gives a host the pieces to
protect the host's own tables, so adoption cannot collide with an existing
schema.

## What each part is for

| Module | What it answers |
| --- | --- |
| `context` | Which tenant is the current work for? |
| `guc` | Telling Postgres, as a bound parameter, for this transaction. |
| `policy` | Four migration helpers: protect a table, unprotect it, lift the protection safely, add a NOT NULL column to a protected table. |
| `engine` | A connection pool dedicated to one tenant, disposed when the block ends. |
| `conformance` | Is any of the above actually in force? |

## Five things this encodes that are easy to get wrong

1. **`current_tenant_id()` raises rather than returning `None`.** The two read
   identically at every call site and behave catastrophically differently: a
   query built with no tenant does not fail, it returns every tenant's rows. A
   host with genuinely tenant-free work says so with `maybe_tenant_id()`.

2. **`FORCE` as well as `ENABLE`.** `ENABLE ROW LEVEL SECURITY` exempts the
   table's *owner*, and in most deployments the application connects as the
   owner. A table that is enabled but not forced looks protected in `psql` and
   protects nothing in production. This is the half that gets left off.

3. **The `UPDATE` in a migration that matches nothing.** A migration binds no
   tenant, so on a forced table the obvious backfill (add nullable, `UPDATE`,
   `SET NOT NULL`) filters every row, reports `UPDATE 0`, and dies on the
   `SET NOT NULL` with an error that blames the data. What makes it expensive is
   where it does *not* fail: it passes on an empty database, so CI and a fresh
   clone are green, and a developer connected as a superuser bypasses the policy
   regardless of `FORCE`, so manual testing looks fine too. The first thing that
   sees it is a deployment against real data. `add_tenant_scoped_column` takes
   the value from the column definition instead, which no policy can filter, and
   which since PG 11 is metadata-only rather than a table rewrite.

4. **Lifting `FORCE` safely.** Some migrations really do need to rewrite rows
   across tenants. Written by hand that is a bare `NO FORCE` ... `FORCE` pair,
   and outside a transactional DDL context a failure between them leaves the
   table unprotected with nothing in any log to say so. `without_force` is a
   context manager, so it cannot leak that.

5. **A pooled connection must not carry one tenant's session variable into
   another's work.** So the transaction-local pin is the default, and
   `tenant_engine` disposes its pool on exit. A tenant-blind pass over a
   protected table reads an empty set and reports success: no error, zero rows,
   and a queue that silently never drains.

## Portability, and why the conformance kit exists

Every policy helper is a **no-op outside Postgres**, so a host's chain stays
runnable on SQLite. That is exactly the trap worth naming: **a suite that only
runs on SQLite proves nothing about isolation.** Neither does one that runs on
Postgres as a superuser, because a superuser ignores row-level security with
`FORCE` set.

So the kit refuses to report success it cannot back:

- `unenforced_reason(engine)` says why isolation cannot be verified here, naming
  the role and the fix, or returns `None` when it can.
- `assert_tables_protected`, `assert_isolated` and `assert_unpinned_sees_nothing`
  all raise on a connection the policies do not apply to.
- `assert_isolated` refuses to pass **vacuously**: no rows means the assertion
  cannot fail, which is where the migration bug above hides.
- **`ASAS_REQUIRE_RLS=1`** turns "cannot check" from a skip into a failure. Set it
  in CI. Same shape `asas-storage` uses to stop its Azure leg vanishing silently.

The suggested host wiring, so the checks can never quietly stop running:

```python
reason = tenancy.unenforced_reason(engine)
if reason:
    if tenancy.is_required():
        pytest.fail(f"ASAS_REQUIRE_RLS is set but {reason}")
    pytest.skip(reason)
```

## Running the tests

```bash
cd packages/asas-tenancy && pip install -e '.[dev]' && pytest -q
```

SQLite by default, which exercises the context, the guards and the no-op path.
The isolation suites need Postgres **and a role the policies apply to**:

```bash
createuser --no-superuser asas_rls          # then ALTER ROLE asas_rls NOBYPASSRLS
createdb -O asas_rls asas_tenancy_test
TEST_DATABASE_URL=postgresql+psycopg://asas_rls@localhost/asas_tenancy_test \
  ASAS_REQUIRE_RLS=1 pytest -q
```

With a bypassing role those tests skip and say so; with `ASAS_REQUIRE_RLS=1` they
fail instead. A full run against a restricted role is `56 passed, 4 skipped` (the
four are the non-Postgres path and the two refusal paths, which can only be
exercised on a connection that does *not* enforce).

See the repo README for the full host contract.
