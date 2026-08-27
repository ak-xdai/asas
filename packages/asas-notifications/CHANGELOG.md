# Changelog — `asas-notifications`

Versions follow semver, and the git tag matches this file: `asas-notifications/v0.12.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.13.0 — 2026-08-27

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
