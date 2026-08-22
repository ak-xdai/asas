# DR 0002 (Asas): Multi-channel notifications — escalation with cancellation

Status: DRAFT for discussion · Author: ak@xdigit.ai (with Claude) · Date: 2026-08-22
Depends on: DR 0001 (tenancy contract) for the org axis of new tables.

## 1. Problem

asas-notifications must grow beyond in-app + email: SMS and WhatsApp are on the
product horizon. The naive way to add channels — fan every emit out to every
enabled channel at once — produces the experience everyone recognizes from
their bank: one action, then an SMS, a WhatsApp ping, and an email for the same
event. Notification spam is not a volume problem, it is a *coordination*
problem: the channels don't know about each other, and none of them knows the
user already saw the thing.

Two requirements fall out:

1. **Cross-channel suppression** — if the user has seen a notification in one
   place, the other channels back off.
2. **Read-state sync** — engaging with a notification through *any* channel
   (opening it in-app, clicking the email button) marks it read everywhere.

Preferences ("never SMS me about mentions") are necessary but not sufficient:
a user with every channel enabled should still not get the email for something
they read in-app two minutes after it happened.

## 2. Current state

What the package already has, and why this DR is small:

- **Per-channel delivery rows** (`NotificationDelivery`), created inside
  `notify()` and sent by a dispatcher that is a correct Transactional Outbox /
  Polling Publisher: rows ride the producer's transaction, an after-commit hook
  gives latency, a sweep gives correctness, and a rows-affected CAS claim works
  on both engines. The send decision already happens at *dispatch* time — the
  exact place a cancellation check belongs.
- **A routing seam** — `_channels_for(category, urgency, reason)` with a
  comment explicitly reserving the per-user preference layer, and a channel
  adapter registry (`adapter_for`) behind which new channels slot.
- **Read/archive axes** on the notification row (`read_at`, `archived_at`).
- Known dispatcher gaps from the prior-art review (Atlas): no retry backoff,
  no delivery idempotency key. Both are prerequisites here (see rollout).

## 3. Design

### The cascade (rules E1–E7)

**E1 — Channels are a sequence with checkpoints, not a parallel blast.** A
kind's routing resolves to an ordered list of *escalation steps*:
`[(channel, delay, cancel_on)]`. The in-app row is step zero, always immediate
and free. Example policy for a `normal` mention:
`in-app now → email +10 min (cancel on read) → whatsapp +2 h (cancel on read)`.

**E2 — Deliveries are scheduled, not immediate.** Each escalation step becomes
a delivery row stamped `not_before = emitted_at + delay`. The dispatcher's
claim query gains one predicate (`not_before <= now`). This is the same column
the digest feature needs — one schema change serves both.

**E3 — Cancellation is checked at send time, against our own read-state.**
When the dispatcher claims a due delivery, it first re-reads the parent
notification: if the step's `cancel_on` state is satisfied (`read_at` set, or
`seen_at` for softer steps), the delivery is marked `skipped` — a terminal
status distinct from `sent`/`failed`, visible in the delivery log. This is the
whole "delivery note → back off" mechanism, keyed on the one signal we fully
control. Downstream receipts (email opens, SMS DLRs, WhatsApp read receipts)
are *secondary* signals only — see D1 for why they cannot be the mechanism.

**E4 — Seen and read are different states.** `seen_at` (the feed was rendered
with this row in it) is added beside `read_at` (the row was opened/acted on).
Escalation steps choose which state cancels them: a badge-count push might
cancel on `seen`; an email cancels on `read`. Feed listing marks `seen_at`;
explicit interaction marks `read_at` (the existing semantics).

**E5 — Read-state syncs inward through links.** Every outbound message links
through the host with the notification id; the host's click-through route
marks the row read on the way to the content. Reading in-app cancels pending
deliveries via E3 — no outward sync needed; the email simply never sends.
(Host wiring, documented in the package README; not package code.)

**E6 — Urgency classes decide what escalates at all.**
- `low` — in-app only, never escalates (unchanged: ambient activity never
  emails you).
- `normal` — in-app immediately; external channels only as delayed,
  cancellable escalation steps.
- `high` — in-app + first external channel immediately; remaining steps
  delayed and cancellable.
- `critical` (new, explicit) — all configured channels immediately, no
  cancellation. This is the 2FA/fraud-alert class: the bank's multi-blast is
  *correct* for it — the failure mode this DR kills is `normal` events
  masquerading as critical.

**E7 — Preferences are the user's veto, applied in the pipeline.** An opt-out
grid — `notification_preference(user_id, org_id, category-or-kind, channel,
enabled)` — with absence = default-on and most-specific-wins resolution
(kind beats category), enforced inside `_channels_for`, never by producers.
Per-entity mute ("unwatch this record") is a preference row with an entity
key, not a new mechanism. Preferences filter which steps exist; the cascade
(E1–E3) then de-spams whatever survives. A `critical` kind may be declared
non-optoutable by the host. The table is `tenant-owned` under DR 0001 T1/T5:
`org_id` stamped per T4, part of the preference row's identity.

