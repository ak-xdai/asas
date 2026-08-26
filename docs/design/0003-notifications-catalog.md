# DR 0003 (Asas): asas-notifications — persistent catalog and data-driven routing

Status: DRAFT for discussion · Author: ak@xdigit.ai (with Claude) · Date: 2026-08-26

## 1. Problem

`asas-notifications` (v0.13.0) has a strong runtime core — transactional emit,
per-channel outbox with CAS-claimed dispatch, archive/read axes — but all of its
*configuration* lives in process memory and code:

- **The kind catalog is a module-level dict.** `service._KINDS` is populated by
  `register_kind()` at wiring time and disappears on restart. Replicas can
  drift; nothing outside the process can read or manage the catalog.
- **Routing is hard-coded.** `_channels_for()` is three lines: urgency `low` →
  in-app only, `normal`/`high` → email. Category and reason are carried on
  every row ("the schema already carries all keys") but route nothing.
- **Presentation is scattered.** Every producer composes `title`/`body` at the
  call site; there is no template layer, no localization, and no way for a
  product owner to change wording without a deployment.
- **No admin or preference surface is possible.** With the catalog in memory
  there is nothing for an admin API to CRUD and nothing for user preferences
  to attach to.

Changing *any* notification behavior — enabling a channel, rewording an email,
silencing a noisy kind — is a code change and a deployment. The proposal
document this DR distills (asas-notifications evolution plan, 2026-08-26,
circulated outside the repo) states the target: developers declare **what
happened**; administrators configure **how it behaves** — at runtime, in the
database, through a packaged admin interface.

## 2. Goals and non-goals

Goals (this DR):

- G-1 The database is the single runtime source of truth for notification
  configuration. No YAML/companion source; the future admin API writes rows.
- G-2 A stable event identity (`kind.key`) whose behavior — grouping, urgency,
  channels, enablement, coalescing — is resolved from the database at emit.
- G-3 Category-level user preferences (a handful of groups), never
  per-event-kind preference rows.
- G-4 Preserve the current invariants unchanged: emit rides the producer's
  transaction; the insert IS the enqueue; dispatch stays at-least-once with
  CAS claims; visibility filtering never leaks a private record.
- G-5 A compatibility path where existing `register_kind()` callers keep
  working through one deprecation cycle.

