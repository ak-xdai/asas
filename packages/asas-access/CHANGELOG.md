# Changelog — `asas-access`

Versions follow semver, and the git tag matches this file: `asas-access/v0.14.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.14.0 — 2026-08-27

- **`redact_view` now redacts mappings, and refuses shapes it cannot redact.** It nulled fields via `hasattr`/`setattr` only, so a plain `dict` read model matched nothing, came back unchanged, and the restricted field reached the caller **with no error** — a redaction function failing open. Dict and other `MutableMapping` projections are now redacted by key; a shape that is neither object nor mapping raises `TypeError` naming the fields it would have disclosed. Hosts passing Pydantic models (the common case) are unaffected. Found by the reference host (Teamy TEAMY-807).

## 0.13.0 — 2026-08-25

- Internal: `asas_access.seed` (the module) is now `asas_access.seeding`; it shadowed nothing exported, but carried the same trap. The seeding entry points (`seed_field_permissions`, `seed_action_permissions`, `ensure_system_groups`, `ensure_clearance_levels`) are unchanged.
- **Adoption is now shape-verified.** `migrate()` previously decided adopt-vs-create on the sentinel table's *name* alone, so a host that already owned a table of that name had the baseline stamped as applied and skipped entirely — silently, and unrepairable by re-running. It now requires every baseline table to be present and the sentinel to carry the baseline's columns, and raises with the table, the package and the remedy otherwise (Teamy TEAMY-795).
- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-access @ v0.15.0` did not install
`asas-access` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
