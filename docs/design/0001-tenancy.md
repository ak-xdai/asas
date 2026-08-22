# DR 0001 (Asas): Multi-tenancy — one contract for the org axis

Status: DRAFT for discussion · Author: ak@xdigit.ai (with Claude) · Date: 2026-08-22

## 1. Problem

Asas packages implement org scoping ("tenancy") individually, with no shared
definition of what the org axis means for reads, writes, background context, or
security decisions. A full audit of all ten packages (2026-08-22) found that the
four packages that improvised the axis each got a different piece wrong, while
the two that treated it as a first-class design input got it right. The failures
are not random bugs; they are the absence of a contract.

### Verified defects (the motivating evidence)

| # | Package | Defect | Severity |
|---|---------|--------|----------|
| T-1 | asas-lookups | Admin writes (`update_value`, `deprecate_value`, `add_alias`, `remove_alias`, `merge_values`) locate their target with the read-scoped `_value_by_code` (org row OR global row). An org-context caller with no override row mutates the **platform-global row** — every tenant sees the edit; a deprecation removes the value for everyone. `service.py:431+` | Critical |
| T-2 | asas-notifications | `Notification.org_id` is `int` NOT NULL with no default (`models.py:60`), but the context resolver contract is "(user_id, org_id) … or None outside a request" (`service.py:73`). Any emit from a background job/CLI/boot sweep inserts NULL → `IntegrityError` at flush — and it rides the producer's transaction, so the domain write dies with it. | Critical |
| T-3 | asas-access | `mac_allows` resolves an org-less record's org from **the caller** (`mac.py:152-155`). An org-2 subject looking at an org-1 record whose child row carries no `org_id`: markings are looked up under org 2 → none found → marked-only record treated as unclassified → **fail open**. Same substitution compares classification ranks against the caller's org catalog — per-org rank integers are not comparable across orgs. | Critical |
| T-4 | asas-workflow | `ProcessInstance.org_id` exists (`models.py:177`) but `open_instance` has no way to set it — every engine-opened instance has `org_id=None`, so both `registry.resolve_floor(session, instance.org_id)` call sites (`engine.py:321,360`) always resolve the **unscoped** floor. The floor exists precisely so "a workflow in one org must never land in another org's approval inboxes" (`registry.py:155`). | High |
| T-5 | asas-lookups | `seed_lookups`'s existence check has no `org_id IS NULL` predicate (`seed.py:216-220`): an org-minted row with the same code suppresses the platform default forever; re-seeding never heals it. | High |
| T-6 | asas-notifications | Coalescing selects its merge target with no org predicate (`service.py:194-209`): an emit in org 2 can fold into (and overwrite) an org-1 row for the same `(user, kind, entity)` when entity ids are not globally unique. | Medium |
| T-7 | asas-lookups | `active_only` filters in SQL **before** `_prefer_org` collapses shadows (`service.py:162-167`): a tenant's *deprecated* override is dropped by the filter, un-shadowing the platform row — the value the tenant retired reappears with the platform label. | Medium |
| T-8 | asas-lookups | `get_value_read`'s supersede follow (`session.get(LookupValue, value.superseded_by_id)`, `service.py:205`) is unscoped: a pointer into another org's row leaks that org's labels. | Medium |
| T-9 | asas-access | `ensure_clearance_levels` invalidates the **process-global** level cache before its rows are committed (`mac.py:216-221`); a rollback leaves phantom levels cached for every session until the next invalidation. | Medium |

### The two packages that got it right (internal prior art)

