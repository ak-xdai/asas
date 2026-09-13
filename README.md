# Asas (أساس)

**Asas** ("foundation") is an embedded application foundation for FastAPI/SQLModel
products: a family of self-contained backend packages — reference-data lookups,
access control, validation, notifications, background jobs, workflow, search,
storage, rate limiting, MCP tooling — that install *into* your application
instead of standing beside it. Your database, your auth, your deployment; no
broker, no Redis, no SaaS dependency, no second platform to operate. Extracted
from [Teamy](https://github.com/wlootah-a11y/teamy) (DR 0017, epic TEAMY-466),
where every package first survived production.

## Why Asas

Every internal product eventually rebuilds the same capabilities, and the naive
version of each is an afternoon's work while the correct version is weeks plus a
long tail of production lessons. Asas is the second version, already paid for:

- **The hard parts are solved once.** Transactional outboxes, duplicate-safe
  concurrent dispatch, edit-aware validation, stale-claim recovery, timezone and
  leap-day edges, dual-engine migrations — argued out and tested in a real
  deployment, not rediscovered per app.
- **Identical behavior on every path.** The API, the web form, the bulk import,
  and an AI agent hit the same seams and get the same answers.
- **Safe defaults are the easy ones.** Visibility filtering at the emit
  boundary, actor exclusion, fail-loud wiring checks — the security-sensitive
  choice is the low-effort choice.
- **Suitable for government and private-cloud deployments.** Host database,
  package-owned migrations, auditable behavior, nothing phones home.
- **Cheap to adopt, cheap to leave.** No host imports, no host foreign keys;
  org and user references are plain ints. Removing a package is a table drop,
  not a refactor.

## Design principles

Every Asas package holds to the same twelve principles, in no particular order:

1. **Generic core, zero business logic.** A package applies to a broad range of
   applications; business meaning is composed at call sites or configured at
   the edge, never baked into the package's vocabulary.
2. **Utility beyond plumbing.** A package earns its place by real savings — the
   subtle cases solved once, the production lessons already paid for. If it
   only rearranges code, it does not ship.
3. **Five-minute adoption.** Install, wire, and see the first result in under
   five minutes; the first rule, notification, or lookup costs one line.
4. **Batteries included.** Packages ship a rich content library on day one —
   validation ships a 44-check vocabulary, notifications ship routing defaults —
   so teams compose from a full shelf instead of rebuilding commodity parts.
5. **Plug-and-play defaults.** Empty configuration behaves correctly.
   Configuration stores deviations from good defaults, never a universe that
   must be filled in before anything works.
6. **Agent-friendly by construction.** Self-describing catalogs, uniform call
   shapes, machine-readable signatures: an AI agent can discover what a package
   offers and wire it into an application without reading prose.
7. **Nothing to forget.** No registration ceremony whose omission silently
   misroutes behavior; whatever must be known is derived automatically or fails
   loud at startup or the call site — never silently.
8. **One namespace with the application.** Packages reference the application's
   own vocabulary (its actions, its fields) rather than maintaining parallel
   catalogs that drift.
9. **Code owns logic, configuration owns dials.** Executable logic lives in
   code, reviewed and tested; runtime configuration covers tunable values only,
   and even that machinery is built when a real deployment asks for it.
10. **No second platform.** Embedded in the host: the host's database, no
    brokers, no queues, no external services; package-owned Alembic chains or
    no tables at all.
11. **One envelope per concern.** Errors, feeds, and contracts share one shape
    across packages, so a host and its clients keep a single code path for each
    concern.
12. **Explainable in plain language.** Every rule, kind, and setting renders as
    a sentence a product owner or an auditor understands; if it cannot be said
    as a sentence, it is in the wrong layer.

## Packages

| Package | Import root | Contract variant |
| --- | --- | --- |
| `asas-lookups` | `asas_lookups` | table-owning (DR 0017 pilot) |
| `asas-validation` | `asas_validation` | table-less |
| `asas-storage` | `asas_storage` | table-less, router-less |
| `asas-ratelimit` | `asas_ratelimit` | table-less, router-less |
| `asas-jobs` | `asas_jobs` | table-owning (package Alembic chain) |
| `asas-access` | `asas_access` | table-owning (package Alembic chain) |
| `asas-workflow` | `asas_workflow` | table-owning (package Alembic chain) |
| `asas-notifications` | `asas_notifications` | table-owning + router |
| `asas-search` | `asas_search` | dialect-branched chain (PG deep tier) |
| `asas-mcp` | `asas_mcp` | protocol-only |
| `asas-cli` | `asas_cli` | tooling |

Packages version independently; the current version of each lives in its
`CHANGELOG.md` and its `pyproject.toml`, tagged `<package>/vX.Y.Z` (see
`RELEASING.md`).

## The host contract

Every package exposes the same five-part surface — nothing more:

1. **`build_routers(get_session)`** — factory taking the host's FastAPI session
   dependency and returning the package's `APIRouter`s. Auth is
   composition-time: the host applies its own guards when including them;
   libraries never learn the host's auth model.
2. **`configure_*` hooks** — optional callables for host concerns (e.g.
   `configure_org_resolver(fn)` for multi-tenancy), defaulting to
   single-tenant/no-op.
3. **`seed(session)`** — idempotent reference-data seeding, called by the host
   at boot.
4. **`migrate(engine)`** — applies the package-owned Alembic chain
   (package-scoped version table, adopt-or-create bootstrap), called by the
   host at boot before its own chain.
5. **Service functions take an explicit `Session`** — no engine, session
   factory, or settings import inside a library.

## Rules

- **No app imports, ever.** Packages depend on FastAPI/SQLModel/Alembic and
  each other's published surface — never on a host application.
- **Dual-engine portability**: every package runs on SQLite and Postgres;
  migrations use batch mode, `native_enum=False`, portable server defaults. CI
  runs both engines per package.
- **No shared kernel yet**: the contract above is a convention, not a package.
  An `asas-core` appears only when a third package repeats identical code.
- **Per-package versioning**: each package tags and releases independently
  (`asas-lookups/v0.13.2`); pre-1.0, a breaking change bumps the minor. See
  `RELEASING.md`.

## Consuming

Pin a package tag via a git install (no package index):

```
asas-lookups @ git+https://github.com/wlootah-a11y/asas.git@asas-lookups/v0.13.2#subdirectory=packages/asas-lookups
```

## Developing

Each package is standalone: `cd packages/<name>`, `pip install -e '.[dev]'`,
`pytest -q`. Set `TEST_DATABASE_URL=postgresql+psycopg2://…` to run a package's
suite on Postgres (unset ⇒ SQLite), mirroring Teamy's convention.

Each package's full documentation is linked from its own `README.md`; design
records are proposed under `docs/design/` (DR 0003 notifications, DR 0004
validation — draft PRs).
