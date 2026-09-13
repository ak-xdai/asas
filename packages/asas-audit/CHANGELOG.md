# Changelog — `asas-audit`

Versions follow semver, and the git tag matches this file: `asas-audit/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 — unreleased

First release. Extracted from a host application's working implementation, so the
shapes below survived production rather than being a first guess at them.

- **`append(session, ...)`** records one business action on the caller's own
  session and does not commit, so the entry and the change it describes share one
  transaction and one fate. Two appends in one unit of work chain to each other
  (the tail read is flushed first) rather than both chaining to the row that
  preceded them.
- **The hash chain** (`chain.py`, pure and DB-free): `canonical_bytes`,
  `chain_payload`, `compute_hash`, `verify_rows`. Writer and verifier share one
  encoding and live in one file, because two encodings is how a chain reports
  breaks that are not there. `verify` detects edits, deletions, insertions and
  reordering, and reports each with both hashes plus a plain-words reason.
- **`canonical_timestamp`**, found by the dual-engine rule: Postgres returns an
  aware datetime and SQLite a naive one, so the same row hashed one way on write
  and another on verify, and every chain read back on SQLite reported as tampered
  with. A naive value is read as UTC, the only choice that does not make a row
  hash differently on two machines.
- **The per-tenant advisory lock, taken before the tail read.** `FOR UPDATE` on
  the tail row does not prevent the race: a waiter holding it from before the
  winner's insert chains off a stale fingerprint and forks the chain, silently.
  Asserted with eight simultaneous writers, and the failure is demonstrated too:
  with the lock disabled the same writers fork the chain on every run.
- **Append-only enforced by the database**: a trigger rejects UPDATE and DELETE
  for anyone, which is the layer that survives a compromised application.
  Postgres only; `TRUNCATE` is deliberately out of scope and says why.
- **Row-level security through `asas-tenancy`**, so this table and the host's own
  are protected by one definition of the policy. First inter-package dependency in
  the family, declared by name rather than by git URL.
- **`history(...)`** newest first, filtered by resource, actor, action and date
  range. A `resource_id` without its `resource_type` is refused: an id is unique
  only within a type.
- **`build_router`** serves the history and the verification report, with the
  fingerprints as hex so a reader can check the chain themselves. Auth is
  composition-time, the tenant is never a request parameter, and there is no write
  route.
- Identity is opaque strings throughout, so a UUID-keyed host and an
  integer-keyed one can both adopt this without a fork. `seq` uses
  `BigInteger().with_variant(Integer, "sqlite")`, because SQLite auto-increments
  only a plain `INTEGER PRIMARY KEY` and a bigint one fails its NOT NULL on every
  insert.
- Documents the `expire_on_commit=False` requirement: with the default, reading a
  row after commit refreshes it in a new transaction with no tenant pinned, and
  the policy makes the row vanish. Pinned as a test.
- Licensed **Apache 2.0**, with `LICENSE` and `NOTICE` in the wheel.
- `tests/test_host_contract.py` per TEAMY-798, plus a test that this package
  exposes no `seed` and no write route.
