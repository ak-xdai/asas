# DR 0004 (Asas): asas-validation — product vision, requirements, and the check-library specification

Status: DRAFT for review · Author: ak@xdigit.ai (with Claude) · Date: 2026-09-11
Scope: the validation capability across Asas — product vision → needs →
requirements → design approach → coverage of existing packages → implementation
spec. Builds on asas-validation 0.11.0 (extracted from Teamy, DR 0007/0017) and
on the control-split principles established in DR 0003.

## 1. Product vision

> When a user saves something invalid — an end date before the start date, a
> birth date in the future, an expired certificate — the application stops
> them, explains the problem **on the exact field**, in **their language**,
> **instantly**, and behaves **identically** everywhere: web form, API, bulk
> import, and an AI agent calling the same action. The rules that define
> "invalid" are written **once**, are **visible** to whoever needs them
> (developers, admins, clients, auditors), and the **tunable numbers** inside
> them can be changed by a product owner without a software release.

The vision is built on one distinction (the DR 0003 control split, applied to
validation):

| Tier | Owns | Example |
|---|---|---|
| **Developer** | The invariants — what must structurally be true, and the vocabulary of checks | "end date ≥ start date"; the `after` check itself |
| **Admin / product owner** | The dials — tunable thresholds and message wording | the **7** in "certificate younger than 7 years"; the Arabic message |
| **Every surface** | Consumes the same declarations | form, API, import, agent, audit report |

And on one adoption rule: **hardcode what no admin will tune, no client must
mirror, and no auditor will ask about; declare what any of those multipliers
touches.** A typical application declares ~30% of its checks; that ratio is
correct, not a failure of adoption.

## 2. Needs

- **N1 (end user)** — field-level, instant, native-language errors; identical
  behavior on every path that writes data.
- **N2 (developer)** — validating correctly must cost about one line; the
  subtle parts (inclusive/exclusive boundaries, timezones, Feb-29, partial
  updates) solved once, not re-decided per call site.
- **N3 (product owner)** — change a threshold or a message without a release;
  see the active rules in plain language.
- **N4 (frontend/agent integrator)** — obtain the machine-readable rule set
  and enforce the same constraints locally without re-implementing them.
- **N5 (auditor / government deployment)** — an authoritative, current answer
  to "what are this system's validation rules", plus an audit trail when a
  dial changes.
- **N6 (admin, later)** — compose *new* rules safely from developer-vetted
  templates, with a dry-run against existing data before activation.

## 3. Requirements

Status legend: ✅ solved in asas-validation 0.11 · ⚠️ partial · ❌ not solved.

| # | Requirement | Status today |
|---|---|---|
| R1 | One definition, enforced on every write path (API, import, agent) | ⚠️ solved where the developer calls it; nothing makes a path go through it |
| R2 | Edit-aware validation: partial updates check only the rules the edit touches, overlaying changes on the current record | ✅ the package's crown jewel |
| R3 | Uniform field-level error envelope, shared with body-shape (Pydantic) errors | ✅ |
| R4 | Open, extensible check vocabulary (dates, numbers, text, required-when, conditions) | ❌ 4 fixed date kinds; registry is private |
| R5 | Tunable policy values at runtime, no release | ❌ — **descoped in review round four (2026-09-11)**: params change rarely, an admin logs in somewhere either way, and the developer updates logic anyway. Code-only; revisit only when a real deployment asks to turn a dial without a release |
| R6 | Rules visible: machine endpoint + human-readable rendering | ⚠️ endpoint exists (ETag-correct); no plain-language rendering |
| R7 | Client-side mirroring: the browser/agent evaluates the *same* rules | ⚠️ rules downloadable; client must hand-write an evaluator |
| R8 | Localized messages (Arabic/English), product-editable | ❌ one hardcoded English string per rule |
| R9 | Timezone-correct temporal checks; datetime/date mixing never crashes | ❌ server-local `date.today()`; datetime vs date comparison raises |
| R10 | Fail loud, never silently skip (missing context, unregistered entity) | ⚠️ startup typo check exists; two silent-skip gaps remain |
| R11 | Admin authoring ladder: dials → template instantiation → flat conditions (never a nested logic builder) | ❌ future tier; format must not preclude it |
| R12 | Dry-run: evaluate a rule in bulk against existing data before activating | ❌ engine validates single edits only |
| R13 | Developer cost ≤ ~2× a hardcoded `if`; field dictionary derived from schemas the developer already wrote | ⚠️ declaration is cheap; dictionary is manual |
| R14 | Dial changes audited (who/what/old/new) | ❌ future; shared Asas audit capability |

