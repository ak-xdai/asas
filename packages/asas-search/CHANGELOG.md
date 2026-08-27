# Changelog — `asas-search`

Versions follow semver, and the git tag matches this file: `asas-search/v0.11.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.12.0 — 2026-08-27

- Documented three things that made a deep-search tier easy to wire inertly: an extractor is called as `extractor(session)` and must yield **every** document for its source (not one record's); registering an extractor does **not** keep the index fresh — a host also needs a `rebuild()` backfill and ORM listeners calling `upsert`/`delete`; and `org_of` returning `None` is a *filter* matching only `None`-org documents, not the absence of one. Each failure mode leaves the index permanently empty with no error, and every *negative* search assertion still passes (Teamy TEAMY-809).

## 0.11.0 — 2026-08-25

- **Adoption is now shape-verified.** `migrate()` previously decided adopt-vs-create on the sentinel table's *name* alone, so a host that already owned a table of that name had the baseline stamped as applied and skipped entirely — silently, and unrepairable by re-running. It now requires every baseline table to be present and the sentinel to carry the baseline's columns, and raises with the table, the package and the remedy otherwise (Teamy TEAMY-795).
- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-search @ v0.15.0` did not install
`asas-search` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
