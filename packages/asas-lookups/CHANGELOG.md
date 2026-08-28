# Changelog — `asas-lookups`

Versions follow semver, and the git tag matches this file: `asas-lookups/v0.13.2`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.13.2 — 2026-08-28

- **The list ETag now varies with the query shape** (PR #11, re-landed). `GET
  /lookups/{type}`'s ETag was keyed on `(type, version, lang, org)` only, so
  page 2, `q=...`, `parent=...`, and `active=false` shared page 1's tag — a
  conforming HTTP client that cached page 1 got `304 Not Modified` for every
  other variant and reused the wrong body. The query shape (`active`, `q`,
  `parent`, `page`, `page_size`) is hashed into the tag; same-request
  revalidation still 304s, and a version bump still busts every variant.

## 0.13.1 — 2026-08-27

- `ensure_type` now declares its parameters instead of forwarding `**kwargs` to the model. The accepted names were invisible in the signature and the identifier is `key`, not `code` — the natural first guess, which failed with a bare `KeyError: 'key'`. `name` now defaults to `key`. Existing keyword calls are unchanged (Teamy TEAMY-809).

## 0.13.0 — 2026-08-27

- **Breaking: every lookup type declares an explicit scope** (issue #35) —
  `platform` (org-read-only reference data, the default) or `org` (org-owned
  vocabulary). Nothing is inferred from `code_system` or `is_open` at runtime;
  migration `0002` backfills existing types to `platform`, except legacy
  `is_open` types which backfill to `org` (an open list means org users add
  values, which only an org-owned type can host). The migration is resumable:
  a `scope` column that already exists but still holds NULLs is backfilled
  and tightened rather than skipped.
- **Platform types are immutable to orgs in full**: org context now gets 403
  on `create_value` too (previously an org could mint its own row on any type
  while the code was free). `is_open=True` is valid only on org types —
  enforced in `ensure_type` (ValueError) and the admin API (422).
- **Org types live wholly at org level.** The platform-held rows are a starter
  template: never served to org reads (an unseeded org sees an empty list, and
  a template-only code answers 404), managed from platform scope, and copied
  per org by the new exported **`seed_org_lookups(session, org_id)`** — called
  by the host at org creation, presence-idempotent per (type, code) so
  backfilling existing orgs is one call. **Required upgrade step**: a host
  whose database already has organizations MUST run it once per existing org
  after `migrate()` — the migration converts legacy open types to org scope
  but cannot enumerate the host's orgs, and until seeded those orgs read
  empty lists for every org-scoped type. Hierarchy parent pointers and
  supersede links are remapped to the org's own rows — fresh copies or values
  the org already had. A seed that creates rows bumps the type `version` so read-API ETags
  invalidate. Template drift is accepted by design; platform types keep
  automatic propagation.
- `LookupTypeCreate`/`LookupTypeRead` carry `scope` (typed as `TypeScope`,
  so an unknown scope is a schema-level 422); `TypeScope` is exported.
- `ensure_type` raises when a re-registration's explicit `scope` disagrees
  with the stored one — changing a type's scope moves ownership of every
  value, which is a data migration, never a silent side effect. Omitting
  `scope` keeps trusting the stored value.

## 0.12.1 — 2026-08-27

- **Supersede and parent pointers resolve through the caller's read scope**
  (issue #33, audit defect T-8). Both follows in `get_value_read` were bare
  `session.get` calls — a pointer landing in another org's row served that
  org's labels (or leaked its code) to a stranger. A pointer outside the
  caller's visible set now behaves as absent.
- **New `find_org_shadows(session)`** (exported): lists `(type_key, code,
  org_id)` for every legacy org row sharing a code with a platform row of the
  same type. Under the no-override model (issue #24) these are data to
  resolve deliberately — 0.12.0 blocks creating new ones, but rows predating
  it still shadow platform values in their org's reads.

## 0.12.0 — 2026-08-27

- **Breaking: platform lookup values are read-only for organizations** (issue #24;
  decides DR 0001's D2 the reject way). An org-context `update_value`,
  `deprecate_value`, `add_alias`, `remove_alias` or `merge_values` whose target
  resolves to a platform row (`org_id IS NULL`) now raises **403** instead of
  silently mutating the row every tenant shares (audit defect T-1). The new
  `_value_for_write` keeps the write path's row selection separate from the read
  path's org-or-global fallback; orgs keep full control of the values they
  created, and platform scope (no org resolver) edits global rows unchanged.
- **Seeding owns platform rows only** (audit defect T-5): existence checks
  predicate on `org_id IS NULL`, so an org-minted row sharing a seed code no
  longer suppresses the platform default — re-seeding heals it — and the
  salutation `show_in_name` backfill can no longer land on an org row.

## 0.11.0 — 2026-08-25

- **Breaking: the library no longer seeds a host's own vocabulary.** `seed(session)` previously wrote seventeen lookup types that belonged to Teamy rather than to any people system — work-item statuses and types, project health, risk and issue categories, team categories, contract types, social platforms, next-of-kin relationships, office locations, and the open CV vocabularies (skill, company, degree, field of study, education institution, awarding body, training provider). A second host acquired all of it at boot, with no error.

  What remains is standards-based or near-universal: `salutation`, `gender`, `marital_status`, `currency`, `country`, `nationality`. **A host that needs any of the removed types now registers them itself** using `ensure_type` / `ensure_value` / `bump_version_if`; existing rows are untouched, and a host re-seeding them keeps its data (the helpers are idempotent). Follows the precedent set when project roles left the library in TEAMY-487 (Teamy TEAMY-803).
- **Breaking:** `asas_lookups.seed` (the module) is now `asas_lookups.seeding`. `asas_lookups.seed` remains the seeding *callable*, which is what the host contract documents; only direct submodule imports need updating.
- **New public API:** `ensure_type`, `ensure_value` and `bump_version_if` are exported. They were `_`-prefixed and imported through the underscore by hosts anyway, which meant the boundary was in the wrong place. A host seeding its own lookup values needs exactly these three.
- **Adoption is now shape-verified.** `migrate()` previously decided adopt-vs-create on the sentinel table's *name* alone, so a host that already owned a table of that name had the baseline stamped as applied and skipped entirely — silently, and unrepairable by re-running. It now requires every baseline table to be present and the sentinel to carry the baseline's columns, and raises with the table, the package and the remedy otherwise (Teamy TEAMY-795).
- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-lookups @ v0.15.0` did not install
`asas-lookups` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