## 4. Requirements → design approach

| Req | Design approach | Decision notes |
|---|---|---|
| R2, R3 | **Keep unchanged.** The overlay engine and 422 envelope are the proven core; every layer below plugs into them | Nothing off the shelf replaces these (see §5) |
| R4, N2 | **The check library** (this DR's centerpiece, §6 V-1): one namespace — `validate.after(a, b, days=3)` — callable inline anywhere, returning a structured `Violation`, never a bare bool | Checks are pure functions over *values*; the declared layer binds *fields* to them. The current private `_KINDS` becomes the public registry |
| R1 | Checks-inline for the 70%; declared bindings for the 30%; (future) the actions layer makes validation a declared guard on the action so no path can skip it | Adoption is per-rule, never all-or-nothing |
| R5 | **Descoped (round four).** All rule definition and application stays in code; the package remains table-less. The check registry's signature-as-data keeps the door open: a policy tier would only ever override `params`, so it can be added later without touching any rule | Complexity razor: the user's own adoption rule applied to the package itself |
| R6, R7 | Declared rules serve as **check-name + bindings + params** — a portable, interpretable format. A small reference JS evaluator ships for the built-in checks; hosts mirror custom checks or fall back to server-round-trip for them | Considered adopting JsonLogic/CEL as the wire format; rejected for v1 — named checks with published semantics are more legible to admins and renderable as sentences (R11). Revisit if check count explodes |
| R8 | Violations carry a **message key + params**; message text lives in a translatable table beside the notification templates (same admin surface eventually) | Inline calls may still pass a literal message; keys are the declared path's default |
| R9 | A **clock/locale seam** (`configure_clock`, tz-aware "today" per request context) + type coercion so datetime-vs-date compares instead of crashing | Same host-seam pattern as every other package |
| R10 | Missing `context` for a namespaced field and unregistered entities become **errors by default** (opt-out flag for the legacy behavior) — the notifications visibility-filter precedent | |
| R11 | Format carries **template identity + typed slots + a flat AND condition list** from day one; the authoring UI itself is far future | OR = two rules; the builder is deliberately not Turing-complete; Level-3 logic stays in code |
| R12 | `evaluate_bulk(entity, rule, query)` — the engine gains a set-based mode; powers the future dry-run screen ("this rule would flag 1,236 records") | Cheap to add early, expensive to retrofit — engine work now, UI later |
| R13 | Field dictionary **derived** from the host's Pydantic/SQLModel schemas via one wiring call; developer adds only labels/exclusions | The existing `register_fields` typo-check registry is the seed of this |
| R14 | Deferred to the shared Asas audit capability; the policy-tier table carries `updated_at`/`updated_by` columns from day one | |

## 5. Coverage: requirements × existing packages

What each candidate gives us out of the box. ✅ covered · ◐ partial / with work · ✗ not covered.

| Requirement | asas-validation 0.11 | **Target (this spec)** | Pydantic v2 | JSON Schema (+ajv) | JsonLogic (py+js) | CEL (cel-python) | Generic rule engines¹ |
|---|---|---|---|---|---|---|---|
| R1 one definition, all paths | ◐ | ✅ | ◐ per-model | ◐ per-schema | ◐ | ◐ | ◐ |
| R2 edit-aware overlay | ✅ | ✅ | ✗ whole-model | ✗ | ✗ | ✗ | ✗ |
| R3 FastAPI 422 envelope | ✅ | ✅ | ✅ | ✗ | ✗ | ✗ | ✗ |
| R4 extensible vocabulary | ✗ | ✅ | ✅ code-first | ◐ | ✅ | ✅ | ✅ |
| R5 runtime-tunable dials | ✗ | ✅ | ✗ | ✗ | ◐ rules-as-data | ◐ | ◐ |
| R6 rules visible/explainable | ◐ | ✅ | ✗ | ◐ schema is the doc | ◐ | ✗ | ◐ |
| R7 client mirrors same rules | ◐ | ✅ | ✗ | ✅ ajv | ✅ same JSON both sides | ✅ cel-js | ✗ |
| R8 localized messages | ✗ | ✅ | ◐ | ◐ | ✗ | ✗ | ✗ |
| R9 timezone-correct temporal | ✗ | ✅ | ◐ DIY | ✗ weak date semantics | ✗ | ◐ | ✗ |
| R10 fail-loud wiring | ◐ | ✅ | ✅ | ◐ | ✗ | ✅ typed | ✗ |
| R11 admin authoring format | ✗ | ✅ format only | ✗ | ✗ | ◐ | ✗ | ◐ some ship UIs² |
| R12 bulk dry-run | ✗ | ✅ | ✗ | ✗ | ✗ | ✗ | ◐ |
| R13 ≤2× developer cost | ✅ | ✅ | ✅ | ◐ | ◐ | ◐ | ◐ |
| Maintained / bus factor | ours | ours | ✅ huge | ✅ standard | ◐ ports vary | ✅ Google/k8s | ✗ mostly single-maintainer |

¹ Cerberus, business-rules forks, py-rules-engine, zspec and kin (surveyed 2026-09-11).
² e.g. ezrules — full standalone products with their own DB/UI stack, violating the embedded/no-second-platform constraint.

**Reading of the table:** no package covers the left column's top three rows —
edit-aware overlay + envelope + all-paths glue — which is precisely the part
asas-validation already owns. The generic engines and expression languages win
only on vocabulary (R4) and client mirroring (R7), both of which the check
library covers at lower integration cost. Conclusion: **evolve, don't replace**;
adopt an external expression core only if the check vocabulary outgrows
legibility (revisit trigger: >~20 built-in checks or demand for arbitrary
boolean composition).

## 6. Implementation specification

### V-1 The check library (foundation — build first)

Public, extensible predicates; the current private `_KINDS` promoted to a
registry. A check is a pure function over **values**.

**Naming rule** (adopted in the guideline-simulation review, 2026-09-11):
every check name completes the sentence *"<field> must be …"*, and the value
under test is always the first argument — each call reads aloud as the
business rule it enforces. This grammar is also what the admin template
dropdowns and plain-language rule rendering (R6/R11) inherit later.

**Functions in a file, two doors** (rounds three, five, and seven —
prototype-validated): there is no ``register()`` call. A check IS a plain
function in a checks module: the library ships its built-ins that way, and a
host adds its own by writing functions in its own checks file. The
``validate`` namespace and ``checks.catalog()`` are **derived by reading the
module** — the function's signature says what it takes, its docstring's first
line is the human sentence — so nothing is declared twice and "define a
check" means "write a function". The primary, human door is attribute
access — ``validate.after(a, b, days=3)``, real functions, IDE-visible
signatures. The machine door — ``validate("after", ...)`` — serves callers
holding the name as data (an agent reading the catalog, the client
evaluator); both doors reach the same functions. Unknown names fail loud.
Evidence: working prototype (FastAPI + React interview form, 2026-09-12) —
the derived namespace/catalog and one shared check list serving both a
live-feedback endpoint and the save endpoint, ~40 lines of framework total.

**Generic vocabulary only** (round five): the registry holds domain-free
primitives — a check like ``interview_window`` is a design smell. Business
meaning comes from *composing* generic checks at the call site (each
violation then reports its own precise reason), and a recurring composite is
wrapped in a plain host function returning a list of violations — ordinary
code reuse, never a library entry. ``enforce(list)`` drops the ``None``\ s
and raises one 422 carrying **all** remaining violations, so the user sees
every error in one response instead of fixing them one resubmit at a time.
The prototype's embedding pattern is the reference: one function returns the
endpoint's check list; a ``/check`` endpoint enforces it without saving (live
per-field form feedback) and the save endpoint enforces the same list before
writing — same rules at both moments by construction.

