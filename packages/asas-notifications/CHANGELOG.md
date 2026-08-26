# Changelog — `asas-notifications`

Versions follow semver, and the git tag matches this file: `asas-notifications/v0.12.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.13.0 — 2026-08-26

- **Feed pagination moved into SQL.** `GET /me/notifications` previously fetched every matching row and sliced the page in Python; it now issues `COUNT` + `LIMIT`/`OFFSET`. `unread_count()` likewise counts in SQL instead of fetching ids. Response shape is unchanged.
- **Org scoping as defense in depth.** When the configured context resolver supplies an org, every feed/read/archive query and per-row ownership check now constrains `org_id` in addition to `user_id` (a cross-org id probe 404s exactly like a missing row), and coalescing never folds an event into another org's row. Hosts without a resolver (or outside a request) are unchanged; host-level tenancy remains the first line.
- **Composite indexes for the hot scans** (migration `0003`, index-only DDL): `(user_id, archived_at, created_at)` for the feed, `(user_id, read_at, archived_at)` for the badge, `(status, claimed_at)` for the dispatcher. The single-column `user_id` and `status` indexes they subsume are dropped.

## 0.12.0 — 2026-08-25

- **Adoption is now shape-verified.** `migrate()` previously decided adopt-vs-create on the sentinel table's *name* alone, so a host that already owned a table of that name had the baseline stamped as applied and skipped entirely — silently, and unrepairable by re-running. It now requires every baseline table to be present and the sentinel to carry the baseline's columns, and raises with the table, the package and the remedy otherwise (Teamy TEAMY-795).
- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-notifications @ v0.15.0` did not install
`asas-notifications` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