### Schema delta

| Change | Where | Serves |
|---|---|---|
| `not_before: datetime NULL` | `notification_delivery` | E2 scheduling, digests, retry backoff |
| `skipped` terminal status | `notification_delivery.status` | E3 cancellation audit trail |
| `seen_at: datetime NULL` | `notification` | E4 |
| `notification_preference` table | new | E7 |
| escalation steps on `KindSpec` / routing config | code, not schema | E1, E6 |

## 4. Prior art

This is the cracked pattern, transcribed:

- **Knock "delays + cancellation"**, **Courier escalation paths**, **MagicBell
  "smart delivery"** — all are E1–E3 verbatim: schedule the next channel,
  cancel when the inbox reports engagement. Knock and Novu inboxes both carry
  the E4 seen/read distinction.
- **Enforcement in the pipeline, not in producers** (Courier's model): the
  reason E7 lives inside `_channels_for`. Producers keep calling `notify()`
  with no channel knowledge — the property the package already has and must
  keep.
- **Why not delivery receipts as the mechanism (D1)**: email opens are
  poisoned by Apple Mail Privacy Protection's prefetch (an "open" may be no
  human); SMS DLRs prove the phone received it, not that anyone read it;
  WhatsApp Business API read receipts are real but user-disableable. The
  field's converged answer is: cancel on *your own* observable engagement
  state; treat provider receipts as optional secondary cancellation signals
  where they exist.
- **Novu digest keys** — E2's `not_before` is the same mechanism their digest
  step uses; landing it for escalation makes windowed digests a follow-on, not
  a new design.

## 5. Decisions

- **D1 — Cancellation keys on our read-state, not channel receipts.** Receipts
  are weak, asymmetric, and provider-specific (§4). Where a receipt exists
  (WhatsApp read), a host may feed it back as a secondary cancel signal
  through the delivery-status webhook path; it is never required.
- **D2 — Preferences veto, cascade de-spams.** Neither alone is sufficient:
  preferences don't know the user just read the thing; the cascade doesn't
  know the user never wants SMS. Both compose in the pipeline.
- **D3 — `critical` bypasses everything by design**, and is a per-kind
  declaration reviewed like a permission — the class exists precisely so that
  nothing else needs to blast.
- **D4 — Escalation config lives on the kind registry** (code/wiring), not in
  the database. Hosts declare policy at boot exactly as they declare kinds
  today; per-user variation is E7's job, not per-kind-per-user routing rows.
- **D5 — In-app remains the anchor channel.** Step zero always exists (it is
  the read-state carrier the cascade checks). A kind that must not appear
  in-app is out of scope for the cascade and routes as `critical` or via a
  host-direct send.
- **D6 — Prerequisites are defect-shaped and land first**: dispatcher retry
  backoff (`next_attempt_at`, full jitter) and a deterministic per-delivery
  idempotency key. Escalation multiplies scheduled sends; scheduling onto a
  dispatcher that hammers failing providers and can double-send would amplify
  both defects.

## 6. Rollout

1. **PR-N1 — dispatcher hardening** (prereq): retry backoff + idempotency key
   passed to adapters. Defect-shaped; shippable regardless of this DR's fate.
2. **PR-N2 — the cascade core**: `not_before`, `skipped` status, escalation
   steps on the kind registry, cancellation check in the dispatcher
   (E1–E3, E6). Two-org tests per DR 0001 T10.
3. **PR-N3 — seen/read split + sync recipe**: `seen_at`, feed marking, the
   click-through mark-read host recipe in the README (E4, E5).
4. **PR-N4 — preferences**: the opt-out grid + resolution in `_channels_for`,
   per-entity mute (E7).
5. **PR-N5 — digests**: windowed batching reusing `not_before` + a
   `digest_key`.
6. **PR-N6 — new channel adapters**: SMS/WhatsApp behind `adapter_for`, plus
   the host webhook path for provider status → secondary cancel signals.
   Deliberately last: adding WhatsApp *before* the cascade exists is exactly
   how the bank experience gets built.

## 7. Open questions

1. Default delays per urgency (10 min email / 2 h second channel is a starting
   point — product call).
2. Should email cancel on `seen` or `read`? (`read` proposed: a glimpsed badge
   shouldn't suppress the email; opening the item should.)
3. Which kinds are `critical` at launch, and is host-side non-optoutability
   (E7) acceptable to the owner?
4. Does Teamy want per-entity mute at the same time as preferences, or later?
5. WhatsApp Business API sender identity and template approval are host/ops
   concerns — confirm they stay entirely outside the package.
