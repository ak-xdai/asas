# DR 0002 (Asas): Package documentation — one shape, derived from the host contract

Status: DRAFT for discussion · Author: ak@xdigit.ai (with Claude) · Date: 2026-08-25

## 1. Problem

Asas ships ten packages behind a single, precisely-specified five-part host
contract (README, "The host contract"). The contract is the repo's best asset:
it tells a host integrator that every package is wired the same way. The package
documentation does not follow it. Each README was written as an extraction
narrative — why this module exists, what Teamy epic it came from — by the person
who extracted it, at whatever length that person felt like. The result is that a
host integrator cannot answer "how do I use this package" from the docs, and
cannot answer it *the same way twice* across two packages.

This is not a tidiness complaint. It is measurable.

### Verified state (2026-08-25, `main` @ 3edd2d5)

Coverage below is the share of each package's declared public surface
(`__all__`, excluding dunders) that appears anywhere in its README.

| Package | README lines | Public symbols | Documented | Coverage |
|---|---:|---:|---:|---:|
| `asas-lookups` | 8 | 5 | 0 | **0%** |
| `asas-validation` | 10 | 13 | 3 | 23% |
| `asas-access` | 64 | 59 | 16 | 27% |
| `asas-notifications` | 89 | 20 | 9 | 45% |
| `asas-mcp` | 45 | 4 | 2 | 50% |
| `asas-search` | 45 | 14 | 7 | 50% |
| `asas-jobs` | 46 | 17 | 10 | 59% |
| `asas-storage` | 77 | 11 | 8 | 73% |
| `asas-ratelimit` | 38 | 9 | 8 | 89% |
| `asas-workflow` | 56 | — | — | see D-4 |
| **Total** | | **152** | **63** | **41%** |

### The defects behind the number

| # | Defect | Severity |
|---|---|---|
| D-1 | **`asas-lookups` documents none of its five exported symbols** — and those five (`migrate`, `seed`, `build_routers`, `configure_org_resolver`, `Routers`) *are* the host contract. The package with the least documentation is the pilot extraction every other package was modelled on. | Critical |
| D-2 | **No package documents its contract variant.** The root README classifies each package (table-owning, router-less, protocol-only, dialect-branched), but no package doc states which of the five parts it implements and which it deliberately omits. `asas-notifications` has no `seed()`; nothing says so. An integrator discovers absence by `AttributeError`. | High |
| D-3 | **No package has an API reference.** 89 undocumented symbols across the repo, including every model, enum, exception and protocol. `asas-access` alone hides 43, among them `mac_allows` and `can_view_field` — the functions a host calls to make security decisions. | High |
| D-4 | **`asas-workflow` declares no `__all__` at all.** It has no stated public surface, so "documented" is undefined for it and any coverage check passes vacuously. A package with no declared API cannot have a stable one. | High |
| D-5 | **Failure modes are undocumented.** `SkipDelivery`, `RangeNotSatisfiable`, `MAX_ATTEMPTS`, `STALE_CLAIM_SECONDS` and the retry semantics around them exist in code and in nobody's README. A host cannot write correct error handling against these packages. | Medium |
| D-6 | **No integration example is runnable.** Snippets show fragments of boot wiring; none shows migrate → configure → register → call end to end. Every host reconstructs the same sequence from source. | Medium |
| D-7 | **Documentation drifts silently.** Nothing ties a README to the code it describes, so a rename lands green. `asas-notifications` is at 0.11.1 and its README still describes the WXL-222 shipping state. | Medium |

### What is already right, and should be generalized

`asas-notifications` (45%, the third-lowest coverage) nonetheless has the best
*prose* in the repo: it states its invariants explicitly — actor exclusion,
urgency routing, coalescing, CAS-claimed dispatch, the read/archive axis
separation. Those are the facts a reader cannot recover from the code in
reasonable time, and they are exactly what an extraction narrative is good at
capturing. The problem is not that this content exists. It is that it exists
*instead of* reference material, and only in the packages whose author felt like
writing it.

The fix is not to replace the narratives. It is to give every package a fixed
shape with a slot for them.

## 2. Goals and non-goals

Goals:

1. One documented shape per package, **derived from the five-part host
   contract**, so that reading any two package docs exercises the same muscle.
2. **Absence is documented.** A package that does not implement a part of the
   contract says so in the same place the others say they do.
3. **Reference and rationale coexist.** The invariants section is mandatory, not
   a nicety; so is the symbol table.
4. **Machine-verifiable.** Coverage is checked in CI, so docs fail loud like
   everything else in this repo, rather than rotting quietly.
5. Cheap to write and cheap to review. One page per package, one PR per package.

Non-goals:

