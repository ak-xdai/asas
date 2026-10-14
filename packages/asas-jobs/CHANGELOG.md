# Changelog — `asas-jobs`

Versions follow semver, and the git tag matches this file: `asas-jobs/v0.11.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.11.1 — 2026-10-02

- **Requires `sqlmodel<0.0.45`.** From 0.0.45, SQLModel's `DateTime` refuses a
  datetime without a timezone, and this package stores naive UTC, so every
  write of a timestamp failed on a fresh install. Nothing changes for a host
  already on 0.0.44 or earlier. Moving the columns to timezone-aware values,
  which lifts the cap, is a separate change.

## 0.11.0 — 2026-08-25

- **Adoption is now shape-verified.** `migrate()` previously decided adopt-vs-create on the sentinel table's *name* alone, so a host that already owned a table of that name had the baseline stamped as applied and skipped entirely — silently, and unrepairable by re-running. It now requires every baseline table to be present and the sentinel to carry the baseline's columns, and raises with the table, the package and the remedy otherwise (Teamy TEAMY-795).
- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-jobs @ v0.15.0` did not install
`asas-jobs` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
