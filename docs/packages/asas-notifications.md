# asas-notifications

> Reference page. Shape defined by [DR 0002](../design/0002-package-documentation.md).
> Version 0.11.1 · table-owning + router variant

## 1. What it is

A generic notification engine for hosts that need an in-app feed plus delivery
to external channels. Producers register event **kinds** and call `notify()`
inside their own transaction, so a notification exists if and only if the domain
change committed — the insert *is* the enqueue. The in-app feed is the
`notification` row itself, served by the package's router. Every other channel
(email, chat) goes through the `notification_delivery` outbox and a host-supplied
channel adapter. The package never imports host models: users, orgs and subject
records are plain integers and opaque objects.

## 2. Install

```
asas-notifications @ git+https://github.com/wlootah-a11y/asas.git@v0.11.1#subdirectory=packages/asas-notifications
```

```python
import asas_notifications as notifications
notifications.__version__  # "0.11.1"
```

## 3. Host contract

| Part | Status | Signature |
|---|---|---|
| Routers | **Implemented**, singular | `build_router(get_session) -> APIRouter` |
| `configure_*` hooks | **Implemented**, two | `configure_context_resolver(fn)`, `configure_recipient_filter(fn)` |
| `seed(session)` | **Not applicable** | The kind catalog is the host's and is registered in process via `register_kind()`, not seeded to a table. |
| `migrate(engine)` | **Implemented** | `migrate(engine) -> None` |
| Explicit `Session` | **Yes** | Every service function takes a `Session`. `dispatch_pending` takes an `engine`, because it manages its own short transactions per outbox row. |

Note the singular `build_router`, against `build_routers` elsewhere in the repo.
It returns one router, mounted at `/me/notifications`.

## 4. Wiring

Boot order matters: migrate before anything touches the tables, configure before
any `notify()` can run.

```python
from fastapi import Depends, FastAPI
import asas_notifications as notifications

app = FastAPI()

# 1. Tables (before the host's own Alembic chain).
notifications.migrate(engine)

# 2. Host context. Returns (user_id, org_id), or None outside a request.
def current_user_org(session) -> tuple[int, int] | None:
    return (ctx.user_id, ctx.org_id) if ctx else None

notifications.configure_context_resolver(current_user_org)

# 3. Visibility. Narrow recipients to those allowed to see `record`.
def visible_recipients(session, user_ids, entity_type, record):
    return [uid for uid in user_ids if can_view(session, uid, record)]

notifications.configure_recipient_filter(visible_recipients)

# 4. The kind catalog. Emitting an unregistered kind raises.
notifications.register_kind(
    "workflow.approval_requested",
    category=notifications.Category.action,
    urgency=notifications.Urgency.normal,
    reason=notifications.Reason.participant,
)

# 5. Channel adapters, one per external channel.
notifications.register_adapter("email", MyEmailAdapter())

# 6. The feed API. Auth is applied here, by the host.
app.include_router(
    notifications.build_router(get_session),
    dependencies=[Depends(require_user)],
)
```

## 5. Usage

**Emit, inside the producer's transaction.** No commit here: the notification
rides the caller's transaction and disappears with it on rollback.

```python
notifications.notify(
    session,
    recipient_user_ids,
    "workflow.approval_requested",
    title="Budget change",
    record=project,              # passed to the recipient filter
    entity_type="project",
    entity_id=project.id,
    actor_user_id=actor.id,      # excluded from recipients
)
```

**Drain the outbox.** The host owns the cadence: an after-commit hook for
latency, a boot sweep for crash recovery, a periodic job as the backstop.

```python
notifications.dispatch_pending(engine)          # default limit=100
```

**Suppress a fan-out** during a bulk import that reuses normal routers, then emit
one digest yourself:

```python
with notifications.suppressed():
    import_rows(session, rows)   # notify() is a no-op in here
```

**Read the feed** from the service module rather than over HTTP:

```python
from asas_notifications import service
service.unread_count(session, user_id)
service.archive_read(session, user_id)
```

## 6. API reference

### Functions

| Symbol | Signature | Notes |
|---|---|---|
| `notify` | `notify(session, recipients, kind, *, title, actor_user_id=None, body=None, link=None, entity_type=None, entity_id=None, record=None, category=None, urgency=None, reason=None, coalesce_unread=False, merge_body=None) -> list[Notification]` | Inserts notification and delivery rows in the caller's transaction. `category`/`urgency`/`reason` override the kind's registered defaults. Returns the rows created (empty if every recipient was filtered out or suppression is active). |
| `register_kind` | `register_kind(kind, *, category, urgency, reason) -> None` | Declares a kind's defaults. Emitting an unregistered kind fails loud. |
| `configure_context_resolver` | `configure_context_resolver(fn: Callable[[Session], tuple[int, int] \| None] \| None) -> None` | Supplies `(user_id, org_id)`. `None` outside a request. Pass `None` to reset. |
| `configure_recipient_filter` | `configure_recipient_filter(fn: Callable[[Session, Sequence[int], str, Any], Sequence[int]] \| None) -> None` | Narrows recipients to those allowed to see the record. Pass `None` to reset. |
| `register_adapter` | `register_adapter(channel: str, adapter: ChannelAdapter \| None) -> None` | Registers, or with `None` removes, the adapter for a channel. |
| `dispatch_pending` | `dispatch_pending(engine, *, limit: int = 100) -> int` | One outbox pass. Returns rows that reached a terminal-or-retried state. |
| `migrate` | `migrate(engine) -> None` | Applies the package Alembic chain. |
| `build_router` | `build_router(get_session) -> APIRouter` | Feed API, prefix `/me/notifications`, tag `notifications`. |
| `suppressed` | `suppressed()` — context manager | No-ops every `notify()` in scope. Unregistered kinds still raise. |

