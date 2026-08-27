# Reference host — a helpdesk on Asas

A small FastAPI application wired to all ten Asas packages, and the executable
definition of "the host contract holds". Its suite runs in CI on both engines,
against the packages **by local path** — so a contract break fails here on the
pull request that caused it.

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload        # works with an empty environment
```

Swagger at `/docs`. There is no frontend, by design.

If you are an agent working in this tree, read [`CLAUDE.md`](CLAUDE.md) first —
it carries the file→contract map and the traps.

## What it demonstrates

Every **seam** and every **composition**. No function for its own sake: the ten
packages export 218 public names, and an app that touched each once would be a
worse copy of the test suites and the version that rots fastest. Breadth belongs
to `__all__` and the package suites; depth belongs here.

The three compositions — features that exist *only* when packages combine, and
that no per-package README is structurally able to teach:

| Composition | Packages | Where |
| --- | --- | --- |
| Escalation approval | workflow + access + notifications | `app/wiring/workflow.py` |
| A classified record | access (MAC) + search | `app/wiring/search.py` |
| An async notification | jobs + notifications | `app/wiring/jobs.py` |

## Adoption sequence

**You do not need to adopt all ten, or adopt them in this order.** The packages
have zero dependencies on each other, so each step below is independently
useful and independently reversible. This is the order that pays off soonest for
a typical application, not a required sequence.

Every step is *one wiring module plus one line in your lifespan*. That is what
the `app/wiring/` layout is for — an adopting host's real question is "what is
the smallest diff that adds this to the app I already have?", and a monolithic
`main.py` answers a question nobody is asking.

1. **`asas-lookups`** — reference data. Start here: it is the only package that
   exercises all four contract rows, so it teaches the shape of the rest.
   Remember `seed()` ships standards-based vocabulary only; your product's own
   words are yours to seed.
2. **`asas-validation`** — table-less, no migration, no seeding. The cheapest
   possible adoption, and it gives you a rules endpoint your frontend can mirror.
3. **`asas-storage`** — one `configure()` call. Note the ordering: before
   anything that might store a byte, which is usually a seed, not a request.
4. **`asas-ratelimit`** — one `configure()`, then a `check()` at the endpoints
   that write rows or call paid APIs.
5. **`asas-access`** — the first one with real policy. Field and action
   permissions become *rows*, not branches. Budget time for deciding your
   principals; the code is the easy half.
6. **`asas-notifications`** — needs a context resolver if you are multi-tenant,
   and a recipient filter if any record is restricted.
7. **`asas-jobs`** — replaces every `BackgroundTasks` and boot sweep you have.
   Handlers must be idempotent; see `app/wiring/jobs.py` for why that is not
   automatic.
8. **`asas-workflow`** — approvals. Definitions are data, seeded once; editing
   the spec and restarting does not change an already-seeded definition.
9. **`asas-search`** — portable on every engine, ranked on Postgres. Never index
   a restricted field.
10. **`asas-mcp`** — last, because an MCP tool should be a thin allowlist over
    capability you already have.

## Brownfield

The case that actually matters, because a greenfield database can only take the
happy path. `migrate()` is adopt-or-create: finding its tables present and no
version table, it concludes your history created them and stamps the baseline.

That is irreversible in effect, so it is shape-verified first. If you own a
table called `notification` — not an exotic name — you get a loud, specific
error rather than a silently skipped baseline. `tests/test_brownfield.py` is
that guard, demonstrated.

## Tiers

The core tier boots with **nothing configured**, on SQLite:

> lookups · access · validation · jobs · workflow · notifications · storage
> (local) · ratelimit

The optional tier is env-gated and reports itself off at `/health`:

| Package | Needs | Without it |
| --- | --- | --- |
| `asas-search` deep tier | Postgres | portable `ilike` search, same contract |
| `asas-mcp` | `MCP_TOKEN` | endpoint absent, not open |
| `asas-storage` s3/azure | `STORAGE_*` | local disk |

Graceful degradation is a property Asas claims everywhere; this is where it is
shown working. A host that degrades *silently* is indistinguishable from one
that is broken, so `/health` names the tier each package is in.

## Auth

Not an Asas package, deliberately — it is the one concern where every host
differs and a shared implementation would be actively harmful.

What this host demonstrates is the **composition seam**: a host
`get_current_user`, guards applied at `include_router` time, and
`configure_org_resolver`. `app/fake_auth.py` is a static token map that refuses
to start without `ENABLE_FAKE_AUTH=1`. It is ugly on purpose. Do not copy it.

## Tests

```bash
pytest tests/ -q                                          # SQLite
TEST_DATABASE_URL=postgresql+psycopg2://... pytest tests/ # Postgres
```

- `test_boot.py` — zero-config boot, every chain at head, tier visibility
- `test_compositions.py` — the three compositions, end to end
- `test_brownfield.py` — adoption guards

To check **your own** host instead, use the sibling tool:

```bash
python ../selfcheck/asas_selfcheck.py --app myapp.main:app --engine myapp.db:engine
```
