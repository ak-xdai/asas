# DR 0003 (Asas): asas-notifications — action-referenced notifications and axis-based management

Status: DRAFT v2 for review · Author: ak@xdigit.ai (with Claude) · Date: 2026-08-27
Supersedes: the 2026-08-26 v1 draft of this DR (persistent kind catalog), replaced
in full after design review. Companion reading: the upstream author's adoption
guide (2026-08, circulated as PDF), whose invariants this DR preserves.

## 1. Overview — concept, architecture, and agreed principles

This DR updates the asas-notifications model in one sentence:

> A notification **references the application action that caused it** and carries
> **four classification axes**; behavior is decided by rules attached to the
> axes — never to individual event types — and the database stores only
> deviations from code-declared defaults.

It replaces two things in the current design: the package-private `kind`
vocabulary (a shadow copy of the application's own action vocabulary) and the
in-memory registration ceremony (`register_kind`). It deliberately does **not**
replace any runtime machinery: the transactional emit, the outbox, dispatch,
and the feed model are untouched.

### The control split (the architecture in one table)

| Tier | Decides | Lives in |
|---|---|---|
| **Developer** | what each emit *is*: action, axes, entity, template choice, coalescing eligibility | code |
| **Admin / product owner** | what the axes *mean*: routing policy, mandatory floors, topic definitions, template wording and locales | database |
| **User** | what *reaches them*: per-topic and per-reason channel preferences (narrowing only) | database |

### Agreed principles (the record of the design discussion)

- **P-1 One namespace.** Notifications do not maintain a terminology analogous
  to the application's actions. The emit passes the action that triggered it
  (`action="job.publish"`) as a *reference without declaration* — a free string
  in the app's `entity.verb` grammar, registered nowhere.
- **P-2 Identity at the leaf, management on the axes.** The action string is
  identity (provenance, coalescing, analytics). All *management* — routing,
  preferences, floors — attaches to four coarse axes that are total over all
  present and future emits, so a new action needs zero setup to be governed.
- **P-3 Imperative tense, success semantics.** The action is named in the
  imperative (`job.publish`, the same id a permission system would use), and
  `notify()` is called only on success — inside the committing transaction, an
  invariant the package already enforces by construction. No parallel
  past-tense "fact"/"event" vocabulary is introduced.
- **P-4 Admins do not manage application logic.** Per-event-type admin control
  ("mute `vcs.pr_opened`") is deliberately excluded: it is application behavior
  reached through a settings screen. If one emit is miscalibrated, that is a
  code fix to its axes.
- **P-5 The database stores deviations, not the universe.** No table enumerates
  the app's events. Config rows exist only where someone changed a default:
  a policy row, a preference row, a template.
- **P-6 Nothing to forget.** There is no registration step whose omission
  silently misroutes an event. Every emit is self-contained; validation
  (§4, I-4) checks references, not ceremonies.
- **P-7 Preserve the engine.** Everything the adoption guide sells stays
  bit-for-bit: emit rides the producer's transaction (insert IS the enqueue),
  CAS-claimed at-least-once dispatch, actor exclusion, visibility filtering at
  the emit boundary, read/archived as independent axes, badge rule, and the
  channel-agnostic adapter payload as a stability contract.

## 2. Current model — areas of improvement

The current model (v0.13, as documented in the adoption guide):

- **A-1 The kind catalog is process memory.** `register_kind()` populates a
  module dict at boot; replicas can drift, nothing outside the process can
  read it, and every behavioral change is a deployment.
- **A-2 `kind` duplicates the application's vocabulary.** Teamy's 13 kinds
  (`workflow.approval_requested`, `vcs.pr_opened`, …) are restatements of
  application actions under a second, package-private naming scheme that the
  host must keep aligned by hand — the exact multi-catalog problem Asas will
  otherwise repeat across RBAC, audit, and workflow as the package count grows.
- **A-3 Registration is a ceremony with runtime-only failure.** Forgetting
  `register_kind` fails loud (good) but only when the code path first runs
  (late), and the ceremony exists solely to feed defaults that could travel on
  the emit itself.
- **A-4 Routing is three hard-coded lines.** urgency `low` → in-app only,
  else → email. Category and reason are carried on every row but route nothing
  ("reserved", per the guide).
- **A-5 No preference surface.** The guide says it plainly: "there is no
  per-user preference engine yet … budget for both." There is also no grouping
  axis for one to attach to — kinds are too granular to be the preference unit.
- **A-6 Presentation is compiled in.** Titles and bodies are composed at call
  sites; product owners cannot edit wording, and there is no localization path
  (Arabic/English matters for the target deployments).

## 3. Design — specific suggestions

### S-1 The emit carries four axes

```python
notifications.notify(
    session, recipients,
    action="job.publish",                    # S-2: reference, not declaration
    topic="jobs",                            # management/preference grouping
    nature="info",                           # action | info | warning
    urgency="normal",                        # low | normal | high
    reason="watching",                       # requested | participant | watching
    entity_type="job", entity_id=job.id, record=job,
    template="job_published",                # S-4: optional; title/body= fallback
    data={"job_title": job.title},
    actor_user_id=actor.id,
)
```

| Axis | Question | Values | Defined by | Prior art |
|---|---|---|---|---|
| `nature` | What does it demand of me? | action / info / warning | package (fixed) | today's `category` enum, renamed |
| `topic` | What part of the product? | ~5–8 per app | host, seeded rows | Android channels; the plan's groups |
| `urgency` | How interruptive? | low / normal / high | package (fixed) | Apple interruption levels |
| `reason` | Why me? | requested / participant / watching | package (fixed) | GitHub reasons (unchanged) |

`nature`/`urgency`/`reason` are enums; `topic` is validated against the seeded
topic table — the one reference an emit can get wrong that preferences and
policy depend on, so an unknown topic fails loud (preserving the guide's
fail-loud property exactly where it still has a job).

Teamy's 13 kinds map onto ~6 topics with no orphans and no splits (evidence the
cap holds on real data): approvals (4 workflow kinds), mentions, assignments,
activity (3), code (2 vcs), system (2).

