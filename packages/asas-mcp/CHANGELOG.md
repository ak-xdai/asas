# Changelog — `asas-mcp`

Versions follow semver, and the git tag matches this file: `asas-mcp/v0.11.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.12.0 — 2026-08-27

- `build_mcp_app` now logs a warning when called without `token_verifier`. Open access stays supported and unchanged — a socket nobody else can reach, or a gateway authenticating in front — but arriving at it by omission mounts a remote query API over the host's data with no login, which should not be silent (Teamy TEAMY-809).

## 0.11.0 — 2026-08-25

- Licensed under **Apache 2.0** (was proprietary/all-rights-reserved). `LICENSE` and `NOTICE` ship inside the wheel and the metadata carries `License-Expression: Apache-2.0` (Teamy TEAMY-797).
- Added `tests/test_host_contract.py`: `__all__` declared and resolving, contract names callable rather than shadowed by a submodule, module exports declared deliberately (Teamy TEAMY-798).

## Before 2026-08-25

Earlier releases were cut as **repo-wide** tags (`v0.1.0` … `v0.15.0`) under the
lockstep scheme in DR 0017, which decayed: from `v0.11.0` onward the repo tag no
longer matched any package's own version, so `asas-mcp @ v0.15.0` did not install
`asas-mcp` 0.15.0. `RELEASING.md` carries the full tag-to-version table for
decoding an old pin. Individual changes are in the git history.