```python
# define: app/checks.py — a plain function; the docstring IS the sentence
def iban(value, *, field="") -> Optional[Violation]:
    """{field} must be a well-formed IBAN"""
    ...

from asas_validation import validate, enforce

# apply (anywhere) — business meaning by composition of generic checks
enforce([
    validate.between(scheduled_date, posting_date, closing_date,
                     field="scheduled_date"),
    validate.after(scheduled_date, application_date, days=3,
                   field="scheduled_date", message_key="interview.min_notice"),
])
# validate() → None when valid, else
#   Violation(field="scheduled_date", code="after",
#             params={"days": 3}, message_key="interview.min_notice")
```

- Ships with the **44-check catalog** (V-1a) — six families: temporal (12),
  numeric (8), cross-field (6), presence/conditional (5), format (10),
  collections (3). Earlier drafts named ~7; review round six expanded to a
  full catalog sourced from Laravel/validator.js/Joi/Django validators,
  filtered by the generic-only rule and renamed into the read-aloud grammar.
- Violation carries `message_key` + `params`; literal `message=` allowed for
  one-offs. **`enforce(violations)`** drops the `None`s and folds the rest
  into the 422 envelope; being a *new* name, v0.11's `raise_if_invalid`
  keeps its signature untouched through the migration — no breaking rename.
