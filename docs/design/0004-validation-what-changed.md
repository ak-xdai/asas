# asas-validation: what changed and why (companion to DR 0004)

Status: shipped as 0.11.1 (additive) · Date: 2026-09-12
Audience: the original author and package maintainers. This is the short,
honest account of where the redesign landed relative to the package you
extracted, and what was deliberately kept. The full design record with the
decision history is DR 0004 (`docs/design/0004-validation.md`, PR #49); the
design was validated with a working FastAPI + React prototype before
implementation.

## The one-sentence summary

The declared-rules engine you built stays untouched; alongside it, validation
gains a **library of 44 generic checks as plain functions** with a one-line
call-site API (`validate.after(a, b, days=3)` inside `enforce([...])`) — no
entity names, no field registration, no ceremony — plus a derived
machine-readable catalog, a clock seam, and translation-ready violations.

## Where we landed versus the original

| Aspect | Original (0.11.0) | Now (0.11.1) |
|---|---|---|
| Adding a rule to an endpoint | Declare in a central catalog at boot: entity name, field names, code | One line at the write path, naming a shipped check, passing values in hand |
| Rule types | 4, dates only, closed set (editing the package to add one) | 44 generic checks in 6 families; hosts add one by writing a function in their own file (`include_checks`) |
| Setup ceremony | declare_rules + register_fields + assert_rules_known | None for the check path: import and use |
| Cross-record values | `parent.field` names + a context dict; forgetting it silently skipped the rule | Pass the value you are holding; nothing to forget |
| Errors | Field-level 422, all at once (kept exactly) | Same envelope; check violations additionally carry `message_key` + `params` for translating clients |
| Partial edits | Only touched rules fire (the crown jewel) | Kept unchanged, and mirrored in the checks: absent values pass, presence checks excepted |
| Today / type mixes | Server-local `date.today()`; date-vs-datetime raised | `configure_clock(fn)` (both paths use it); coercion instead of a crash |
| Discoverability | Rules endpoint listing declared rules | Plus `catalog()`: every check with what it takes and its human sentence, derived by introspection |
| Tables / config | None | Still none — deliberately (a policy tier was designed and then descoped; see DR 0004) |
| Where business logic lives | Between the catalog and the code | At the write path it guards, composed from generic checks |

The pattern: what was central, declared, and named moved to local, written,
and held; what was already excellent (the envelope, partial edits, no tables)
did not move at all.

## Where the value lives (stated plainly)

The module is a **rich library times a small standard**. The 44 checks carry
most of the paid-for correctness (boundaries, leap days, timezones, coercion,
presence semantics); the frame around them is small as code but valuable as a
standard — one error shape, violation-not-boolean, absent-values-pass, the
read-aloud naming grammar, the derived catalog — because it makes every Asas
application behave identically and every client keep one error path. Either
factor alone would be underwhelming; the multiplication is the product. The
build consequence we committed to: effort goes ~80% into checks and their
edge-case tests, ~20% into the frame.

## Decisions you may want to challenge (with their reasoning)

1. **No registration API.** `include_checks(module)` takes a module of plain
   functions; the namespace and catalog are derived by introspection
   (signature = what it takes, docstring first line = the sentence). A
   `register()` call was designed, then removed: it declared the same facts a
   function already states.
2. **Generic vocabulary only.** A business-named check is rejected by
   convention (and review): business meaning composes at call sites, which
   also yields one precise message per failed aspect.
3. **No runtime-tunable rules.** A DB policy tier was fully designed and then
   descoped: parameters change rarely, and the developer is updating logic
   anyway. It can return as a pure addition if a real deployment asks.
4. **`catalog.py` → `fields.py` rename.** The new `catalog()` callable would
   have shadowed the submodule — the exact TEAMY-798 trap your host-contract
   test pins. Public imports are unchanged.

## What comes next (DR 0004 phases, all optional)

Phase 2 rebases the declared engine's four kinds onto the check library (no
behavior change). Phase 3 adds an Arabic/English message table keyed by
`message_key`. Phase 4 serves the catalog over HTTP with a reference
browser-side evaluator and adds bulk dry-run of a rule against existing data.
