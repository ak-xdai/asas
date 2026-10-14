# DR 0005 (Asas): async hosts, one implementation served to both kinds of application

Status: DRAFT for review · Author: ignacio.galindo@xdigit.ai (with Claude) · Date: 2026-09-26
Scope: how an application built on async SQLAlchemy (`AsyncSession`, asyncpg or
aiosqlite) consumes the table-owning Asas packages without a second engine, a
second transaction or a second implementation. Covers asas-notifications,
asas-audit, asas-tenancy, asas-jobs, asas-lookups, asas-access, asas-workflow
and asas-search. asas-storage is related but separate (section 7).

## 1. Problem

Every table-owning Asas package is synchronous: its service functions take a
`sqlmodel`/SQLAlchemy `Session`, `migrate(engine)` takes a sync `Engine`, and the
reference host is sync throughout. That is right for the hosts Asas was
extracted from, and it is the reason this DR keeps the sync core (section 3).

An async host cannot pass its `AsyncSession` to any of those functions, so today
each one builds its own bridge. ad-recruiter (FastAPI, SQLAlchemy 2.0 async,
Postgres RLS) has run asas-notifications in production this way since 0.16, and
the bridge taught three lessons the hard way:

1. **The sync session must be flushed before it goes out of scope.** `notify`
   flushes once for the outbox ids, then adds `notification_delivery` rows and
   leaves the final flush to the caller's unit of work. Across a bridge that
   session is about to be discarded, so without an explicit flush the
   notification persists and its deliveries vanish: no email, no error, nothing
   in the outbox to retry.
2. **Nothing may be read off returned ORM objects afterwards.** They are detached
   the moment the bridge returns, and a lazy attribute read raises. Everything
   the caller needs has to be copied to plain values inside the bridge.
3. **Two call sites needed a whole second stack.** Dispatch and `migrate` run
   outside any request, so the host built a second, sync engine and promoted a
   second database driver (psycopg) to a runtime dependency purely to serve the
   package, and then had to remember to bind the RLS tenant on that engine's
   connections too (a dispatch pass with no tenant bound reads an empty outbox
   and reports success).

None of that is ad-recruiter specific. Every async host rediscovers it, which is
exactly what principle 2 ("the subtle cases solved once") says a package should
prevent, and what principle 7 ("nothing to forget") says must not be left to the
host.

## 2. Needs

- **N1 (sync host)** nothing changes: same functions, same signatures, same
  behavior, no new dependency.
- **N2 (async host)** a documented, supported way to call every public service
  function from an `AsyncSession`, in the host's own transaction, without a
  second engine or driver.
- **N3 (package author)** the logic is written once; adding a public function
  cannot silently leave async hosts without it.
- **N4 (operator)** isolation guarantees that hold for a sync host (RLS GUC,
  org filters) hold identically for an async one.

## 3. Requirements

- **R1** The sync API stays the source of truth. No package logic is written
  twice.
- **R2** An async call runs on the **caller's connection and transaction**: a
  rollback in the host takes the package's writes with it, exactly as for a sync
  host. (This is the property an async-only core could not give sync hosts: a
  sync `Session` cannot drive an async driver on its own connection, so a sync
  host would be pushed onto a second connection and a second transaction. It is
  why the core stays sync.)
- **R3** Database I/O does not block the event loop.
- **R4** Async entry points return values that are safe to use after the call:
  no detached ORM instances whose attributes lazy-load.
- **R5** Pending writes are flushed before control returns to the host.
- **R6** Every public session-taking function has an async counterpart, and a
  test fails if one is missing.
- **R7** `migrate` is callable from an async engine without a second engine.
- **R8** Both engines keep CI coverage: the existing SQLite and Postgres legs,
  plus an async leg on each (aiosqlite, asyncpg).

## 4. Options considered

| Option | Summary | Verdict |
|---|---|---|
| A. Async-first rewrite, sync wrapper on top | Rewrite services as `async def`; sync hosts call through a wrapper | **Rejected.** A sync wrapper over async code needs an event loop per call or a loop thread and a second connection, which breaks R2 for every sync host, today's main consumers included. |
| B. Parallel async implementation | A second copy of each service written against `AsyncSession` | **Rejected.** Violates R1; two implementations drift, which is the failure Asas exists to prevent. |
| C. Thread offload | `await loop.run_in_executor(None, sync_fn, sync_session, ...)` | **Rejected.** Needs a sync session, therefore a sync engine and driver, therefore a separate connection and transaction (breaks R2, R7 and the "no second stack" goal). |
| **D. Async facade over the sync core via `AsyncSession.run_sync`** | One-line `aio` wrappers that run the sync function on the caller's connection through SQLAlchemy's greenlet bridge | **Chosen.** Same connection and transaction (R2); the sync ORM code runs in a greenlet and every database call is awaited on the async driver, so the loop is not blocked (R3); logic is written once (R1). Proven in production by ad-recruiter. |

## 5. Design

### 5.1 One `aio` module per table-owning package

```python
# asas_notifications/aio.py
from sqlalchemy.ext.asyncio import AsyncSession

from asas_notifications import service
from asas_notifications._bridge import in_host_transaction


async def notify(session: AsyncSession, **kwargs) -> service.NotifyResult:
    """Async twin of :func:`asas_notifications.notify`. Runs on the caller's
    connection, inside the caller's transaction."""
    return await in_host_transaction(session, service.notify, **kwargs)
```

The host imports `asas_notifications.aio as notifications` and keeps its call
sites unchanged apart from `await`.

### 5.2 The bridge helper: flush, then hand back plain values

