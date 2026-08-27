# asas-cli

The developer on-ramp to the Asas package collection. Not a runtime framework —
it never wraps or wires anything at import time in a consuming project. Two
one-shot code generators:

```
asas add lookups                          # pin one package into an existing project
asas new myservice --with lookups,access  # scaffold a new project wired for a set
asas list                                 # see every known package + its variant
```

## Installing

### Once this is merged and tagged

Pin `asas-cli` itself the same way you'd pin any other Asas package (see the
repo README's "Consuming" section) — its own tag, `asas-cli/vX.Y.Z`, or
globally with `pipx`:

```bash
pipx install "asas-cli @ git+https://github.com/wlootah-a11y/asas.git@asas-cli/v0.1.0#subdirectory=packages/asas-cli"
```

### Right now — trying it locally, before it's published

`asas-cli` isn't reachable via a git URL until this merges to `main` and a
new tag is cut. Until then, install it straight from your clone:

```bash
git clone https://github.com/wlootah-a11y/asas.git   # or `git pull` if you already have it
cd asas
git checkout example/helpdesk                         # this branch, until it's merged

python3 --version   # needs >= 3.11 — every Asas package requires it. If this
                     # prints something older, point the next line at a newer
                     # interpreter you have (`python3.12 -m venv .venv`, etc.)
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip   # editable installs need pip >= 21.3
                                       # (PEP 660) — an older pip fails with
                                       # "editable mode currently requires a
                                       # setuptools-based build"
pip install -e 'packages/asas-cli[dev]'

asas --help
asas list
```

**`asas add`/`asas new` still work fully today** — only `asas-cli` itself is
local-only pre-merge. Every *other* package (`lookups`, `ratelimit`, …) is
already tagged and published on GitHub, so a real end-to-end smoke test
works right now:

```bash
asas new asas-smoke-test --with ratelimit,lookups --dir /tmp
cd /tmp/asas-smoke-test && pip install -e '.[dev]' && pytest -q
```

Running the CLI's own test suite:

```bash
cd packages/asas-cli
pytest -q
```

## `asas add <package>`

Writes the correct `git+https://...#subdirectory=...` line into your
project's `pyproject.toml` `[project.dependencies]` — pinned to that
package's own latest tag by default (`--version` to override). Packages
version **independently** since [`RELEASING.md`](../../RELEASING.md)
(2026-08-25): the tag is `asas-<pkg>/vX.Y.Z`, so pinning two packages never
shares one version number. Idempotent: running it again for the same
package updates the pin in place instead of duplicating it. Accepts either
the short key (`lookups`) or the full dist name (`asas-lookups`).

```
asas add ratelimit --version 0.11.0 --path ./services/api/pyproject.toml
```

## `asas new <name> --with <keys>`

Scaffolds `<name>/main.py`, `settings.py`, `pyproject.toml`, `README.md`,
`.env.example`, and `tests/test_smoke.py` (the generated project's own
boots-with-zero-edits check), wired for whichever packages you list. The generated
`main.py` is **plain, editable Python** — the same manual `migrate` →
`seed` → `build_routers` → `include_router` sequence you'd write by hand
following each package's host contract, just typed for you. It is not a
runtime abstraction: nothing in a consuming project depends on `asas-cli`
after generation, and re-running `asas new` never edits a file you've
already touched — it only ever starts a fresh project directory.

Every non-comment line it generates is a real call against the package's
actual API. Lines needing data or logic only the host can supply (policy
grants, workflow specs, tool implementations, …) are left as `# TODO`
comments naming the exact function to call — never a fabricated call.

```
asas new myservice --with lookups,ratelimit,access
cd myservice && pip install -e '.[dev]' && uvicorn main:app --reload
```

## Where this fits

See the repo README's "host contract" — every package's real behavior is
defined there and enforced by that package's own tests, not by this CLI.
`asas_cli.registry` (install metadata) and `asas_cli.templates` (boot
snippets) are this tool's description of that contract for scaffolding
purposes; update both when a package's public surface changes.