Non-goals (deliberately out, per the proposal's own §22):

- The admin React UI and end-user React components (follow-up DR; they also
  force a JS workspace decision on the repo that deserves its own record).
- Templates/localization *implementation* (the table is designed here so the
  catalog schema is stable; rendering ships in a later phase).
- Real-time delivery (SSE/WebSocket), retention policies, delivery dashboards.
- Any new infrastructure: no Redis, no message bus, no separate service.

## 3. Concepts — and one naming collision to resolve first

The proposal reuses the word **category** for its user-facing preference groups
(`Approvals`, `Mentions`, `Activity`…). The package already has a `Category`
enum — `action` / `info` / `warning` — which is a *display semantic*: it drives
the feed's `?category=` filter and Teamy's "needs action" tab. These are
different dimensions and must not be merged; repurposing the existing column
would silently break the feed API contract.

This DR therefore uses two names:

| Term | What it answers | Cardinality | Who sees it |
|---|---|---|---|
| `category` (existing enum) | How should the UI treat this row? | 3, fixed | The feed API/UI |
| `preference_group` (new) | Which knob controls this? | ~5–8 per deployment | Users, admins |

`reason` (`requested`/`participant`/`watching` — GitHub's participating-vs-
watching, generalized) stays as emit metadata and gains an *optional* seat in
routing policy (D-2). `kind` remains the stable event identity.

## 4. Data model

Runtime tables (`notification`, `notification_delivery`) stay as they are, with
one addition: a nullable `data` JSON column on `notification` (D-3). Four
configuration tables are added, all package-owned via the existing Alembic
chain:

### 4.1 `notification_kind`

| column | notes |
|---|---|
| `id`, `created_at`, `updated_at` | |
| `key` | unique, e.g. `approval.requested` — the value producers emit |
| `name`, `description` | admin-facing |
| `preference_group_id` | FK → `notification_preference_group` |
| `category` | display enum (`action`/`info`/`warning`), default for emits |
| `default_urgency` | `low`/`normal`/`high` |
| `default_reason` | default when the emit passes none |
| `enabled` | disabled kinds emit nothing (in-app included) |
| `coalesce` | replaces the per-call `coalesce_unread` flag as the default |
| `auto_created` | true when created by the unknown-kind path (D-5) |

### 4.2 `notification_preference_group`

| column | notes |
|---|---|
| `id`, `created_at`, `updated_at` | |
| `key`, `name`, `description` | e.g. `approvals` / "Approvals" |
| `user_configurable` | groups like `security` can be locked |
| `sort_order` | preference-screen ordering |

### 4.3 `notification_channel_policy`

| column | notes |
|---|---|
| `id`, `created_at`, `updated_at` | |
| `preference_group_id` | nullable — group-level default |
| `kind_id` | nullable — kind-level override (exceptional) |
| `reason` | nullable — condition on the recipient's reason (D-2) |
| `channel` | `in_app`, `email`, `teams`, … (free string, adapter-keyed) |
| `enabled` | |
| `mandatory` | user preferences cannot disable (e.g. security → email) |

Exactly one of `preference_group_id`/`kind_id` is set (CHECK constraint).
Resolution precedence is in §5.

### 4.4 `notification_user_preference`

| column | notes |
|---|---|
| `id`, `created_at`, `updated_at` | |
| `user_id` | plain int, no host FK (extraction rule) |
| `preference_group_id` | preferences attach to groups, never kinds (G-3) |
| `channel` | |
| `enabled` | |

Unique on (`user_id`, `preference_group_id`, `channel`). Absence of a row means
"use the policy default" — the table stores only deviations, so changing an
org-wide default later doesn't require backfilling every user.

### 4.5 `notification_template` (schema now, implementation Phase E)

(`kind_id`, `channel`, `locale`, `title_template`, `body_template`, `enabled`);
unique on (`kind_id`, `channel`, `locale`). English/Arabic are the first two
locales the rendering phase must support.

**Org scoping of the catalog (D-6):** all five configuration tables are
deployment-global — no `org_id`. Notification *rows* are tenant data; the
*catalog* is product configuration, like code was before. Per-org overrides, if
ever needed, arrive as nullable `org_id` override rows on the policy and
preference-group tables — an additive change, not a redesign. (Aligns with DR
0001's separation of tenant data from deployment configuration.)

## 5. Runtime resolution

`notify()` becomes:

```
resolve kind by key (cached, D-4)
  └ unknown → per-deployment mode (D-5)
kind.enabled? no → return []          (nothing inserted, in-app included)
category  = per-emit override | kind.category
urgency   = per-emit override | kind.default_urgency
reason    = per-emit override | kind.default_reason
channels  = policy resolution:
    start: kind-level policy rows (matching reason or reason IS NULL)
    else:  group-level policy rows      (same matching)
    else:  built-in fallback = today's rule (low → in-app only, else email)
    then:  subtract user-disabled channels (never mandatory ones)
in-app row + delivery rows inserted in the caller's transaction (unchanged)
```

Notes:

- `in_app` becomes an explicit channel in policy (mandatory-able, e.g. a group
  whose members may mute even the bell) but keeps its implementation: the
  notification row itself, no delivery row.
- User preferences are read at **emit** (they gate row creation); templates are
  rendered at **dispatch** (D-3). A preference change therefore affects new
  events, not queued ones — same model as every mainstream notifier.
- The per-call `coalesce_unread` flag is deprecated in favor of
  `kind.coalesce`; the org-context requirement from PR #20 stands.

## 6. Decisions

- **D-1 Keep `category`, add `preference_group`.** The display enum and the
  preference dimension are different concepts (§3). No migration of existing
  rows; the feed API contract is untouched.
- **D-2 `reason` stays, as optional policy input.** Policy rows may condition
  on reason (`watching` never emails; `requested` always does) via one nullable
  column. It never becomes a per-user preference dimension — that would
  multiply the preference matrix for marginal value. If no deployment uses it
  within two phases, we drop the column, not the concept.
- **D-3 Render in-app at emit, external at dispatch.** The feed needs a
  concrete `title` at insert; external channels re-render from
  `notification.data` + template at dispatch, so template edits apply to
  not-yet-sent deliveries and per-recipient locale is resolved where the
  recipient is known. Producers may still pass explicit `title`/`body`
  (ad hoc notifications keep working; they simply have no kind-level config).
- **D-4 Catalog reads are cached in-process, TTL ≤ 60s.** `notify()` runs
  inside hot business transactions; per-emit catalog queries are unacceptable.
  The existing `_KINDS` dict becomes that cache. Admin changes propagate
  within the TTL across replicas; no cross-replica invalidation bus (non-goal).
- **D-5 Unknown kinds: wiring-time mode, default = today's fail-loud.**
  `configure_catalog(on_unknown="reject" | "create")`. `create` (dev
  convenience) inserts a disabled-external, `low`-urgency, uncategorized kind
  flagged `auto_created` and logs it. Production deployments keep `reject`;
  an `asas-notifications validate` CLI (compares emitted-kind inventory against
  the catalog) ships in the same phase so CI can gate deploys. Auto-creation
  never happens silently in prod.
- **D-6 Catalog is deployment-global** (§4, org scoping note).
- **D-7 Urgency stays `low`/`normal`/`high`.** No `critical` until a real
  escalation behavior needs it (proposal §11 says the same).
- **D-8 `register_kind()` becomes a seed, then deprecated.** Phase A: it
  upserts missing kinds/groups at startup and never overwrites existing rows —
  the database wins. Phase B: it warns; the catalog is authoritative. One
  release later it is removed.

## 7. Rollout (the implementing PRs)

- **Phase A** — migrations for the five tables; `register_kind()` upserts;
  resolution still reads the in-memory registry. Pure additive, no behavior
  change.
- **Phase B** — `notify()` resolves from the database through the D-4 cache;
  policy resolution (§5) replaces `_channels_for()`; built-in fallback keeps
  today's behavior for catalogs with no policy rows. `validate` CLI + D-5 mode.
- **Phase C** — admin CRUD API (`build_admin_router(get_session)`, host applies
  auth; audit via the shared Asas audit capability if one exists by then).
- **Phase D** — user preference API + resolution step.
- **Phase E** — templates + rendering (dispatch-side), `data` column usage,
  English + Arabic.
- **Phase F+** — admin UI / React components / operations dashboard: separate
  DR after the JS-workspace decision.

Each phase is a reviewable PR against the current test matrix (SQLite +
Postgres); Phase B must land with routing-equivalence tests proving an empty
catalog reproduces today's urgency rule exactly.

## 8. Open questions for review

1. **`in_app` as a policy channel (§5):** worth the generality, or should the
   bell stay unconditional and policy govern external channels only?
2. **Ad hoc notifications (no kind):** keep them fully outside the catalog
   (current lean), or require a reserved `system.adhoc` kind so they are at
   least group-able for preferences?
3. **`suppressed()` interaction:** should suppression also skip catalog
   auto-creation in `create` mode, or is discovering kinds during bulk imports
   desirable?
4. **Does Phase C block on the shared audit capability**, or ship with a
   package-local audit table and migrate later?