```python
async def in_host_transaction(session: AsyncSession, fn, /, *args, **kwargs):
    def call(sync_session):
        result = fn(sync_session, *args, **kwargs)
        sync_session.flush()          # R5: nothing pending when we return
        return result
    return await session.run_sync(call)
```

It takes the host's `AsyncSession`, so any GUC the host bound on that
connection (RLS tenant, `SET LOCAL` settings) applies to the package's
statements unchanged (N4). It opens no transaction and commits nothing: the host
owns both, as it does for a sync call.

### 5.3 Return values (R4)

Functions whose sync form returns ORM instances get an async twin that returns
a **frozen read model** (a dataclass or pydantic model of the fields a caller
reads), built inside the bridge while the session is live. Functions that
already return ids, counts, booleans or plain dicts wrap unchanged. Approximate
split today:

| Package | Public session-taking functions | Return ORM objects (need a read model) |
|---|---|---|
| asas-notifications (0.16 line, #44) | 14 | 7 |
| asas-audit (#48/#55) | 3 | 3 |
| asas-jobs | 9 | 3 |
| asas-search | 4 | 1 |
| asas-access | 19 | 4 |
| asas-lookups | 24 | 10 |
| asas-workflow | 20 | 14 |

Read models are also what the feed and admin routers serialise, so several
already exist as schemas and can be reused rather than invented.

### 5.4 `migrate` from an async engine (R7)

`migrate` accepts an `Engine` **or** a `Connection`. Today each package's
`migrations/env.py` opens its own connection (`engine.connect()` on the engine
passed through `config.attributes`), which a `Connection` cannot do, so the
change is small but real in two files per package: `migrate` puts whichever it
was given into `config.attributes`, and `env.py` uses a given connection as is
and only calls `.connect()` on an engine (`sa.inspect` already accepts both).
An async host then needs no sync engine:

```python
async with async_engine.begin() as conn:
    await conn.run_sync(notifications.migrate)
```

### 5.5 Background runners

- **asas-notifications dispatch**: an async twin of the dispatch pass that takes
  an `async_sessionmaker` and a per-session binder hook (the async form of the
  existing context binder), so a multi-tenant host binds its tenant exactly
  where it does for requests. This retires the host's second engine.
- **asas-jobs**: the runner accepts coroutine handlers and awaits them on the
  loop with a session from the host's `async_sessionmaker`; sync handlers keep
  running in the executor as today. Registration stays one line; the runner
  inspects the handler (`inspect.iscoroutinefunction`), so there is nothing new
  to declare.

### 5.6 Routers

Out of scope for phase 1. An async host that wants a package's routes can wrap
them in its own (ad-recruiter does so already). A later phase may let
`build_routers` take an async session dependency.

### 5.7 Nothing to forget (R6)

Each package gains one conformance test: enumerate the public functions whose
first parameter is a `Session` and assert `aio` exposes a coroutine of the same
name. A new public function without a twin fails CI rather than failing an async
host in production.

### 5.8 Testing (R8)

Each table-owning package adds an async CI leg per engine (aiosqlite locally and
in CI, asyncpg against the Postgres service) that runs the package's existing
behavioral tests through the `aio` twins. The sync legs are unchanged.

## 6. Versioning and compatibility

Purely additive: new `aio` modules, `migrate` accepting a `Connection`, optional
coroutine handlers. A minor bump per package under the pre-1.0 rule; no sync
host changes a line. `sqlalchemy[asyncio]` (greenlet) becomes an **optional**
extra (`asas-<pkg>[async]`), so sync hosts install nothing new.

## 7. Related but separate: asas-storage

asas-storage's problem is not the session: its backends call blocking SDKs, so
`run_sync` does not help. An async host needs native async backends
(`azure.storage.blob.aio`, an async S3 client) and a streaming `put` that stages
blocks instead of taking the whole object as bytes. That is its own proposal;
ad-recruiter's measured block-staging implementation is available as the
starting point.

## 8. Phasing

| Phase | Scope | Why this order |
|---|---|---|
| P1 | The bridge helper, asas-notifications `aio` + async dispatch + `migrate(Connection)` + conformance test + async CI leg | Already proven by a production host; lets ad-recruiter delete its bridge, second engine and psycopg runtime dependency. |
| P2 | asas-audit and asas-tenancy | Smallest surfaces (3 functions for audit); both are pending review in #55, so the facade can land with them. |
| P3 | asas-jobs coroutine handlers | Unblocks async hosts running periodic work on the durable queue. |
| P4 | asas-lookups, asas-access, asas-workflow, asas-search | On demand, when an async host adopts one. Workflow carries the most read-model work. |

## 9. Open questions for review

1. **Where does the helper live?** It is ~15 identical lines in every
   table-owning package, which is the README's own trigger for a shared kernel
   ("an `asas-core` appears only when a third package repeats identical code").
   Options: a minimal `asas-core` holding only the bridge and the conformance
   test, or a copied private `_bridge.py` per package. Proposal: `asas-core`,
   since the conformance test is also identical.
2. **Read models or eager ORM?** Section 5.3 proposes frozen read models. The
   alternative, returning ORM rows with every needed attribute loaded and
   `expire_on_commit=False`, is less code but leaves the lazy-load trap open for
   any attribute nobody loaded.
3. **Do the sync hosts intend to move to async?** If Teamy and the reference host
   are moving anyway, the balance shifts toward an async core with a sync
   facade for scripts. This DR assumes they are not; the maintainers should say.
4. **Router variants**: is an async `build_routers` wanted, or do async hosts
   keep writing their own thin routes?
