# Changelog — `asas-notifications`

Versions follow semver, and the git tag matches this file: `asas-notifications/v0.12.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.13.0 — 2026-08-26

- **Feed pagination moved into SQL.** `GET /me/notifications` previously fetched every matching row and sliced the page in Python; it now pages via the new `service.list_feed()` (`COUNT` + `LIMIT`/`OFFSET`; also callable directly by host jobs). `unread_count()` likewise counts in SQL. Response shape is unchanged; `total` and the page are separate statements, so a concurrent commit can transiently skew them by a row — the standard trade for SQL pagination.
- **Org scoping as defense in depth.** When the configured context resolver supplies an org, every feed/read/archive query and per-row ownership check now constrains `org_id` in addition to `user_id` (a cross-org id probe 404s exactly like a missing row); all sites share one `_recipient_conditions` chokepoint. Coalescing now *requires* an org context — an org-less emit (e.g. a background job in a host that stamps `org_id` via its own ORM listener) inserts a fresh row instead of merging, since the lookup cannot tell orgs apart. Resolvers are consulted on read paths too and must return `None` cheaply outside a request. Hosts without a resolver keep user-only scoping; host-level tenancy remains the first line.
- **`mark_all_read()` / `archive_read()`** each issue one bulk `UPDATE` instead of loading every row and flushing per-row updates.
- **Composite indexes for the hot scans** (migration `0003`): `(user_id, archived_at, created_at, id)` for the feed (id as the ORDER BY tiebreaker), `(user_id, read_at, archived_at)` for the badge, `(status, claimed_at)` for the dispatcher; the single-column `user_id` and `status` indexes they subsume are dropped. Every create/drop is guarded by an existence check (adopting hosts may have differently-named historical indexes), and on Postgres the builds run `CONCURRENTLY` so a boot-time `migrate()` never blocks writes to a live table.

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
