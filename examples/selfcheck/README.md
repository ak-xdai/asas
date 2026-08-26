# Host self-check

Run this against **your own** application to find out whether it wires the Asas
packages correctly. It checks two rows of the [host contract](../../README.md#the-host-contract):
*Schema* (`migrate`) and *Host hooks* (`configure_*`).

It exists because both failure modes are silent. A forgotten `migrate()` looks
fine until the first query hits a missing column, and every `configure_*` hook
defaults to single-tenant/no-op, so a host that forgets one gets quieter
behaviour rather than an error.

## Run it

```bash
python asas_selfcheck.py --app myapp.main:app --engine myapp.db:engine
```

`--app` takes the same `module:attr` your `uvicorn` command does, and the
working directory is put on `sys.path` the same way. Pass it whenever you can:
hosts usually install their hooks inside the FastAPI lifespan, so the tool runs
that lifespan before inspecting. Without `--app` every hook reads as missing.

For schema-only checks, `--database-url` works instead of `--engine`. Add `-v`
to print passing checks too.

Exit status is 0 when nothing failed, 1 otherwise.

## Reading the output

| | Meaning |
| --- | --- |
| `FAIL` | Broken or about to be. Every one carries a `fix:` line naming the call that resolves it. |
| `warn` | A legitimate configuration that is more often an oversight — most commonly a tenancy hook a single-tenant host does not need. The message states what the default actually does; decide and move on. |
| `ok` | Checked and correct. Hidden unless `-v`. |

Warnings never affect exit status. Only the host knows whether single-tenant is
the intent, so the tool reports rather than rules.

## From pytest

No plugin. The core is a pure function:

```python
from asas_selfcheck import run_checks

def test_asas_wiring(engine):
    report = run_checks(engine)
    assert not report.failures, report.format()
```

Note that this observes whatever state your test process is in — if your hooks
are installed in the lifespan, the fixture has to have started it.

## Scope

It asserts things about a **host's integration**, never about the packages —
that is what each package's own `tests/test_host_contract.py` is for. It also
cannot tell you whether your handlers, policies, or seeds are *correct*; it
tells you they are wired.

Packages with neither schema nor hooks (`asas-validation`, `asas-mcp`) are
absent from the registry on purpose: there is no host-side state to get wrong,
and a check that can never fail trains people to skim.
