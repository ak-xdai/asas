# Asas (أساس)

**Asas** ("foundation") is a family of reusable FastAPI/SQLModel libraries extracted from
[Teamy](https://github.com/wlootah-a11y/teamy) — self-contained backend modules any internal
product can install: reference-data lookups, access control, validation, background jobs, and more.

Design record: `docs/src/content/docs/architecture/decisions/0017-asas-libraries.md` in the Teamy
repo (DR 0017, epic TEAMY-466).

## Packages

| Package | Import root | Shape |
| --- | --- | --- |
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
| `asas-cli` | `asas_cli` | developer CLI (`asas add`, `asas new`) — no host contract, install-time only |

All ten planned modules are extracted (Teamy epic TEAMY-466, complete 2026-07-29);
`asas-cli` is a companion developer tool on top of them, not an eleventh module.
Current versions are per package — see each package's `CHANGELOG.md`, and
[`RELEASING.md`](RELEASING.md) for the tag scheme.

## The host contract

A package exposes the parts that **apply to it** — not all five, and the names differ where
the shapes genuinely differ. This table is the contract; it is generated from the real surface
and pinned by a conformance suite (`tests/test_host_contract.py` in every package).

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
  appears only when a third package repeats identical code.
- **Per-package versioning**: each package versions independently and its tag carries its name
  (`asas-lookups/v0.11.0`), so a pin says exactly what it installs. Lockstep was the original
  choice (DR 0017) and decayed — see [`RELEASING.md`](RELEASING.md) for what went wrong, the
  release procedure, the support window, and the historical tag mapping.

## Consuming

The easiest path is `asas-cli` (see `packages/asas-cli`): `asas add <package>` pins
one package into an existing project's `pyproject.toml`; `asas new <name> --with
<packages>` scaffolds a new FastAPI project with a working boot sequence already
wired. Neither hides anything — both just generate the same manual wiring below,
so you can always drop the CLI and do it by hand.

Manual pin via a git install (no package index):

```text
asas-lookups @ git+https://github.com/wlootah-a11y/asas.git@asas-lookups/v0.11.0#subdirectory=packages/asas-lookups
```

Each package carries its own `CHANGELOG.md`. Tags before 2026-08-25 are repo-wide
(`v0.15.0`) under the retired lockstep scheme; [`RELEASING.md`](RELEASING.md) maps them
to the package versions they actually contained.

## Developing

Each package is standalone: `cd packages/<name>`, `pip install -e '.[dev]'`, `pytest -q`.
Set `TEST_DATABASE_URL=postgresql+psycopg2://…` to run a package's suite on Postgres
(unset ⇒ SQLite), mirroring Teamy's convention.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). Every package
declares `License-Expression: Apache-2.0` and ships both files inside its wheel,
so an SBOM or license scan reads the same answer from the repo and from an
installed artefact.

All runtime dependencies are permissive (MIT or BSD). The one copyleft component
anywhere in the tree is `psycopg2-binary` (LGPL with exceptions), a **test-only
extra** — it is imported to run the Postgres suite, never vendored or
redistributed, so its terms do not reach this code.

Asas is licensed separately from, and more permissively than, the products built
on it.