- **asas-jobs**: `org_id: Optional[int]` with *explicit* NULL semantics ("platform
  job"), and the org axis folded into every uniqueness invariant — separate
  partial unique indexes for org-scoped and global dedupe keys and schedules
  (`models.py:19-26`). Dedup can never collide across tenants because the
  *database* owns the invariant.
- **asas-search**: the deep tier takes an `org_of(session, user)` callback and
  **fails closed** — "No org ⇒ no deep hits … never search across tenants"
  (`fts.py:132-141`); `org_id` is stamped into every indexed row at write time
  and every query predicates on it.

These two define the house style the contract below generalizes.

## 2. Goals and non-goals

Goals:

1. One written contract (this DR) that every package conforms to, like the
   five-part host contract in the README.
2. **Fail loud or fail closed, never open**: missing org context must never
   widen visibility, mutate shared data, or crash the producer's transaction
   with a constraint error.
3. Single-tenant hosts stay zero-config: every rule below degrades to a no-op
   when no resolver is configured.
4. Portable enforcement: every rule expressible on SQLite and Postgres alike
   (application-level predicates + partial unique indexes — both engines
   support them). Engine-specific hardening (Postgres RLS) may be layered on
   by hosts, never relied on by packages.
5. Minimal churn: the contract legitimizes what jobs/search already do and
   corrects the other four packages to match.

Non-goals:

- Sub-org hierarchies (the `parent_org_id` walk-up sketched in
  `lookups/service.py:52-59`). The contract reserves the semantics; nothing
  implements the walk yet.
- Schema-per-tenant or database-per-tenant isolation. Asas is
  shared-schema-with-tenant-column by construction; see §5 prior art.
- A shared `asas-core` package. The contract is convention + identical code
  shapes, same as the host contract. (§7 revisits the trigger condition.)

## 3. Concepts

- **Org**: the tenant, a host-defined opaque `int`. Packages never FK it
  (extraction rule) and never interpret it beyond equality.
- **Org context**: the caller's org for the current unit of work, resolved from
  the `Session` by a host-configured hook. `None` means *no tenant context* —
  boot, seeds, CLI, background sweeps — which is a **platform** scope, not a
  wildcard.
- **Table classes** — every table in a tenancy-aware package declares exactly
  one:
  - **`tenant-owned`**: every row belongs to one org (or explicitly to the
    platform). `org_id: Optional[int]`, `NULL` = platform-scope row with
    documented meaning. *(jobs, notifications, workflow instances, search
    documents, access grants/levels/markings.)*
  - **`shared-with-overrides`**: `NULL` rows are platform defaults visible to
    every tenant; an org row with the same natural key **shadows** the global
    row for that org only. *(lookup values.)*
  - **`org-agnostic`**: no org column; the package is tenancy-blind and says
    so. *(validation rules, ratelimit buckets — hosts encode org in the
    ratelimit key string; workflow/lookup *definitions* today.)*

## 4. The contract

Rules are numbered for citation in PRs (like the host contract's five parts).

**T1 — Declare the axis.** Each package's README states, per table, its class
from §3 and what `org_id IS NULL` means there. Undeclared = org-agnostic, and
the package must then contain no `org_id` column at all.

**T2 — One resolver shape.** Org context comes from one configured hook per
package, uniform in shape and semantics:
`configure_org_resolver(fn: Callable[[Session], Optional[int]]) -> None`,
default unconfigured. Packages that also need the acting user (notifications)
keep their `(user_id, org_id)` context resolver, but the org element obeys the
same semantics. Resolvers are consulted **per operation**, never cached at
import/configure time, and must be cheap. The *documented host convention* is
`session.info["org_id"]` (`lambda s: s.info.get("org_id")`) — the one carrier
that behaves identically in request handlers, asas-jobs workers, and tests;
contextvar-backed resolvers remain equally valid.

**T3 — Read scoping.**
- `shared-with-overrides`: no context → `org_id IS NULL` rows only; with
  context → `org_id IS NULL OR org_id = :org`, then collapse natural-key
  duplicates preferring the org row (`_prefer_org`). Shadowing is by identity,
  not by status: a deprecated org override still shadows its global row —
  status filters apply **after** the collapse (fixes T-7).
- `tenant-owned`: queries predicate on `org_id = :org` when context exists;
  with no context, a package-level choice — either platform rows only
  (`org_id IS NULL`) or an explicit "unscoped admin read" API, never a silent
  union of all tenants.
- Cross-row pointers (supersede, parent) resolve **through the same scope** as
  the row that holds them; a pointer that lands outside the caller's visible
  set behaves as absent (fixes T-8).

**T4 — Write scoping.** The write path never reuses the read path's row
selection (the root cause of T-1).
- `shared-with-overrides`: with org context, mutations target **only the
  caller's org row**. When no override exists, the mutation
  **copies-on-write**: it materializes an org override row (copying the global
  row's identity) and applies the change to the copy. The global row is
  mutable only from a no-context (platform) session. *(Decision D2, §6,
  records the rejected alternative.)*
- `tenant-owned`: inserts stamp `org_id` from, in order: an explicit `org_id=`
  parameter → the resolver → the package's declared no-context behavior. Where
  the column is NOT NULL and neither source produced a value, the package
  raises a clear `ValueError`/`LookupError` **before** flush — a producer bug
  must not surface as an engine-specific `IntegrityError` mid-transaction
  (fixes T-2). `org_id` is never read from a request payload, and it is
  **immutable after insert** (the acts_as_tenant convention) — moving a row
  between tenants is a delete + recreate, not an update.

**T5 — The org axis is part of every identity.** Any uniqueness, dedup,
coalescing, or upsert key on a tenant-owned or shared-with-overrides table
includes the org axis, enforced in the database where the engines allow it
(partial unique indexes — the asas-jobs pattern) and always in the query
predicate (fixes T-6; the lookup alias unique constraint lands under this
rule too).

**T6 — Security decisions fail closed on the org axis.** A security check
(access grants, MAC, search visibility) that cannot resolve the *record's* org
from the record itself or an explicit parameter must **deny** (or return
nothing) — never substitute the caller's org, and never compare rank/level
values across two orgs' catalogs (fixes T-3; generalizes what `mac.py` already
does for the caller-less case and what `fts.py` does for deep search).

**T7 — No-context work is platform work.** Seeds, migrations, and boot sweeps
run without a resolver and may touch only `org_id IS NULL` rows; their
idempotency checks predicate on it explicitly (fixes T-5). Background emits
that act *for* a tenant must carry the org explicitly (T4).

**T8 — Org-derived caches.** Any process-global cache keyed by org data is
(a) keyed by org, and (b) invalidated **after** commit, not at mutation time —
an uncommitted change must not poison other sessions through the cache
(fixes T-9). Response caching (ETags) includes the org in the tag, as
`lookups/router.py` already does.

**T9 — Storage keys carry the org prefix.** Blob keys for tenant data follow
`orgs/{org_id}/…` (the existing Teamy convention); `delete_prefix("orgs/{id}")`
remains the tenant-erasure primitive. The package stays org-blind; the
convention is the host's obligation and is now written down.

**T10 — Two-org tests.** Every tenancy-aware package's suite gains a shared
test shape: two orgs + the platform, asserting (a) reads don't leak across
orgs, (b) an org write never mutates global or foreign rows, (c) no-context
behavior matches the declaration, (d) identity keys (T5) hold per org — on
both engines. The bugs in §1 all become permanent regressions under this
recipe.

## 5. Prior art

A research pass (2026-08-22) over the multi-tenancy field. Short version: the
asas model is an independent re-derivation of well-attested designs; the
contract above mostly *names* what mature frameworks already converged on, and
borrows the few conventions asas lacks.

**Isolation models.** The consensus (Frontegg, and every current
RLS-vs-schema-vs-DB comparison) is that shared-schema-with-tenant-column is the
right default for small-to-mid SaaS; schema-per-tenant is a Postgres-only
technique (`search_path`) with per-schema migration pain, and
database-per-tenant is the enterprise-tier escalation. Two notes matter for
asas: (a) shared-schema is the **only** model expressible identically on SQLite
and Postgres, and (b) the Session-only contract preserves a free escape hatch —
a host can hand a package a Session bound to a per-tenant engine (including the
production-attested per-tenant-SQLite-file pattern) without any package change.
→ D1 confirmed.

**Postgres RLS.** The canonical recipe (AWS Database Blog; Supabase docs) is a
policy over a transaction-scoped GUC (`SET LOCAL app.current_org`), with
`WITH CHECK` on writes, a non-owner connection role, and
`FORCE ROW LEVEL SECURITY` — requirements that reach into host operations a
git-installed library cannot dictate, and SQLite has no RLS at all. Dual-engine
projects therefore standardize on **application-level filtering as the primary
mechanism, RLS as optional host-side defense in depth**. The contract keeps
that layerable: one uniform column name (`org_id`) across all packages so a
host can write generic policies, and the `NULL OR = :org` visibility rule maps
1:1 onto a `USING` clause. A host RLS recipe belongs in an appendix, explicitly
out of contract.

**Framework contracts.** The closest analogs, and what each contributes:

- **ABP Framework multi-tenancy / ASP.NET Boilerplate `IMayHaveTenant`** — the
  exact `NULL = host/platform` semantics of asas's `org_id`, including the key
  insight the contract adopts in T7: a null tenant context is a *first-class
  platform scope that sees only null rows* — deny-by-scoping, not "unfiltered".
  ABP's explicit `Change(id)` / filter-disable escape hatches are the named
  affordances T4's "platform session" mirrors.
- **acts_as_tenant (Rails)** — three conventions T4 borrows: `tenant_id` is
  auto-stamped from context on create (never accepted from the caller's
  payload); the column is **immutable** after insert; and `require_tenant`
  (fail-closed when context is missing) exists as an opt-in strict mode.
  Its `has_global_records` is the read-side OR-with-global rule of T3.
- **django-multitenant (Citus)** — a cautionary contract: with no tenant set,
  queries run **unscoped** (fail-open), which is exactly the failure class
  T3/T6 forbid. Its composite-FK machinery is Citus-motivated; ignored.
- **django-tenants** — schema-per-tenant via `search_path`; structurally
  incompatible (Postgres-only, middleware-owned). Ignored.
- **SQLAlchemy `do_orm_execute` + `with_loader_criteria`** — the native
  mechanism for auto-injecting tenant criteria into every ORM select,
  propagating into relationship loads. Available to packages as optional
  belt-and-suspenders on their own models, but it misses Core/textual
  statements — so explicit `_org_scoped`-style filtering stays the primary,
  testable mechanism, and suites must pass with the listener disabled.

**Context propagation.** contextvars is the standard carrier under async;
SQLAlchemy's sanctioned per-session channel is `Session.info`. The
resolver-takes-a-Session contract composes with both — the documented host
convention should be `session.info["org_id"]`, the only carrier that behaves
identically in request handlers, job workers, and tests.

**Defaults-with-overrides.** The shadowing pattern is well-attested — ABP's
null-tenant rows, Salesforce's per-org customization over shared standard
objects, "tenant overrides" in BI products, WorkOS's template-plus-deltas RBAC.
Two field consensuses the contract encodes: nobody lets a tenant mutate the
shared row in place (universally treated as a cross-tenant write bug → T4), and
"hide this global value for my org" is expressed as an org-owned **tombstone**
(inactive shadow row) — which is why T3 shadows by identity and filters status
*after* the collapse.

## 6. Decisions

*(Each records the choice, the alternative, and why — final wording pending
the research pass.)*

- **D1 — Isolation model**: shared schema + tenant column (status quo),
  because the dual-engine rule and "libraries only see a Session" preclude
  schema-per-tenant and RLS-as-the-mechanism. The research pass confirms this
  is the field's default for this product size, and the Session-only contract
  keeps two host-level escape hatches free: per-tenant databases (a Session
  bound to a per-tenant engine — packages unchanged) and Postgres RLS as
  defense in depth (uniform `org_id` naming makes host policies generic; a
  host recipe — `SET LOCAL`, non-owner role, `FORCE ROW LEVEL SECURITY`,
  `USING` + `WITH CHECK` — belongs in an appendix, out of contract).
- **D2 — Org edits of shared reference data**: copy-on-write override (T4)
  vs rejecting with 403 "platform value". Copy-on-write is the natural
  completion of the shadowing model the schema already encodes (partial
  uniques for global + org rows) and is what "an org row shadows a global row"
  already promises; rejection would make the override feature admin-only. The
  field consensus supports it — no surveyed system lets a tenant mutate the
  shared row in place — and "hide this global value for my org" is expressed
  the same way: an org-owned **tombstone** (deprecated shadow row), which is
  what T3's shadow-by-identity ordering makes work.
- **D3 — `notifications.org_id`**: keep NOT NULL + fail-loud + explicit
  parameter (T4), vs relaxing to nullable platform rows. Keeping NOT NULL
  preserves "a notification always has a tenant" (the WXL-218 mapping) and
  the migration cost of relaxing is real; fail-loud converts the crash into a
  producer-side contract error.
- **D4 — Record org in MAC**: require the record (or an explicit argument) to
  carry it; deny otherwise (T6). The alternative — trusting the caller's org —
  is exactly bug T-3.
- **D5 — Shared kernel**: still no `asas-core`. The resolver shape (T2) is now
  repeated ~5×, which arguably meets the README's "third repetition" trigger —
  but a conventions DR + identical code blocks keeps packages standalone
  (the property the git-subdirectory install depends on). Revisit after the
  remediation PRs land.
- **D6 — Automatic ORM scoping**: SQLAlchemy's `do_orm_execute` +
  `with_loader_criteria` can inject the org predicate into every ORM select on
  a package's own models (propagating into relationship loads). Permitted as
  optional belt-and-suspenders, with two conditions: suites must pass with the
  listener disabled (explicit `_org_scoped` filters remain the primary,
  testable mechanism), and it is never trusted for Core/textual statements,
  which it does not cover.
- **D7 — Strict mode**: an opt-in `require_org` (the acts_as_tenant
  `require_tenant` analog) that makes a `None` resolver result raise instead
  of meaning platform scope, for hosts that never want implicit platform
  reads on request paths. Default off — the ABP-style "None = platform scope"
  stays the contract default because it is a *narrower* view, not a wider one.

## 7. Remediation plan (the implementing PRs)

Ordered so each PR is independently shippable and dual-engine tested:

1. **PR-T1 (this DR + declarations)**: add this document; per-package README
   table-class declarations (T1); the two-org test recipe as a shared snippet
   in each suite (T10) — initially exercising only the already-correct
   packages (jobs, search).
2. **PR-T2 (lookups write scoping)**: `_value_by_code_for_write` (org-row-only
   lookup), copy-on-write in `update_value`/`deprecate_value`/alias ops/
   `merge_values` (T4/D2), scoped supersede resolution (T3), shadow-then-filter
   ordering (T3), seed predicate (T7). The largest PR; carries T-1, T-5, T-7,
   T-8.
3. **PR-T3 (notifications)**: explicit `org_id=` on `notify()`, fail-loud
   pre-flush check (T4), org predicate in the coalesce lookup (T5). Carries
   T-2, T-6.
4. **PR-T4 (workflow)**: `org_id` parameter on `open_instance` (resolver
   fallback per T4), threading it to both floor call sites. Carries T-4.
5. **PR-T5 (access)**: record-org requirement + fail-closed in `mac_allows`
   (T6), post-commit cache invalidation (T8). Carries T-3, T-9.
6. **PR-T6 (docs sweep)**: T9 storage-key convention into asas-storage's
   README; alias unique constraint under T5 lands with the separate
   lookup-alias work.

Each remediation PR adds its §1 defects as two-org regression tests first
(red), then the fix (green), on both engines.

## 8. Open questions for review

1. Is copy-on-write (D2) the intended semantics for org admin edits, or should
   org admins be blocked from touching platform codes entirely?
2. Should `tenant-owned` reads with no context default to "platform rows only"
   or "error" for feed-shaped queries (notifications lists take a user id —
   is user scoping alone acceptable when the host guarantees globally-unique
   user ids)?
3. Does Teamy rely anywhere on the current (buggy) global-mutation behavior of
   lookups admin writes — i.e., is anyone's "admin edit" today actually a
   platform edit on purpose?
