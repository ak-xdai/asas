# Releasing Asas

How Asas packages are versioned, released, and kept current in the applications
that use them. It covers both sides: what a **maintainer** does to ship a change,
and what a **consuming application** owes in return.

## The strategy in brief

1. **Every package versions on its own.** Tags look like `asas-validation/v0.12.0`.
2. **Every change to shipped code is a release.** CI refuses a package change
   that does not bump the version, so `main` is always release-ready.
3. **Cutting the release is one command:** `asas release <package>`.
4. **Pins are frozen.** Nothing reaches an application until it bumps its pin,
   so no fix ever arrives as a surprise.
5. **Not every release demands a refresh.** Patches are optional, minors are a
   deliberate upgrade, and an **advisory** is the one mandatory refresh.
   `asas outdated --ci` in the consumer's pipeline enforces exactly that.

## 1. What a version means

### One tag per package

Each package versions **independently** and its git tag carries the package name:

```text
asas-lookups/v0.11.0
asas-storage/v0.15.0
```

A pin therefore says exactly what it installs:

```text
asas-lookups @ git+https://github.com/wlootah-a11y/asas.git@asas-lookups/v0.11.0#subdirectory=packages/asas-lookups
```

### Which number moves

Pre-1.0, semver shifts one place to the right:

| Change | Bump | Examples |
|---|---|---|
| **Breaking** — a host must change code, config, or data to keep working | **minor** (`0.12.3` → `0.13.0`) | removing or renaming a public name; a required new argument; changing an error or violation code a host matches on; a migration that needs host action |
| **Additive** — nothing existing changes | patch (`0.12.3` → `0.12.4`) | a new check, function, or optional keyword; a new table `migrate()` creates idempotently |
| **Fix** — behavior brought back in line with the documented contract | patch | a crash on blank input; a wrong result |

When a fix changes behavior a host could reasonably depend on, treat it as
breaking. The version number is a promise about what an upgrade costs, and
under-promising is cheaper than a broken consumer.

### One version, three places

The version in `pyproject.toml`, `__version__` in `__init__.py`, and the newest
heading in the package's `CHANGELOG.md` must agree.
`tests/test_version_consistency.py` enforces it, so a half-finished bump fails
CI rather than shipping.

## 2. What a consuming application owes: the update tiers

A pin is frozen by design. The cost of that safety is that staying current is a
deliberate act, so the obligation is tiered rather than "always refresh":

| Where the pin stands | What it means | What the application must do |
|---|---|---|
| **current** | on the newest tag | nothing |
| **patch behind** (`0.12.0`, latest `0.12.3`) | only fixes and additions are waiting | nothing required; refresh when convenient |
| **minor behind** (`0.12.x`, latest `0.13.0`) | breaking changes are waiting | a deliberate upgrade: read the CHANGELOG, adapt, bump the pin |
| **advisory-affected** (pin listed in `ADVISORIES.json`) | a security or integrity problem is fixed upstream | **refresh now**, whatever the tier |

### Checking it

`asas outdated` reads the project's `pyproject.toml`, compares each Asas pin
with the newest tag, and checks the advisory list:

```text
$ asas outdated
ok       asas-lookups            v0.13.2     latest v0.13.2
patch    asas-storage            v0.15.0     latest v0.15.1
MINOR    asas-validation         v0.11.0     latest v0.12.0
```

### Enforcing it in the application's CI

```bash
asas outdated --ci
```

| Exit | Meaning | Suggested CI handling |
|---|---|---|
| `0` | current or patch behind | pass |
| `1` | at least one pin is a minor behind | fail, or mark as a warning if the team schedules upgrades |
| `2` | at least one pin is affected by an advisory | fail, always |

Without `--ci` the command only reports a minor-behind pin, but still exits `2`
on an advisory, so even a manual run cannot miss one. If the advisory file
cannot be fetched (no network in CI, for example), the command prints a warning
and skips that check. A network hiccup never fails a build on its own.

## 3. Advisories: the mandatory refresh

### When to publish one

Publish an advisory when staying on an affected version is dangerous, not
merely inconvenient:

- a **security** problem: an authorization bypass, data exposure, injection;
- a **data integrity** problem: silent loss or corruption, or an audit chain
  reporting tampering that did not happen;
- a check that **silently passes** input it should reject.

A crash that fails loudly, a poor error message, or a missing feature is not an
advisory. Those ride the normal tiers.

### How to publish one

