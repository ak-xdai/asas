# Changelog — `asas-lookups`

Versions follow semver, and the git tag matches this file: `asas-lookups/v0.11.1`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.11.1 — 2026-08-27

- `ensure_type` now declares its parameters instead of forwarding `**kwargs` to the model. The accepted names were invisible in the signature and the identifier is `key`, not `code` — the natural first guess, which failed with a bare `KeyError: 'key'`. `name` now defaults to `key`. Existing keyword calls are unchanged (Teamy TEAMY-809).

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
