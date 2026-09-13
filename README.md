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
<<<<<<< HEAD
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
=======
| `asas-lookups` | `asas_lookups` | table-owning: package Alembic chain (DR 0017 pilot) |
| `asas-validation` | `asas_validation` | table-less contract variant |
| `asas-storage` | `asas_storage` | table-less, router-less variant |
| `asas-ratelimit` | `asas_ratelimit` | table-less, router-less variant |
| `asas-jobs` | `asas_jobs` | table-owning: package Alembic chain |
| `asas-access` | `asas_access` | table-owning: package Alembic chain |
| `asas-workflow` | `asas_workflow` | table-owning: package Alembic chain |
| `asas-notifications` | `asas_notifications` | table-owning + router variant |
| `asas-search` | `asas_search` | dialect-branched chain: PG deep tier |
| `asas-mcp` | `asas_mcp` | protocol-only variant |
| `asas-tenancy` | `asas_tenancy` | table-less, router-less, chain-less variant |
| `asas-audit` | `asas_audit` | table-owning + router variant (depends on `asas-tenancy`) |
| `asas-cli` | `asas_cli` | developer CLI (`asas add`, `asas new`) — no host contract, install-time only |

All ten planned modules are extracted (Teamy epic TEAMY-466, complete 2026-07-29);
`asas-cli` is a companion developer tool on top of them, not an eleventh module.
`asas-tenancy` and `asas-audit` are not from that epic: both are extracted from a
second consumer's working implementation. `asas-tenancy` is the first package
whose subject is a property of the *host's own* tables rather than of tables it
owns, and `asas-audit` is the first package to depend on another (see the note
under **Rules** below).
Current versions are per package — see each package's `CHANGELOG.md`, and
[`RELEASING.md`](RELEASING.md) for the tag scheme.
>>>>>>> ak/audit-hardening

## The host contract

Every package exposes the same five-part surface — nothing more:

<<<<<<< HEAD
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
=======
| Package | Routers | Schema | Seeding | Host hooks |
| --- | --- | --- | --- | --- |
| `asas-lookups` | `build_routers` | `migrate` | `seed` | `configure_org_resolver` |
| `asas-access` | — | `migrate` | `seed_field_permissions`, `seed_action_permissions`, `ensure_system_groups`, `ensure_clearance_levels` | — |
| `asas-workflow` | — | `migrate` | `seed_workflow_definitions` | — |
| `asas-notifications` | `build_router` | `migrate` | — | `configure_context_resolver`, `configure_recipient_filter` |
| `asas-jobs` | — | `migrate` | `ensure_schedule` | `configure_context_binder`, `configure_runner` |
| `asas-search` | — | `migrate` | — | — |
| `asas-storage` | — | — | — | `configure` |
| `asas-validation` | `build_router` | — | — | — |
| `asas-ratelimit` | — | — | — | `configure` |
| `asas-mcp` | `build_mcp_app` | — | — | — |
| `asas-tenancy` | — | — | — | migration helpers (`enable_rls`, …) |
| `asas-audit` | `build_router` | `migrate` | — | — |

Reading the table:

1. **Routers.** `build_routers(get_session)` is **plural** when a package returns a bundle
   (lookups returns `read` + `admin`) and **singular** when it returns one `APIRouter`. `asas-mcp`
   is neither — it returns a mounted ASGI app. In all cases the factory takes the host's FastAPI
   session dependency. **Auth is composition-time**: the host applies its own guards when
   including the routers; libraries never learn the host's auth model.
2. **`migrate(engine)`** — applies the package-owned Alembic chain (package-scoped version table,
   adopt-or-create bootstrap). Call it **after** the host's own chain, not before: an adopting
   host's historical migrations must have created the tables before `migrate()` looks for them.
   Adoption is shape-verified — a host that already owns a table of the same name gets a loud
   error rather than a silently skipped baseline.

   **If your host builds its schema with `SQLModel.metadata.create_all`, pass `tables=`.**
   That metadata object is process-global, so importing any Asas package registers *its*
   tables into it and a bare `create_all(bind)` creates them. The package's own `migrate()`
   then fails — and fails into exactly the brownfield shape (tables present, no version
   table), so the error blames adoption for what was really a host-side sweep. A host with
   its own Alembic chain is unaffected **only if its `env.py` targets host-only metadata,
   or filters these tables out** — an `env.py` that hands autogenerate the process-global
   `SQLModel.metadata` picks up the imported Asas tables the same way, and will start
   emitting migrations for tables it does not own.
3. **Seeding** is idempotent and host-called at boot, but it is **not** uniformly named `seed`.
   `asas-lookups` seeds only vocabulary that is standards-based or near-universal — salutation,
   gender, marital status, currency, country, nationality. **Your own product's words are yours
   to seed**; register them with `ensure_type` / `ensure_value` / `bump_version_if`.
   Only `asas-lookups` seeds pure reference data with no host input; the others seed *host policy*
   and so take it as an argument, which is why they read `seed_field_permissions(session, …)`
   rather than `seed(session)`. Packages with nothing to seed expose nothing.
4. **`configure_*` hooks** — optional callables for host concerns, defaulting to
   single-tenant/no-op. `configure_org_resolver(fn)` is the canonical example: tenancy stays a
   *host* concept, and a host that never calls it runs single-tenant with no tenancy engine at all.
   `asas-tenancy` fills none of the four slots: it owns no tables, so it has no chain to compose
   and nothing to seed, and its surface is the tenant context, the RLS session GUC, four helpers a
   host calls inside its **own** migrations, and a conformance kit. Note that the `org_id` filters
   in the packages above are defence in depth rather than an isolation boundary; where a host needs
   the boundary itself, that is what `asas-tenancy` is for.
5. **Service functions take an explicit `Session`** — no engine, session factory, or settings
   import inside a library.

Every package declares `__all__`, and no name in it that the contract calls callable resolves to
a submodule — a trap that cost real time before it was pinned by a test.

## Rules

- **No app imports, ever.** Packages depend on FastAPI/SQLModel/Alembic and each other's
  published surface — never on a host application.
- **Dual-engine portability**: every package runs on SQLite and Postgres; migrations use batch
  mode, `native_enum=False`, portable server defaults. CI runs both engines per package.
- **No shared kernel yet**: the contract above is a convention, not a package. An `asas-core`
  appears only when a third package repeats identical code. `asas-audit` depending on
  `asas-tenancy` is not that rule being broken: it is one package *using* another's subject
  matter, not shared plumbing being hoisted. The alternative was a second copy of the RLS
  policy SQL in the audit chain, and on the one table where cross-tenant visibility would be
  worst, two definitions that can drift was judged the higher cost. The dependency is declared
  by NAME (`asas-tenancy>=0.1`) rather than by git URL, so it is satisfied by the consumer's
  own top-level pin: a URL would hardcode the upstream remote (which a mirroring consumer
  rewrites) and pin one tenancy tag per audit release.
- **Per-package versioning**: each package versions independently and its tag carries its name
  (`asas-lookups/v0.11.0`), so a pin says exactly what it installs. Lockstep was the original
  choice (DR 0017) and decayed — see [`RELEASING.md`](RELEASING.md) for what went wrong, the
  release procedure, the support window, and the historical tag mapping.
>>>>>>> ak/audit-hardening

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