### Via the `service` module

`service` is exported as a module; these are not top-level names.

| Symbol | Signature | Notes |
|---|---|---|
| `service.unread_count` | `(session, user_id) -> int` | Unread **and un-archived**. |
| `service.mark_read` | `(session, user_id, notification_id) -> Notification \| None` | `None` if not found or not the recipient's. |
| `service.mark_all_read` | `(session, user_id) -> int` | Includes archived rows, so the badge can never be left non-zero. |
| `service.archive` | `(session, user_id, notification_id) -> Notification \| None` | Idempotent. |
| `service.unarchive` | `(session, user_id, notification_id) -> Notification \| None` | Read state untouched. |
| `service.archive_read` | `(session, user_id) -> int` | Archives read rows only. |
| `service.registered_kinds` | `() -> dict[str, KindSpec]` | The live catalog. |
| `service.current_user_id` | `(session) -> int \| None` | From the configured resolver. |
| `service.MAX_ATTEMPTS` | `int = 5` | Retry cap per delivery row. |
| `service.STALE_CLAIM_SECONDS` | `int = 300` | Age past which a `sending` claim is reclaimed. |

### Models

| Symbol | Notes |
|---|---|
| `Notification` | Table `notification`. See §7. |
| `NotificationDelivery` | Table `notification_delivery`. See §7. |

### Enums

| Symbol | Values |
|---|---|
| `Category` | `action`, `info`, `warning` — what it means for the recipient. |
| `Urgency` | `low`, `normal`, `high` — channel and display policy. |
| `Reason` | `requested`, `participant`, `watching` — why this recipient. |
| `DeliveryStatus` | `pending`, `sending`, `sent`, `failed`, `skipped`. |

### Protocols, payloads, exceptions

| Symbol | Notes |
|---|---|
| `ChannelAdapter` | `Protocol` with `send(payload: DeliveryPayload) -> None`. |
| `DeliveryPayload` | Frozen dataclass handed to adapters: `delivery_id`, `notification_id`, `channel`, `recipient_user_id`, `org_id`, `kind`, `category`, `urgency`, `reason`, `title`, `body`, `link`, `created_at`. Deliberately ORM-free — resolving a recipient's email address or Slack id is the adapter's job. |
| `LoggingAdapter` | Stub adapter that logs and succeeds. Collects sends in `.sent` for tests. |
| `SkipDelivery` | Raise from an adapter to mark the row `skipped`. Graceful, no retry. |
| `__version__` | `"0.11.1"` |

## 7. Data model

Two tables. Org and user references are plain `int` with no host foreign keys,
per the extraction rule. Enums are stored as `VARCHAR` (`native_enum=False`) for
dual-engine portability.

**`notification`** — the row *is* the in-app delivery.

`id` · `org_id` (indexed) · `user_id` (recipient, indexed) · `kind` (indexed) ·
`category` · `urgency` · `reason` · `entity_type` · `entity_id` (generic subject
reference, never an FK) · `title` · `body` · `link` · `read_at` · `archived_at` ·
`resolved_at` · `created_at`

`resolved_at` exists and **nothing writes it**. It was reserved for auto-clearing
`action` rows when the underlying task completes; Teamy evaluated that for
TEAMY-692 and chose the explicit archive gesture instead.

**`notification_delivery`** — outbox, one row per (notification, channel) the
routing policy selects at emit time.

`id` · `notification_id` (FK to `notification.id`, indexed) · `channel` (indexed)
· `status` (indexed) · `attempts` · `claimed_at` · `sent_at` · `last_error`

## 8. Migrations

- Chain: `src/asas_notifications/migrations/`, revisions `0001_baseline`,
  `0002_archived_at`.
- Version table: `alembic_version_asas_notifications` (package-scoped,
  adopt-or-create bootstrap).
- Call `migrate(engine)` at boot, **before** the host's own chain.
- Batch mode, `native_enum=False`, portable server defaults. Runs on SQLite and
  Postgres; CI covers both.

## 9. Invariants

These are the rules the engine enforces. They are the reason to use it rather
than write `INSERT INTO notification`.

1. **Actor exclusion.** A user is never notified of their own action. Pass
   `actor_user_id` and that id is removed from the recipient set.
