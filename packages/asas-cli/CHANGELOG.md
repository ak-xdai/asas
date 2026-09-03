# Changelog — `asas-cli`

Versions follow semver, and the git tag matches this file: `asas-cli/v0.1.0`.
Pre-1.0, a breaking change bumps the **minor**.

Release procedure and the historical tag mapping: [`RELEASING.md`](../../RELEASING.md).

## 0.1.0 — 2026-08-27

- Initial release: `asas add <package>` pins one Asas package into an existing
  project's `pyproject.toml`; `asas new <name> --with <packages>` scaffolds a
  new FastAPI project with a working boot sequence pre-wired for the chosen
  packages; `asas list` enumerates known packages.
- Born after the per-package tag scheme (`RELEASING.md`) landed, so it resolves
  and pins each selected package's own `asas-<pkg>/vX.Y.Z` tag — never a single
  shared tag across a multi-package `asas new --with a,b,c`.
- Licensed under **Apache 2.0**, matching every other package in this repo.