- Clock seam: `configure_clock(fn)` supplying a tz-aware "today"; date/datetime
  coercion (never a 500 from a type mix).
- Inline calls are the blessed 70% path: no catalog, no ceremony, one line.

**Code/table boundary and signatures** (review rounds three and four,
2026-09-11):

- A check is an *implementation* and stays in code —
  executable logic in a database row is unreviewable, untestable, and
  unauditable, and admins configure behavior, never author logic (the DR 0003
  principle). **Round four descoped the table side entirely**: rule
  applications and params also stay in code (they change rarely, and the
  developer is updating logic anyway) — the package stays table-less, and
  the runtime-dials idea returns only on demonstrated demand. Genuinely new
  logic at runtime remains the CEL/JsonLogic revisit trigger (§5).
- **Arguments**: a check takes ordered *values* (record data under test, one
  or many) and named *params* (configuration constants — the dials). Stored
  rules carry the identical split as ``bindings`` (ordered field refs) and
  ``params`` (JSON), so inline calls and table rows are the same information.
- **Discoverability**: the signature as data is *derived*, never authored —
  ``checks.catalog()`` introspects each function (parameter names, types,
  defaults) and takes the sentence template from its docstring ("{value}
  must be after {reference} by {days} day(s)"). It serves agents, docs, the
  rules endpoint, and the future template dropdowns; a wrong-arity call
  fails loud with the expected signature named in the error; the sentence
  is the R6/R11 plain-language rendering for free.

### V-1a The shipped catalog (44 checks, review round six)

Sourced from the union of mainstream SaaS/framework validators (Laravel
rules, validator.js, Joi/Zod, Django/Rails), kept only where generic and
renamed into the grammar. Every check: values first, params named, a
``sentence`` template, catalog-listed.

**Temporal (12)**: ``not_in_future(v)`` · ``not_in_past(v)`` ·
``after(v, ref, days=0)`` · ``before(v, ref, days=0)`` ·
``between(v, start, end)`` · ``not_older_than(v, years=|days=)`` ·
``not_beyond(v, years=|days=)`` (max future horizon) ·
``age_at_least(birth, years=18)`` · ``age_at_most(birth, years=)`` ·
``on_weekday(v, allowed=[0..4])`` ·
``within_period(start, end, p_start, p_end)`` (range inside parent range) ·
``no_overlap(start, end, o_start, o_end)`` (booking clashes).

**Numeric (8)**: ``at_least(v, minimum)`` · ``at_most(v, maximum)`` ·
``positive(v)`` · ``non_negative(v)`` · ``multiple_of(v, step)`` ·
``max_decimals(v, places=2)`` · ``percent(v)`` · ``luhn(v)``.

**Cross-field (6)**: ``greater_than(v, ref)`` · ``less_than(v, ref)`` ·
``equal_to(v, ref)`` (confirmation fields) · ``different_from(v, ref)``
(new ≠ old) · ``sums_to(*parts, total=)`` (splits) ·
``ratio_at_least(v, ref, ratio=)`` (the generic "band" rule).

**Presence/conditional (5)**: ``required_when(v, when=)`` ·
``forbidden_when(v, when=)`` · ``required_together(*vs)`` ·
``at_least_one(*vs)`` · ``exactly_one(*vs)``.

**Format (10)**: ``email(v, domains=None)`` · ``url(v, schemes=("https",))``
· ``phone(v, region=None)`` · ``iban(v)`` · ``uuid(v)`` · ``slug(v)`` ·
``matches(v, pattern)`` (regex escape hatch) · ``no_html(v)`` ·
``one_of(v, allowed)`` (membership against dynamic lists, e.g. asas-lookups)
· ``not_in(v, forbidden)``.

**Collections (3)**: ``unique_items(vs)`` · ``subset_of(vs, allowed)`` ·
``contains_none(vs, terms)``.

**Deliberately excluded**: type/required/length/size checks (Pydantic — the
boundary stands); DB uniqueness/foreign-existence (a pure value check must
not hold a query seam; host router code today, possible seam later — open
question 5); authorization (asas-access); region-specific ids (Emirates ID
etc. — violate generic-only; future opt-in regional packs as extra checks
modules).

### V-2 Declared rules become stored bindings of checks

`Rule` is refactored to *(entity, check, bindings, params, message_key,
conditions)* — a stored call of a V-1 check. The four current kinds map 1:1;
`declare_rules` / `evaluate` / `assert_rules_known` keep their signatures.
New: `conditions` — a flat AND-list of simple field predicates (the R11 slot);
missing namespaced `context` fails loud (R10) unless the rule opts out.

### V-3 Policy tier — DESCOPED (review round four)

Removed from scope: params change rarely, and the cost (a table, a migrate,
a cache, an admin surface) outweighs it. The signature-as-data registry means
a future policy tier is a pure addition (params overrides only), so nothing
is foreclosed. Revisit trigger: a real deployment asking to change a
threshold without a release.

### V-4 Messages and localization

`message_key` → text lookup with locale fallback (`ar`/`en` first-class),
table-backed in the policy module, dict-backed (code) without it. Violations
serialize key + params so clients may also render locally.

### V-5 Rules endpoint v2 + reference client evaluator

The endpoint serves check-name + bindings + params + conditions (ETag semantics
unchanged). A small `@asas/validation-js` reference evaluator implements the
built-in checks so forms enforce the same rules with zero re-implementation;
unknown custom checks degrade to server-side-only, explicitly listed as such.

### V-6 Bulk evaluation (dry-run engine)

`evaluate_existing(session_or_query, rule) -> sample + count` — set-based
evaluation for "what would this rule flag today". Powers the future admin
dry-run screen; immediately useful in host test suites and data-quality jobs.

### Field dictionary (spans V-2/V-5)

`derive_fields(model, exclude=[...], labels={...})` lifts entity/field/type/
label metadata from the host's existing Pydantic/SQLModel classes into the
known-field catalog — `register_fields` becomes the low-level primitive under
it. Unregistered entities flip from vacuously-known to fail-loud (R10).

### Phasing

| Phase | Ships | Requirements closed |
|---|---|---|
| P1 | V-1 checks + clock seam + R10 fixes | R4, R9, R10, N2 |
| P2 | V-2 bindings refactor + field derivation | R13, R11 (format), R1 |
| P3 | V-4 messages (ar/en) | R8 |
| P4 | V-5 endpoint v2 + JS evaluator; V-6 bulk | R6, R7, R12 |

Each phase is one reviewable PR, dual-engine tested where DB-backed,
version-bumped per RELEASING.md. P1 is pure addition (no breaking change);
P2 is the one breaking refactor (pre-1.0 minor bump).

## 7. Open questions for review

1. ~~Policy tier placement~~ — resolved by descoping the tier (round four).
2. `required_when` blurs into Pydantic's territory — include it (proposed,
   for the conditions story) or document the boundary as "shape is Pydantic,
   semantics are checks"?
3. Should V-5's JS evaluator wait for the repo's JS-workspace decision
   (shared with @asas/notifications-react), or ship as a separate npm repo?
4. The CEL/JsonLogic revisit trigger said ">~20 checks" — V-1a now ships 44
   *primitives*, which is vocabulary breadth, not composition demand.
   Re-state the trigger as: demand for arbitrary boolean composition or
   runtime-defined logic, regardless of catalog size?
5. DB-backed checks (uniqueness, foreign existence): permanently excluded,
   or a future opt-in query seam?
