# You are reading a reference, not a starting point

This is a **conformance harness that reads as an example**. Its suite runs in CI
against the packages by local path, so it cannot drift from them — that is the
only reason anything here is trustworthy.

It is **not** a scaffold, a template, or a starter app. If you are adopting Asas
you almost certainly have an application already; the useful move is to copy one
wiring module at a time into it, not to start from this tree.

## Read in this order

1. `app/main.py` — the boot sequence. The numbering is the contract, and the
   sequence is the part that is genuinely hard to reconstruct from ten package
   READMEs.
2. `app/wiring/<package>.py` — one module per package. Each names the contract
   row it demonstrates and the one trap that package has.
3. `app/routers/tickets.py` — the call order a write path follows.

## File → contract row

| File | Contract row / composition |
| --- | --- |
| `app/db.py` | Schema — the host's own, before any package `migrate()` |
| `app/main.py` | The boot sequence; Routers, applied with the host's guards |
| `app/fake_auth.py` | The auth **composition seam** (not auth) |
| `app/wiring/lookups.py` | Routers, Schema, Seeding, Host hooks — all four |
| `app/wiring/access.py` | Schema, Seeding — field/action policy as data |
| `app/wiring/validation.py` | Routers — table-less, rules as code-declared data |
| `app/wiring/storage.py` | Host hooks — `configure`, and its ordering |
| `app/wiring/ratelimit.py` | Host hooks — `configure` |
| `app/wiring/jobs.py` | Schema, Seeding, Host hooks + **async-notification composition** |
| `app/wiring/workflow.py` | Schema, Seeding + **escalation composition** |
| `app/wiring/notifications.py` | Routers, Schema, Host hooks + recipient filtering |
| `app/wiring/search.py` | Schema + **classified-record composition**; dialect dispatch |
| `app/wiring/mcp.py` | Routers — `build_mcp_app`, an ASGI app |

**A file that cannot name its row does not belong here.** That rule is the
guardrail against the real failure mode, which is not under-building — it is
growing a second Teamy.

## Do not copy

- **`app/fake_auth.py`.** It is a static token map with no secret, no expiry and
  no hashing. It exists to show the *seam*, and it refuses to arm without
  `ENABLE_FAKE_AUTH=1`. Authentication is deliberately not an Asas package.

  Note what "fails closed" does and does not mean here: without the flag there
  is no identity, so `require_user` admits nobody and the **admin surfaces are
  unreachable** — the read surface and the ticket routes stay open. An earlier
  version returned `None` in that branch, which made the guard a pass-through
  and left the lookup admin router's state-changing routes anonymous. A guard
  that demonstrates nothing is worse than no guard in a file people read to
  learn the seam.
- **`create_all` in `app/db.py`** — copy the `tables=` argument, not the
  approach. A real host owns an Alembic chain.

## Traps this host hit while being written

Each is documented at the point of use; they are collected here because they are
the things most likely to cost you an afternoon.

- **`SQLModel.metadata` is process-global.** Importing any Asas package
  registers its tables into it, so a bare `SQLModel.metadata.create_all(bind)`
  creates *their* tables and the package's own `migrate()` then fails. Always
  pass `tables=`. (`app/db.py`)
- **`redact_view` uses `hasattr`/`setattr`.** Hand it a plain `dict` and it
  silently redacts nothing — no error, and the restricted field goes to the
  client. Project to an object. (`app/routers/tickets.py`)
- **Validate the effective record, not the payload.** A rule is skipped when a
  value it reads is null, and defaults are not in the payload — so on a create
  path, construct first and validate against what the row will hold.
  (`app/routers/tickets.py`)
- **A workflow `end` node must carry `config["outcome"]`.** The engine reads it
  unguarded; omitting it raises `KeyError` from inside the engine.
  (`app/wiring/workflow.py`)
- **A completion callback runs inside the engine's transaction.** Committing in
  one discards the rollback-on-failure guarantee it exists to provide.
  (`app/wiring/workflow.py`)
- **Idempotence must be designed.** Delivery is at-least-once, and a handler
  that "only reads and notifies" is not idempotent. (`app/wiring/jobs.py`)
- **`notify(record=...)` is what runs the recipient filter.** Omit it and a
  classified record's title reaches an inbox with no error.
  (`app/wiring/notifications.py`)
- **`ensure_type(session, **kwargs)`** forwards straight to the model, so its
  accepted names are invisible in the signature. The identifier is `key`, not
  `code`. (`app/wiring/lookups.py`)
- **A search extractor takes a *session*, not a record.** `fts.rebuild` calls
  `extractor(session)` and expects every document for that source. The
  per-record shape the name suggests fails only when a rebuild actually runs.
  (`app/wiring/search.py`)
- **Registering a deep-search provider indexes nothing.** You also need a write
  listener and a backfill, or the tier is registered and permanently inert —
  and every *negative* search assertion still passes. (`app/wiring/search.py`)
- **`org_of` returning `None` is a filter, not "no filter".** It matches only
  documents written with a `None` org, so pairing it with real `org_id`s
  silently returns nothing. (`app/wiring/search.py`)
- **`MCPToolDef` takes flat `read_only` / `destructive` / `idempotent`**, not an
  `annotations` dict. (`app/wiring/mcp.py`)
- **`build_mcp_app` without `token_verifier` mounts with no authentication.**
  (`app/wiring/mcp.py`)

## Running it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Zero configuration is a hard constraint here, not an aspiration — `test_boot.py`
enforces it. Swagger is at `/docs`; there is no frontend, by design.

```bash
pytest tests/ -q                                    # SQLite
TEST_DATABASE_URL=postgresql+psycopg2://... pytest   # Postgres, deep search on
```

## Checking your own host

`../selfcheck/asas_selfcheck.py` runs against **your** application and reports
what your wiring is missing:

```bash
python ../selfcheck/asas_selfcheck.py --app myapp.main:app --engine myapp.db:engine
```

Every finding names the exact call that fixes it. Start there rather than
reading this tree top to bottom.