### S-2 `action` replaces `kind`: reference without declaration

The `kind` column becomes `action`. It is passed on every non-ad-hoc emit and
declared nowhere. It serves exactly three purposes:

1. **Provenance** — which application action produced this row (debugging,
   analytics, feed iconography).
2. **Coalescing identity** — see S-5.
3. **The future join key** — if/when an application actions layer exists
   (declared actions driving permissions/audit/tooling), this column already
   speaks its namespace: a validate step can cross-check emitted actions
   against declared ones, and an actions runtime can stamp the column
   automatically, all without schema change. This DR does not depend on or
   design that layer.

One action may legitimately produce several notifications (watchers ambiently
and the owner directly, from one `job.publish`): distinct emits, distinct
axes/templates, same action. The action is provenance, not a unique key.

Ad hoc notifications (`notify(title=..., urgency="low")`, no action) remain for
genuine one-offs; they carry axes but no action or template.

### S-3 The database stores deviations: five small tables, no catalog

| Table | Keyed by | Holds |
|---|---|---|
| `notification_topic` | `key` | the seeded topic list: name, description, `user_configurable`, `sort_order` |
| `notification_channel_policy` | (`topic` \| axis condition) × `channel` | enabled / mandatory rows; the routing table |
| `notification_topic_preference` | `user_id` × `topic` × `channel` | user deviations from policy |
| `notification_reason_preference` | `user_id` × `reason` × `channel` | e.g. "email me only when requested" |
| `notification_template` | `key` × `channel` (× `locale`, later) | product-editable title/body templates |

There is **no** table of event types. Empty policy tables must reproduce
today's behavior exactly (urgency low → in-app only; normal/high → email) via
built-in fallback rows — the Phase-2 equivalence tests in §5 prove it.

### S-4 Templates by explicit reference

Code chooses *which* template (`template="approval_requested"`); the DB row
owns *what it says*. No template row → the emit's inline `title`/`body` render
as-is. Localization arrives later as per-locale template rows resolved at
dispatch; the renderer sits between the outbox and the adapter, so the
`DeliveryPayload` an adapter receives is unchanged (P-7).

### S-5 Resolution rule

```
channels(emit) =
      policy(topic, nature, urgency)          # admin routing table, floors marked mandatory
    ∧ topic_preference(user, topic)           # user narrowing
    ∧ reason_preference(user, reason)         # user narrowing
    with mandatory channels exempt from both preference filters
```

