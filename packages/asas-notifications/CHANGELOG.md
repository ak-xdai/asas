# Changelog — `asas-notifications`

Versions follow semver, and the git tag matches this file: `asas-notifications/v0.13.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

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