`ADVISORIES.json` at the repository root is the whole mechanism. Add an entry
in an ordinary reviewed PR, after the fixing release is tagged:

```json
{
  "advisories": [
    {
      "package": "asas-audit",
      "fixed_in": "v0.3.1",
      "note": "2026-09-20: naive datetimes could break the hash chain; take v0.3.1"
    }
  ]
}
```

Every pin **below** `fixed_in` counts as affected. That is deliberately
conservative: it is cheaper for an unaffected consumer to refresh than for an
affected one to be missed. The moment the entry lands on `main`, every
application running `asas outdated --ci` on an older pin fails until it
upgrades.

Never delete an advisory. An application restored from an old branch still
needs to be told.

## 4. Cutting a release (maintainers)

1. **In the change's own branch, alongside the code**, for each changed package:
   - bump `version` in `pyproject.toml` **and** `__version__` in `__init__.py`,
     choosing the number with the table in section 1;
   - add a `CHANGELOG.md` entry: what a *consumer* must do differently, not a
     commit list. Breaking changes first;
   - bump the package's entry in `asas-cli`'s `FALLBACK_TAGS`
     (`packages/asas-cli/src/asas_cli/git_tags.py`), the offline pin that
     `asas add` and `asas new` fall back to. (Library packages only: `asas-cli`
     itself has no entry.)
2. **Merge to `main`.** The bump lands **with** the code, so the commit you tag
   is the commit that describes itself.
3. **Tag from `main`:**
   ```bash
   git checkout main && git pull
   asas release validation        # or: asas release asas-validation
   ```
   The command checks that the three version declarations agree, refuses a tag
   that already exists locally or on `origin`, then creates and pushes the
   annotated tag `asas-validation/vX.Y.Z`. Use `--no-push` to create it locally
   only.
4. **If the release fixes something in section 3, publish the advisory.**
5. **Consumers pick it up** through `asas outdated`: a patch whenever they like,
   a minor as a planned upgrade, an advisory immediately. Known consumers
   (Teamy's `backend/requirements.txt`) bump their pins in their own reviewed PR.

**Never tag from a feature branch.** A tag is a promise that the code is on
`main`; tagging early strands consumers on a commit that may never merge.

## 5. What is enforced, and where

| Rule | Enforced by | Fails when |
|---|---|---|
| A shipped change is a release | CI job `release-discipline` (`scripts/check_version_bumps.py`) | a PR changes a package's `src/**` or `pyproject.toml` but leaves its version unchanged. Tests, README, and CHANGELOG-only edits are exempt, as is a brand-new package |
| One version, three places | `tests/test_version_consistency.py` (CI job `repo`) | `pyproject.toml`, `__version__`, and the newest CHANGELOG heading disagree |
| The CLI knows every release | `tests/test_version_consistency.py` | `FALLBACK_TAGS` lags a package's version, or the CLI registry misses a package |
| A tag matches its contents | `asas release` | the three declarations disagree, or the tag already exists |
| Applications stay safe | `asas outdated --ci`, in the application's own CI | a pin is affected by an advisory (exit 2) or a minor behind (exit 1) |

Run the version-bump check locally before pushing:

```bash
python scripts/check_version_bumps.py origin/main
```

**Known gap:** nothing yet fails when a version bump merges but nobody runs
`asas release`. The release is invisible to `asas outdated` until the tag exists,
so step 3 still depends on the maintainer who merged.

## Support window

The **current minor of each package** receives fixes. There is no long-term
support branch: with two consumers and a shared owner, the supported answer to a
bug is to take the next patch, not to backport.

A consequence for advisories: the fix ships on the current minor, so an
application two minors behind has to take the breaking upgrade to clear the
advisory. That pressure is intended, since it keeps applications from drifting
far behind.

If that stops being workable (a consumer pinned to an older minor who cannot
upgrade), the answer is a maintenance branch per package, and it needs deciding
before it is needed rather than during an incident.

## Consumers who mirror this repo

A host on a closed network may mirror this repository internally and rewrite the
origin with `url.insteadOf`. Tags must travel with that mirror: `git push
--mirror`, or a refresh that copies branches only, leaves every pin unresolvable
and the failure appears at `pip install` rather than at push time.

A release is not delivered to such a consumer until their mirror carries the new
tags. Such a consumer should also mirror `ADVISORIES.json`, or `asas outdated`
will warn that it cannot fetch the list and skip the advisory check.

## Why not lockstep

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