Preferences compose by **narrowing only** (each rule can remove channels, never
add), so the two preference dimensions AND cleanly without a topic×reason×
channel cube. Coalescing keys on **(recipient, action, entity)** — the same
granularity as today's kind-based folding (edit bursts fold; comments on the
same entity stay separate) — still requires an org context (PR #20), and still
applies only when the resolved channels are in-app only. Note the coupling that
creates: a policy change that routes a topic externally also stops its
coalescing. Correct, but an admin surface must say so.

### S-6 Admin scope

Admins manage: topics, the routing policy table, mandatory floors, template
wording/locales, and (from PR #20's groundwork) delivery operations. Admins do
**not** manage individual actions (P-4). The admin API/UI ships against the
five S-3 tables and is therefore small.

## 4. Implications

- **I-1 Per-event runtime tuning requires a deploy — by design.** The v1-draft
  catalog allowed an admin to re-urgency one event type at runtime; this design
  trades that for P-4. Accepted explicitly in review.
- **I-2 Feed API field rename.** `kind` → `action` and `category` → `nature`
  appear in `NotificationRead` and the `?category=` filter. Pre-1.0 breaking
  minor bump; the filter keeps `category` as a deprecated alias for one release.
- **I-3 `register_kind()` becomes a shim, immediately.** No phased catalog
  life: the shim maps a registered kind's (category/urgency/reason) to axis
  defaults applied when `notify()` is called with a legacy kind string and no
  axes, warns on use, and is removed one minor release later. The guide's boot
  wiring keeps working through the deprecation window.
- **I-4 Validation shrinks to references.** `asas-notifications validate`
  checks: every `template=` reference resolves; every `topic=` exists; (later)
  every `action=` exists in the host's declared actions, when a host has such a
  list. No catalog sync to verify — there is no catalog.
- **I-5 What is lost, on the record:** per-event admin mute (P-4, code fix
  instead), per-event analytics keyed by a curated catalog (action strings +
  topic serve instead), and the v1 draft's auto-create/`validate`-the-catalog
  machinery (obsolete — nothing to create).
- **I-6 Relationship to open DRs.** DR 0001 (tenancy): config tables are
  deployment-global, org-free, per DR 0001's data/config split; notification
  rows remain tenant data. The channel-cascade DR 0002 (escalation) composes:
  cascade steps are a *policy-layer* concern (which channel, then which) and
  attach to the S-3 policy table, not to actions. Numbering collision between
  the two circulating "DR 0002"s still needs resolving; unaffected by this DR.

## 5. Specific updates to be made (the implementing PRs)

1. **U-1 Schema + emit.** Migration: rename `notification.kind` → `action`
   (nullable now — ad hoc emits), `category` → `nature`, add `topic` (indexed),
   `data` JSON, `template` ref. New `notify()` signature with the four axes;
   `register_kind` shim per I-3; feed filter alias per I-2. Version bump
   (breaking minor).
2. **U-2 Topics + policy + resolution.** `notification_topic` +
   `notification_channel_policy` tables; resolution (S-5) replaces
   `_channels_for()`; built-in fallback = today's rule; **equivalence tests**:
   empty tables reproduce v0.13 routing decisions for the full Teamy catalog
   mapped to axes.
3. **U-3 Preferences.** Both preference tables, the AND rule, mandatory-floor
   exemption, `/me/notification-preferences` API. Two-org and both-engine tests.
4. **U-4 Templates + renderer.** Template table, dispatch-side rendering,
   `DeliveryPayload` unchanged; missing-variable detection; locale column
   landed but only `en` resolved (Arabic in the localization follow-up).
5. **U-5 Admin API.** CRUD routers for topics/policy/templates behind
   host auth (`build_admin_router(get_session)`); audit hooks if the shared
   audit capability exists by then, else a TODO referencing it.
6. **U-6 Validate CLI** per I-4, CI-friendly exit codes.

Each phase is one reviewable PR, SQLite + Postgres green, version-bumped per
RELEASING.md. U-1/U-2 are the substance; U-3–U-6 are additive.

## 6. Open questions for review

1. `action` nullable (ad hoc emits) or required with a reserved `system.adhoc`
   value — which failure mode do we prefer for lazy producers?
2. Does the reason-preference UI ship in U-3, or does the table land with the
   API exposing topics only until a host asks for it?
3. Coalesced digest titles: keep `merge_body` as the producer's hook (status
   quo), or move merging into templates once U-4 lands?
4. The `?category=` alias window: one release or two?