2. **Visibility filtering.** Recipients pass through the host's registered
   filter before any row is written, so a notification can never leak the
   existence of a record its recipient may not see.
3. **The insert is the enqueue.** `notify()` writes in the caller's transaction
   and never commits. A rolled-back domain change takes its notifications with
   it. There is no separate publish step to get wrong.
4. **Routing by urgency.** `low` is in-app only — ambient activity never emails.
   `normal` and `high` also get delivery rows.
5. **Coalescing.** With `coalesce_unread=True`, an unread row for the same
   `(recipient, kind, entity)` is merged into rather than duplicated, so an edit
   burst stays one bell entry. `merge_body` controls how bodies combine.
   Coalescing never merges into an archived row.
6. **Duplicate-safe dispatch.** Each outbox row is claimed with a rows-affected
   CAS before the adapter is called. Overlapping passes (after-commit hook
   racing the periodic job, or two instances) lose the CAS and skip the row.
   Claims older than `STALE_CLAIM_SECONDS` are treated as belonging to a crashed
   pass and reclaimed. Delivery is **at-least-once**, not exactly-once: an
   adapter that sends and then crashes before the status write will send again.
   Adapters should be idempotent where the channel allows it.
7. **Read and archived are independent axes.** Reading does not archive.
   Archiving does not mark read. Un-archiving does not restore unread. A host
   showing actionable notifications needs a row to survive being read and to
   leave only when the recipient acts on it — an "unread means outstanding"
   model empties itself as the recipient browses.
8. **`unread_count` ignores filters.** On every list response it is
   unread-and-un-archived, so a badge fed from any call agrees with every other.
   `total` follows the request's filters; `unread_count` never does.
9. **`archive_read` never archives an unread row.** Bulk-filing what you have
   dealt with cannot hide something you have not seen.
10. **Unregistered kinds fail loud**, including inside `suppressed()`.
    Suppression silences delivery, never catalog mistakes.

## 10. Failure modes

| Condition | Behavior |
|---|---|
| Unregistered `kind` | Raises. Fails loud at emit, including under suppression. |
| Adapter raises `SkipDelivery` | Row marked `skipped`. No retry. Use for "this channel is not configured in this deployment." |
| Adapter raises anything else | Row marked `failed`, `last_error` recorded, retried on later passes until `MAX_ATTEMPTS` (5). |
| No adapter registered for a channel | Row marked `skipped`. |
| Claim older than `STALE_CLAIM_SECONDS` (300) | Reclaimed by the next pass, on the assumption the previous pass died. |
| No authenticated user on a router call | `401 No authenticated user`. |
| Notification id not found, or not the caller's | `404 Notification not found`. |
| Recipient filter returns empty | `notify()` returns `[]`. Not an error. |

**Two open defects recorded in DR 0001** ([PR #15](https://github.com/wlootah-a11y/asas/pull/15), pending merge; relink to `../design/0001-tenancy.md` once it lands):

- **T-2 (Critical).** `Notification.org_id` is `int NOT NULL` with no default,
  but the context resolver contract permits `None` outside a request. Emitting
  from a background job, CLI or boot sweep inserts NULL and raises
  `IntegrityError` at flush — and because the insert rides the producer's
  transaction, **the domain write dies with it**. Until this is fixed, only emit
  from within a request context.
- **T-6 (Medium).** Coalescing selects its merge target with no `org_id`
  predicate, so where entity ids are not globally unique an emit in one org can
  fold into another org's row.

## 11. Testing against it

The package's own suite:

```bash
cd packages/asas-notifications
pip install -e '.[dev]'
pytest -q                                             # SQLite
TEST_DATABASE_URL=postgresql+psycopg2://… pytest -q   # Postgres
```

Host-side, `LoggingAdapter` is the seam. Register it, run a dispatch pass, and
assert on `.sent`:

```python
adapter = notifications.LoggingAdapter()
notifications.register_adapter("email", adapter)

notifications.notify(session, [user.id], "workflow.approval_requested", title="X")
session.commit()
notifications.dispatch_pending(engine)

assert [p.title for p in adapter.sent] == ["X"]
```

Reset global state between tests: `configure_context_resolver(None)`,
`configure_recipient_filter(None)`, `register_adapter("email", None)`.

## 12. Design notes

- Origin: Teamy epics WXL-209 / WXL-222 (engine and outbox), TEAMY-475 (dispatch
  hardening: CAS claims, stale reclaim), TEAMY-693 (archive axis and inbox
  filters). Extraction epic TEAMY-466, design record 0017.
- Related: DR 0001 — tenancy ([PR #15](https://github.com/wlootah-a11y/asas/pull/15),
  defects T-2, T-6), [DR 0002 — documentation](../design/0002-package-documentation.md).
- The channel-adapter registry follows the house provider pattern: adding a
  channel is one adapter class and one `register_adapter` call, with no caller
  changes. Payloads are channel-agnostic; formatting belongs to the adapter.
- `resolved_at` is deliberately unwritten (see §7).
