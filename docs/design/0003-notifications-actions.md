# DR 0003 (Asas): asas-notifications — action-referenced notifications and axis-based management

Status: DRAFT v3 for review · Author: ak@xdigit.ai (with Claude) · Date: 2026-09-03
History: v1 (2026-08-26, persistent kind catalog) rejected in design review.
v2 (2026-08-27, PR #21) established the model; this v3 incorporates the
upstream author's PR #21 review in full — the three blocking items, the
mechanical fixes, the review's positions on all four open questions, and a
rebase against `main` at asas-notifications **0.15.0**. Companion reading: the
author's adoption guide (2026-08 PDF), whose invariants this DR preserves.

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

### Agreed principles

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
  (I-4) checks references, not ceremonies.
- **P-7 Preserve the engine.** The runtime invariants stay: emit rides the
  producer's transaction (insert IS the enqueue), CAS-claimed at-least-once
  dispatch, actor exclusion, visibility filtering at the emit boundary (with
  0.14.0's filter signature carrying `entity_id`), org-scoped read paths and
  coalescing (0.13.0/0.15.0), read/archived as independent feed axes, and the
  badge rule. Adapters remain render-consumers of a channel-agnostic payload;
  the payload's *field names* change once with this DR's breaking bump (S-7) —
  its *shape and role* are the stability contract.

## 2. Current model — areas of improvement

The current model (v0.15.0, as documented in the adoption guide):

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
| `topic` | What part of the product? | ~5–8 per app | host, seeded rows | Android channels |
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

The `kind` column becomes `action` — **nullable**: ad hoc emits
(`notify(title=..., urgency="low")`, no action) carry axes but no action, which
naturally excludes them from coalescing and keeps provenance queries and any
future declared-actions cross-check free of a reserved fake value (per review).
For non-ad-hoc emits the action serves exactly three purposes:

1. **Provenance** — which application action produced this row (debugging,
   analytics, feed iconography).
2. **Coalescing identity** — see S-6.
3. **The future join key** — if/when an application actions layer exists
   (declared actions driving permissions/audit/tooling), this column already
   speaks its namespace: a validate step can cross-check emitted actions
   against declared ones, and an actions runtime can stamp the column
   automatically, all without schema change. This DR does not depend on or
   design that layer.

One action may legitimately produce several notifications (watchers ambiently
and the owner directly, from one `job.publish`): distinct emits, distinct
axes/templates, same action. The action is provenance, not a unique key.

### S-3 The database stores deviations: five small tables, shared-with-overrides

| Table | Keyed by | Holds |
|---|---|---|
| `notification_topic` | `key` (× `org_id` nullable) | the seeded topic list: name, description, `user_configurable`, `sort_order` |
| `notification_channel_policy` | (`topic` \| axis condition) × `channel` (× `org_id` nullable) | enabled / mandatory rows; the routing table (precedence: S-5) |
| `notification_topic_preference` | `org_id` × `user_id` × `topic` × `channel` | user deviations from policy |
| `notification_reason_preference` | `org_id` × `user_id` × `reason` × `channel` | e.g. "email me only when requested" |
| `notification_template` | `key` × `channel` (× `locale`) (× `org_id` nullable) | product-editable title/body templates |

Tenancy (revised per review, following DR 0001's landed shared-with-overrides
pattern from asas-lookups): the three config tables hold **platform rows**
(`org_id NULL`) and optional **org override rows**; an org row beats the
platform row for the same key. Org-level admin UI can come later — the schema
is ready now. Both **preference tables carry `org_id`**: preferences are
per-membership, so a user in two orgs configures each independently (matching
notification rows, which are org data).

A policy row conditions on **exactly one of** `topic` **or** an axis condition
(`urgency` and/or `nature` values) — enforced by CHECK. There is **no** table
of event types, and empty config tables must reproduce 0.15.0 behavior exactly
(the U-2 equivalence tests prove it).

`data` is a denormalized presentation payload — the structured sibling of
`title` today, with the same PII/retention posture as the row it sits on. When
coalescing folds rows, the latest `data` wins (as the latest title does today).

### S-4 Templates by explicit reference; in-app renders at emit

Code chooses *which* template (`template="approval_requested"`); the DB row
owns *what it says*. No template row → the emit's inline `title`/`body` render
as-is.

**Where rendering happens** (revised per review — the v2 text covered only the
outbox path):

- **In-app: rendered at emit and stored.** A feed row is a *historical
  record*; retroactively rewriting past notifications when a product owner
  edits wording would falsify history, so wording edits apply to future emits
  only. The recipient's locale is resolved at emit through a new host seam,
  `configure_locale_resolver((user_id, org_id) -> locale | None)` — rows are
  per-recipient, so per-recipient locale at emit is well-defined. The row
  stores `template` + `data` alongside the rendered text, so a future
  localization DR *may* move the feed to read-time rendering without a
  migration; that decision is explicitly deferred there.
- **External channels: rendered at dispatch**, between the outbox and the
  adapter, using the same locale seam — so template edits do apply to
  not-yet-sent deliveries, and the adapter contract is untouched.
- **Render failure never blocks delivery**: a template that errors at dispatch
  (bad edit, missing variable) falls back to the stored `title`/`body` and
  logs; the admin API validates variables at save time, and the validate CLI
  (I-4) checks references — but the runtime failure mode is degraded prose,
  never a `failed` delivery row.

### S-5 Resolution rule and policy precedence

```
channels(emit) =
      policy(topic, nature, urgency)          # precedence below
    ∧ topic_preference(org, user, topic)      # user narrowing — all channels incl. in_app
    ∧ reason_preference(org, user, reason)    # user narrowing — external channels only
    with mandatory channels exempt from both preference filters
```

**Policy precedence (per channel, most specific wins):**

1. a `topic`-condition row (org override row beats platform row);
2. else an axis-condition row (urgency/nature; org beats platform);
3. else the **built-in code fallback** = today's rule (`low` → in-app only,
   else in-app + email).

The fallback being code (not wildcard rows) keeps "empty tables reproduce
0.15.0" trivially true. `mandatory` applies from whichever row wins.
Preferences compose by **narrowing only** (each rule can remove channels,
never add), so the two preference dimensions AND cleanly without a
topic×reason×channel cube.

**`in_app` is a real, narrowable channel** (revised per review question): a
user may mute a topic entirely — the single most-expected preference — in
which case **no notification row is inserted** for them (not a hidden row; the
badge and feed stay consistent for free). Mandatory floors pin `in_app` for
topics that must always land (e.g. Security). Reason preferences deliberately
do *not* narrow `in_app` — they are an external-channel refinement. A full
topic mute therefore means zero delivery on every channel; that is what mute
means, and mandatory floors are the guardrail.

### S-6 Coalescing

Coalescing keys on **(org, recipient, action, entity)** — the org axis kept
per DR 0001 and 0.13.0's fix (the v2 text predated that landing) — and the
same granularity as today's kind-based folding: edit bursts fold, comments on
the same entity stay separate. It still requires an org context and still
applies only when the resolved channels are in-app only. Note the coupling
this creates: a policy change that routes a topic externally also stops its
coalescing. Correct, but an admin surface must say so. `merge_body` stays the
producer's merge hook (status quo, per review); revisit after U-4 with usage.

### S-7 The delivery payload (revised per review — this is a breaking change)

`DeliveryPayload` renames with the columns and gains the new fields, in the
same I-2 breaking bump — one release, one vocabulary, no permanent skew:

```
delivery_id, notification_id, channel, recipient_user_id, org_id,
action, topic, nature, urgency, reason,        # was: kind, category, urgency, reason
title, body, link,                             # rendered (post-template, post-locale)
data, created_at
```

Adapters remain render-consumers: they receive final text plus taxonomy and
never touch templates. The v2 claim that the payload was "unchanged" is
withdrawn; its *role* is the stability contract, and this is its one rename.

### S-8 Admin scope

Admins manage: topics, the routing policy table, mandatory floors, template
wording/locales (platform rows now; org override rows when an org admin
surface exists), and delivery operations. Admins do **not** manage individual
actions (P-4).

## 4. Implications

- **I-1 Per-event runtime tuning requires a deploy — by design** (P-4;
  accepted explicitly in review).
- **I-2 One breaking bump.** `kind` → `action` and `category` → `nature`
  across the row, the feed API (`NotificationRead`, the `?category=` filter —
  kept as a deprecated alias for **one release**, per review), and the
  `DeliveryPayload` (S-7). Pre-1.0 breaking minor per RELEASING.md.
- **I-3 `register_kind()` becomes a shim, immediately.** The shim maps a
  registered kind's (category/urgency/reason) to axis defaults **and gains a
  `topic=` argument with a package-seeded default topic (`general`)** so a
  legacy emit always satisfies topic validation (v2's shim could not produce a
  topic and would have failed every legacy emit — review catch). Warns on use;
  removed one minor release later.
- **I-4 Validation shrinks to references.** `asas-notifications validate`
  checks: every `template=` reference resolves; every `topic=` exists; (later)
  every `action=` exists in the host's declared actions, when a host has such
  a list. No catalog sync to verify — there is no catalog.
- **I-5 What is lost, on the record:** per-event admin mute (P-4 — code fix
  instead), per-event analytics keyed by a curated catalog (action strings +
  topic serve instead), the v1 draft's catalog machinery (obsolete), and one
  coalescing-continuity window: renaming Teamy's kinds to imperative actions
  means a pre-deploy unread row and a post-deploy emit for the same entity
  briefly do not fold. Accepted.
- **I-6 Relationship to other work.** DR 0001 (tenancy): config tables follow
  its landed shared-with-overrides pattern (S-3); notification rows remain org
  data; coalesce identity keeps the org axis (S-6). The channel-cascade
  DR 0002 draft composes: cascade steps are a policy-layer concern attaching
  to S-3's policy table, not to actions. PR #20's read-path hardening is on
  main as 0.15.0 (re-landed as #37); this DR builds on it.

## 5. Specific updates to be made (the implementing PRs)

1. **U-1 Schema + emit.** Migration **`0004`**: rename `notification.kind` →
   `action` (nullable), `category` → `nature`, add `topic` (nullable on
   historical rows — no backfill: topic governs *future* routing and feed
   filtering; already-delivered rows do not re-route; new emits require it),
   `data` JSON, `template` ref. New `notify()` signature; `register_kind` shim
   per I-3; feed filter alias per I-2; `DeliveryPayload` per S-7; locale seam
   per S-4. Version bump (breaking minor). Revisit 0.15.0's composite feed
   indexes once `topic` becomes a filter axis.
2. **U-2 Topics + policy + resolution.** Topic and policy tables (S-3, with
   nullable `org_id`); precedence resolution (S-5) replaces `_channels_for()`;
   built-in fallback = today's rule; **equivalence tests**: empty tables
   reproduce 0.15.0 routing for the full Teamy catalog mapped to axes.
3. **U-3 Preferences.** Both preference tables (org-scoped), the AND rule,
   mandatory-floor exemption, in-app muting (no row inserted),
   `/me/notification-preferences` API — **topics only**; the reason table and
   resolution logic land here, its UI/API surface deferred until a host asks
   (per review). Two-org and both-engine tests.
4. **U-4 Templates + renderer.** Template table, emit-side (in-app) and
   dispatch-side (external) rendering, save-time variable validation, runtime
   fallback-to-stored-text (S-4), locale column landed with `en` resolved
   (Arabic in the localization follow-up DR, which also owns the read-time
   feed-rendering question).
5. **U-5 Admin API.** CRUD routers for topics/policy/templates behind host
   auth (`build_admin_router(get_session)`); platform rows in scope, org
   override rows schema-ready; audit hooks if the shared audit capability
   exists by then.
6. **U-6 Validate CLI** per I-4, CI-friendly exit codes.

Each phase is one reviewable PR, SQLite + Postgres green, version-bumped per
RELEASING.md. U-1/U-2 are the substance; U-3–U-6 are additive.

## 6. Resolved in review (was §6 "open questions") and what remains

Per the upstream review of v2, adopted in full: **(1)** `action` is NULL for
ad-hoc emits (no reserved value); **(2)** reason-preference table + logic land
in U-3, API exposes topics only; **(3)** `merge_body` stays; **(4)** the
`?category=` alias lives one release. The review's added question — is
`in_app` narrowable — is answered in S-5 (yes, by topic preference, with
mandatory floors; muted = not inserted).

Still genuinely open:

1. **The localization follow-up's scope**: read-time feed rendering vs the
   emit-time rendering this DR ships (S-4 keeps the door open by storing
   `template` + `data`).
2. **Org-admin authorization**: when org override rows get an admin surface
   (U-5+), who may write them is a host/access-package question this DR only
   flags.
