# asas-notifications

A generic notification engine: producers register event **kinds** and call
`notify` inside their own transaction, so a notification exists if and only if
the domain change committed — the insert IS the enqueue. The in-app feed is the
`notification` row itself (`build_router` serves it); external channels go
through the `notification_delivery` outbox and registered **channel adapters**
(email, chat, …). Dispatch is duplicate-safe under concurrent passes and
at-least-once overall.

Table-owning + router variant of the Asas host contract: 2 tables, no `seed()`,
org/user refs are plain ints with no host FKs.

**→ [Full documentation](../../docs/packages/asas-notifications.md)** — wiring,
API reference, data model, invariants, and failure modes.

Extracted from Teamy (notifications epic WXL-209/WXL-222, TEAMY-475 dispatch
hardening, TEAMY-693 archive axis; extraction epic TEAMY-466, design record
0017).
