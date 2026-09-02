# Releasing Asas

## Versioning: one tag per package

Each package versions **independently** and its git tag carries the package name:

```text
asas-lookups/v0.11.0
asas-storage/v0.15.0
```

A pin therefore says exactly what it installs:

```text
asas-lookups @ git+https://github.com/wlootah-a11y/asas.git@asas-lookups/v0.11.0#subdirectory=packages/asas-lookups
```

Pre-1.0, a **breaking change bumps the minor** (`0.11.0` → `0.12.0`); additive
changes and fixes bump the patch. The version in `pyproject.toml`, the
`__version__` in `__init__.py`, and the newest heading in the package's
`CHANGELOG.md` must agree — a repo-wide test enforces it, so a half-finished
bump fails CI rather than shipping.

### Why not lockstep

DR 0017 chose one version for the whole repo. It held for ten releases and then
decayed, because bumping ten packages to release one is friction nobody absorbs.
From `v0.11.0` the repo tag stopped matching any package's own version, and the
result was pins that could not be read:

- `asas-storage @ v0.15.0` installed storage **0.14.1**
- `asas-notifications @ v0.15.0` installed notifications **0.11.0**
- `asas-jobs` was **identical** at `v0.11.0`, `v0.12.0`, `v0.13.0`, `v0.14.0` and `v0.15.0`

A second consumer makes that untenable: a host pinned to a tag can tell neither
what code it holds nor what a fix would move it to. Per-package tags are not a
new policy so much as an admission of what the versions were already doing.

## Cutting a release

1. In the change's own branch, alongside the code:
   - bump `version` in `pyproject.toml` **and** `__version__` in `__init__.py` for
     each changed package (a repo-wide test fails if they disagree);
   - add a `CHANGELOG.md` entry per changed package — what a *consumer* must do
     differently, not a commit list. Breaking changes first;
   - bump the package's entry in `asas-cli`'s `FALLBACK_TAGS`
     (`packages/asas-cli/src/asas_cli/git_tags.py`) — the offline pin
     `asas add`/`asas new` fall back to. The same repo-wide test fails if it
     lags the `pyproject.toml` version. (Library packages only: `asas-cli`
     itself is not installable via `asas add` and has no entry.)
2. Land it on `main`. The bump and the changelog land **with** the code, so the
   commit you tag is the commit that describes itself.
3. Tag each changed package from that `main` revision:
   `git tag asas-lookups/v0.11.0 && git push origin asas-lookups/v0.11.0`
4. Bump the consuming pins (Teamy's `backend/requirements.txt`) in their own
   reviewed PR.

The version bump belongs in the same commit as the change, not a release commit
after it: tagging a `main` that does not yet carry the bump produces a tag whose
`asas-lookups/v0.11.0` name disagrees with the `0.10.3` inside it — the exact
defect this scheme exists to remove.

**Never tag from a feature branch.** A tag is a promise that the code is on
`main`; tagging early strands consumers on a commit that may never merge.

### Consumers who mirror this repo

A host on a closed network may mirror this repository internally and rewrite the
origin with `url.insteadOf`. Tags must travel with that mirror: `git push
--mirror`, or a refresh that copies branches only, leaves every pin unresolvable
and the failure appears at `pip install` rather than at push time.

A release is not delivered to such a consumer until their mirror carries the new
tags.

## Support window

The **current minor of each package** receives fixes. There is no long-term
support branch: with two consumers and a shared owner, the supported answer to a
bug is to take the next patch, not to backport.

If that stops being true — a consumer pinned to an older minor who cannot
upgrade — the change is a maintenance branch per package, and it needs deciding
before it is needed rather than during an incident.

## Historical tags (pre-2026-08-25)

Repo-wide tags under the retired lockstep scheme. Kept for decoding old pins;
do not create more.

| repo tag | lookups | validation | storage | ratelimit | jobs | access | workflow | notifications | search | mcp |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `v0.1.0` | 0.1.0 | — | — | — | — | — | — | — | — | — |
| `v0.2.0` | 0.2.0 | 0.2.0 | — | — | — | — | — | — | — | — |
| `v0.2.1` | 0.2.1 | 0.2.1 | — | — | — | — | — | — | — | — |
| `v0.3.0` | 0.3.0 | 0.3.0 | 0.3.0 | — | — | — | — | — | — | — |
| `v0.4.0` | 0.4.0 | 0.4.0 | 0.4.0 | 0.4.0 | — | — | — | — | — | — |
| `v0.5.0` | 0.5.0 | 0.5.0 | 0.5.0 | 0.5.0 | 0.5.0 | — | — | — | — | — |
| `v0.6.0` | 0.6.0 | 0.6.0 | 0.6.0 | 0.6.0 | 0.6.0 | 0.6.0 | — | — | — | — |
| `v0.7.0` | 0.7.0 | 0.7.0 | 0.7.0 | 0.7.0 | 0.7.0 | 0.7.0 | 0.7.0 | — | — | — |
| `v0.8.0` | 0.8.0 | 0.8.0 | 0.8.0 | 0.8.0 | 0.8.0 | 0.8.0 | 0.8.0 | 0.8.0 | — | — |
| `v0.9.0` | 0.9.0 | 0.9.0 | 0.9.0 | 0.9.0 | 0.9.0 | 0.9.0 | 0.9.0 | 0.9.0 | 0.9.0 | — |
| `v0.10.0` | 0.10.0 | 0.10.0 | 0.10.0 | 0.10.0 | 0.10.0 | 0.10.0 | 0.10.0 | 0.10.0 | 0.10.0 | 0.10.0 |
| `v0.10.1` | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 |
| `v0.11.0` | 0.10.2 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.11.0 | 0.10.1 | 0.10.1 | 0.10.2 | 0.10.1 |
| `v0.12.0` | 0.10.2 | 0.10.1 | 0.10.1 | 0.10.1 | 0.10.1 | 0.12.0 | 0.10.1 | 0.10.1 | 0.10.2 | 0.10.1 |
| `v0.13.0` | 0.10.2 | 0.10.1 | 0.13.0 | 0.10.1 | 0.10.1 | 0.12.0 | 0.10.1 | 0.10.1 | 0.10.2 | 0.10.1 |
| `v0.14.0` | 0.10.2 | 0.10.1 | 0.14.0 | 0.10.1 | 0.10.1 | 0.12.0 | 0.10.1 | 0.10.1 | 0.10.2 | 0.10.1 |
| `v0.15.0` | 0.10.2 | 0.10.1 | 0.14.1 | 0.10.1 | 0.10.1 | 0.12.0 | 0.10.1 | 0.11.0 | 0.10.2 | 0.10.1 |