- **No documentation site.** No Starlight, no Sphinx, no autodoc build. Markdown
  in the repo, rendered by GitHub. A site is a separate decision and should not
  gate the content. (Teamy's Starlight setup is available if this is revisited.)
- **No generated API docs.** The symbol table is hand-written, because the value
  is in what a signature does not say — invariants, ordering, transaction
  expectations. Generated docs would restate the types and lose the point.
- **No `asas-core`.** This DR describes a convention, exactly as the README
  says of the host contract itself: "the contract above is a convention, not a
  package."
- Not a rewrite of existing prose. Existing narrative moves into the Invariants
  and Design notes sections largely intact.

## 3. The contract

Every package gets one page, `docs/packages/<package-name>.md`, with these
sections in this order. Sections are mandatory; a section with nothing to say
says so in one line ("This package owns no tables.") rather than being omitted,
so that a reader scanning two packages compares like with like.

| # | Section | Contents |
|---|---|---|
| 1 | **What it is** | One paragraph: the problem the package solves, and for whom. No history. |
| 2 | **Install** | The pinned git-subdirectory line, and the current version. |
| 3 | **Host contract** | The five parts as a table: implemented / not applicable, with the signature. This is where D-2 is fixed. |
| 4 | **Wiring** | One runnable block: `migrate` → `configure_*` → `register_*` → `include_router`, in boot order. |
| 5 | **Usage** | The two or three calls a producer actually makes, in context. |
| 6 | **API reference** | Every symbol in `__all__`: signature, parameters, returns, raises. Grouped by role (functions, models, enums, protocols, exceptions). |
| 7 | **Data model** | Tables owned, their columns, and the no-host-FK convention. Or "owns no tables." |
| 8 | **Migrations** | Chain name, version table, adopt-or-create behavior, dual-engine notes. |
| 9 | **Invariants** | The rules the code enforces that a reader cannot infer from signatures. Mandatory. |
| 10 | **Failure modes** | Exceptions raised, retry/attempt semantics, what fails loud vs closed. Fixes D-5. |
| 11 | **Testing against it** | How a host tests, and how to run the package's own suite on both engines. |
| 12 | **Design notes** | Origin epic, related DRs, deliberate omissions (e.g. `resolved_at`). Where the extraction narrative lives. |

### Rules

- **The page is the source of truth; the README is a pointer.** Each package
  README shrinks to a title, the one-paragraph "what it is", and a link to its
  page. Content lives in exactly one place.
- **Every `__all__` symbol appears in section 6.** This is the checked rule.
- **Signatures are copied, not paraphrased**, including keyword-only markers and
  defaults.
- **Invariants cite behavior, not implementation.** "A notification is never
  delivered to its own actor" — not "line 168 filters `actor_user_id`."
- **Known defects are linked, not hidden.** Where a DR records an open defect in
  this package (DR 0001's T-2, T-6 for notifications), the affected section
  links to it. Documenting the intended contract while the code violates it is
  how a doc becomes a lie.

## 4. Verification

A CI job runs `tools/check_docs.py`, which for each package parses `__all__`
from its `__init__.py` and asserts every non-dunder symbol appears in
`docs/packages/<name>.md`. Failure lists the missing symbols.

The check is deliberately shallow. It cannot tell whether a description is
*good*; it can tell whether a symbol was added and never written up, which is
the drift that actually happens (D-7). Depth is review's job.

Two consequences worth accepting up front:

- **`asas-workflow` must declare `__all__`** before the check means anything for
  it (D-4). That is a small code change and a good one regardless.
- **The check is gating from the moment a package's page lands**, not before.
  Packages are added to the checked set one at a time, as their page merges, so
  the build never goes red waiting on documentation that has not been written.

## 5. Rollout

One PR per package, so review stays small and a stalled package blocks nothing.

1. **This DR + `asas-notifications` page + `check_docs.py`** (this PR). The
   worked example proves the shape against the hardest package: two tables, a
   router, an outbox, adapters, coalescing, CAS-claimed dispatch, and a
   two-axis feed state. If the template survives notifications, it survives
   everything.
2. **`asas-lookups`** next, as the inverse test: the thinnest doc, the pilot
   package, 0% coverage, and the one an integrator meets first.
3. Remaining seven in any order; `asas-access` last, as it is by far the largest
   surface (59 symbols) and benefits from the template being settled.

Ordering note: DR 0001 (tenancy) changes the org-axis surface of several
packages, including the `configure_*` hooks this template documents in section
3. Packages whose tenancy behavior DR 0001 corrects should be documented after
it lands, or their pages will describe a surface that is about to change.
Notifications carries two DR 0001 defects (T-2, T-6); its page links them rather
than waiting, because the rest of its surface is stable.

## 6. Alternatives considered

**Generated API docs (Sphinx/mkdocstrings) from docstrings.** Rejected: the
docstrings in this repo are already good, and generation would produce a
signature dump that omits precisely the invariants section 9 exists to capture.
It also adds a build step to a repo that currently has one CI file. Revisit if
the symbol count grows past what hand-maintenance tolerates.

**Expand the READMEs in place, no `docs/` pages.** Rejected: READMEs are the
first thing a browsing engineer sees and should stay short. A twelve-section
reference at the top of every package directory buries the one paragraph that
tells you whether you want the package at all.

**A documentation site now.** Deferred, not rejected. The content is the
bottleneck, not the rendering, and Markdown in-repo is reviewable in the same PR
as the code it describes. When ten pages exist and are maintained, a site is a
half-day of work on top.

**No convention, per-package judgment.** This is the status quo, and section 1
measures it.
