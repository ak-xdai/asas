# Changelog — `asas-notifications`

Versions follow semver, and the git tag matches this file: `asas-notifications/v0.15.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.16.0 — 2026-09-03

Implements DR 0003 (U-1 + U-2): action-referenced notifications and axis-based,
data-driven routing. **Breaking** (pre-1.0 minor); every rename below keeps a
one-release deprecated alias where the DR promises one.

- **`kind` is gone; emits reference the application action.** `notify()` takes
  `action=` — the app's own `entity.verb` id, a reference declared nowhere —
  plus the four axes `topic=` / `nature=` (was `category`) / `urgency=` /
  `reason=`. `action` is nullable: ad hoc emits carry axes only and never
  coalesce. Coalescing keys on (org, recipient, action, entity).
- **`register_kind()` is a deprecated shim** (one release): a registered kind
  supplies axis defaults for emits that pass its name, with `topic` defaulting
  to the seeded `general` topic. `notify(kind=...)`/`notify(category=...)`
  warn and map to `action=`/`nature=`.
- **Routing is data-driven** (migration `0004`): new deviation-only tables
  `notification_topic` and `notification_channel_policy` (platform rows +
  org override rows, DR 0001's shared-with-overrides pattern). Per channel,
  most specific wins: topic row → axis row → the built-in fallback, which is
  exactly the 0.15 rule — empty tables reproduce 0.15 routing bit-for-bit
  (equivalence-tested). `in_app` is an explicit channel: a policy that
  disables it suppresses the whole insert. Config reads are TTL-cached (60s;
  `config_cache_clear()` for tests/admin writes). Unknown topics fail loud.
- **Schema** (`0004`): `notification.kind` → `action` (nullable), `category`
  → `nature`, new `topic`, `data` (JSON presentation payload — same PII
  posture as `title`; latest wins on coalesce), `template` (reference stored
  now; rendering lands with U-4). Feed API: `NotificationRead` carries
  `action`/`topic`/`nature`/`template`/`data`; the `?category=` filter remains
  as a deprecated alias for `?nature=` for one release. `service.list_feed`
  filter kwarg is `nature=`.
- **`DeliveryPayload` renamed with the columns** (`action`, `nature`, plus new
  `topic`, `data`) — the payload's one rename; adapters remain
  render-consumers of a channel-agnostic payload.
- Review hardening: the unknown-topic error fires inside `suppressed()` too
  (suppression silences delivery, never catalog mistakes); topic validation
  re-queries fresh on a cache miss (cross-replica seeding lag is one extra
  SELECT, never a false LookupError); policy tie-breaks prefer the newest row;
  the `register_kind` shim covers only fully-legacy calls (any explicit axis
  ⇒ the new fail-loud contract) and warns at the emit site; coalesce folds
  refresh `topic`/`template` alongside `data`; `list_feed` keeps a deprecated
  `category=` alias; migration `0004` builds its `notification` indexes
  CONCURRENTLY on Postgres and its downgrade backfills NULL `action` rows;
  the adoption guard recognizes the post-rename schema instead of advising
  the destruction of real data.

## 0.15.0 — 2026-08-28

Re-lands the still-open parts of PR #20 (opened against 0.12.0; its emit-side
org fixes were superseded by 0.13.0's) on top of 0.14.0.

- **Feed pagination moved into SQL.** `GET /me/notifications` previously fetched
  every matching row and sliced the page in Python; it now pages via the new
  `service.list_feed()` (`COUNT` + `LIMIT`/`OFFSET`; also callable directly by
  host jobs). `unread_count()` likewise counts in SQL. Response shape is
  unchanged; `total` and the page are separate statements, so a concurrent
  commit can transiently skew them by a row — the standard trade for SQL
  pagination.
- **Org scoping as defense in depth on the read paths.** 0.13.0 fixed the emit
  side; now, when the configured context resolver supplies an org, every
  feed/read/archive query and per-row ownership check constrains `org_id` in
  addition to `user_id` (a cross-org id probe 404s exactly like a missing row);
  all sites share one `_recipient_conditions` chokepoint. Resolvers are
  consulted on read paths too and must return `None` cheaply outside a request.
  Hosts without a resolver keep user-only scoping; host-level tenancy remains
  the first line.
- **`mark_all_read()` / `archive_read()`** each issue one bulk `UPDATE` instead
  of loading every row and flushing per-row updates.
- **Composite indexes for the hot scans** (migration `0003`):
  `(user_id, org_id, archived_at, created_at, id)` for the feed (id as the
  ORDER BY tiebreaker), `(user_id, org_id, read_at, archived_at)` for the
  badge — `org_id` second so the org-scoped queries filter on the index while
  unscoped single-tenant queries still use the `user_id` prefix — and
  `(status, claimed_at)` for the dispatcher; the single-column `user_id` and
  `status` indexes they subsume are dropped. Every create/drop is guarded by an
  existence check (adopting hosts may have differently-named historical
  indexes), and on Postgres the builds run `CONCURRENTLY` so a boot-time
  `migrate()` never blocks writes to a live table; a name-matching INVALID
  index left by an interrupted concurrent build is detected via
  `pg_index.indisvalid` and dropped + rebuilt rather than silently kept.

## 0.14.0 — 2026-08-27

- **BREAKING: the recipient filter's signature gained `entity_id`.** It is now
  called as `fn(session, user_ids, entity_type, entity_id, record)`. **Action
  for hosts:** add the parameter to your filter.
- **The filter now runs for every `notify` that names an `entity_type`**, not
  only those that also passed `record=`. Filtering on `record is not None` let a
  producer skip the visibility check silently just by not having the row to
  hand — every named recipient was notified, including for a restricted subject,
  and a notification is a *copy*, so there is no redaction pass afterwards.
  `record` is still passed through when the producer has it and is `None`
  otherwise; the id is always passed so the filter can resolve the row itself.
  **Action for hosts:** make sure your filter tolerates `record=None` — an
  entity type that needs no filtering should return `user_ids` unchanged.
- Requiring `record=` at every call site was considered and rejected: a generic
  producer (a workflow-event bridge) legitimately holds only the type and the
  id and cannot load an arbitrary subject. Only the host knows which entity
  types need gating, so the decision belongs in the filter (Teamy TEAMY-807).

## 0.13.0 — 2026-08-27

- **Breaking: an emit with no org fails loud at the emit site** (issue #27,
  audit defect T-2). `Notification.org_id` is NOT NULL, but a `notify()` from a
  background job, CLI, or boot sweep — where the context resolver answers
  `None` — used to insert NULL and die as an engine-specific `IntegrityError`
  at flush, taking the producer's whole transaction with it. Stamping order is
  now: the new explicit `org_id=` parameter → the context resolver → a clear
  `ValueError` raised before any row is staged. Background producers acting
  *for* a tenant pass `org_id=` explicitly; hosts that relied solely on an ORM
  tenancy listener must pass it or configure the resolver.
- **Coalescing never crosses orgs** (defect T-6): the `coalesce_unread` merge
  identity now includes `org_id` — where hosts' entity ids are not globally
  unique, an org-2 emit can no longer fold into (and overwrite) an org-1 row
  for the same (recipient, kind, entity).

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
