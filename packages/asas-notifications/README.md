# asas-notifications

A generic notification engine: producers register event **kinds** (category ×
urgency × reason taxonomy) and call `notify` inside their own transaction — the
insert IS the enqueue, so a notification exists iff the domain change committed.
The in-app feed is the `notification` row itself (`build_router` serves it);
external channels go through the `notification_delivery` outbox and registered
**channel adapters** (email, chat, …).

Engine rules baked in (from Teamy's WXL-209 epic):

- **Actor exclusion** — you are never notified of your own action.
- **Visibility filtering** — recipients pass through the host's registered filter,
  so a notification never leaks a private record.
- **Routing by urgency** — `low` is in-app only (ambient activity never emails);
  `normal`/`high` get delivery rows. Unread ambient rows can **coalesce** per
  (recipient, kind, entity) so an edit burst stays one bell entry.
- **Duplicate-safe dispatch** — each outbox row is claimed with a rows-affected
  CAS before the adapter send; overlapping passes (hook vs job vs second
  instance) lose the CAS and skip. Stale claims from crashed passes reclaim.
  At-least-once overall; failures retry to an attempt cap, `SkipDelivery` marks
  a graceful no-retry skip.

Table-owning + router variant of the Asas host contract (2 tables; org/user
refs are plain ints — no host FKs):

- **`migrate(engine)`** — package Alembic chain
  (`alembic_version_asas_notifications`, adopt-or-create).
- **`build_router(get_session)`** — the `/me/notifications` feed API; the host
  applies auth at include time.
- **`configure_context_resolver(fn)`** — `(session) -> (user_id, org_id) | None`.
- **`configure_recipient_filter(fn)`** — `(session, user_ids, entity_type,
  entity_id, record) -> user_ids` allowed to know the subject exists. Runs for
  **every** `notify` that names an `entity_type`. `record` is the subject row
  when the producer had it and `None` when it did not — a generic producer may
  hold only the type and the id — so the filter gets both and decides: use the
  row, resolve it from the id, or return `user_ids` unchanged for an entity
  type that needs no gating.
- **`register_kind` / `register_adapter`** — the kind catalog and channel
  adapters are the host's; `dispatch_pending(engine)` is one outbox pass and the
  host owns the cadences (after-commit hook, boot sweep, periodic job).

```python
import asas_notifications as notifications

# boot (host wiring)
notifications.migrate(engine)
notifications.configure_context_resolver(current_user_org)
notifications.configure_recipient_filter(visible_recipients)
notifications.register_kind("workflow.approval_requested",
                            category="action", urgency="normal", reason="participant")
notifications.register_adapter("email", MyEmailAdapter())
app.include_router(notifications.build_router(get_session),
                   dependencies=[Depends(require_user)])

# producers (inside their own transaction)
notifications.notify(session, recipient_user_ids, "workflow.approval_requested",
                     title="Budget change", record=project, entity_type="project",
                     entity_id=project.id, actor_user_id=actor.id)

# dispatch cadence of your choosing
notifications.dispatch_pending(engine)
```

## Feed state: two independent axes

A notification carries **`read_at`** (seen) and **`archived_at`** (dealt with),
and they never imply each other. Reading does not archive, archiving does not
mark read, and un-archiving does not make a row unread again. That separation is
the point: a host showing actionable notifications needs a row to survive being
read and to leave only when the recipient acts on it or files it away — an
"unread means outstanding" model empties itself as the recipient browses.

`GET /me/notifications` filters compose freely:

| Param | Values | Default |
| --- | --- | --- |
| `state` | `open` (un-archived) · `archived` · `all` | `open` |
| `unread_only` | bool | `false` |
| `category` | `action` · `info` · `warning` | all |

So `?state=open&category=action` is "still needs me", `?state=archived` is the
history, and `?unread_only=true` is the classic feed. `total` reflects the
filters; **`unread_count` never does** — it is unread-and-un-archived on every
response, so a badge fed from any list call agrees with every other.

Writes: `POST /{id}/read`, `/read-all`, `/{id}/archive`, `/{id}/unarchive`, and
`/archive-read` (bulk-files the read rows, never an unread one). `resolved_at`
exists on the row but is deliberately unwritten — Teamy weighed auto-clearing
action rows from engine events and chose the archive gesture instead.

See the repo README for the full contract. Extracted from Teamy (notifications
epic WXL-209/WXL-222 + TEAMY-475 dispatch hardening; extraction epic TEAMY-466 /
design record 0017; archive axis + inbox filters TEAMY-693).
