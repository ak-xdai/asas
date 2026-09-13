"""Constraint engine — evaluates the declared rules against a proposed edit.

Model-free: it reads the current record with ``getattr`` (duck-typed) and overlays the
incoming ``changes`` dict, so it never imports host models. Two guards keep it well-behaved
for partial updates:

  * a rule only fires when the edit **actually touches** one of its fields — so an
    unrelated update (e.g. just the name) is never blocked by pre-existing data;
  * a rule is skipped when any field it reads is missing/``None`` — you can't compare
    against an absent date.
"""

from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date

from .clock import today as _clock_today
from .clock import date_of as _shared_date_of
from .clock import years_ago as _shared_years_ago
from typing import Any, Mapping, Optional

from . import fields as catalog
from .rules import declared_rules, rules_for


@dataclass(frozen=True)
class Violation:
    field: str
    code: str
    message: str
    # For translating clients (DR 0004): a stable key plus the numbers used,
    # so the message can be rendered in the viewer's language. Optional with
    # defaults, so pre-0.11.1 constructions are untouched.
    params: dict = dc_field(default_factory=dict)
    message_key: str = ""

    def __hash__(self):
        # A frozen dataclass would derive __hash__ from ALL fields and choke
        # on the params dict — 0.11.0 Violations were hashable (hosts dedupe
        # them in sets), so identity stays (field, code, message, key) and
        # params ride along as detail.
        return hash((self.field, self.code, self.message, self.message_key))


def _effective(
    record: Optional[Any],
    changes: Mapping[str, Any],
    context: Optional[Mapping[str, Any]],
    field: str,
) -> Any:
    """The value ``field`` would have after applying ``changes`` to ``record``.

    A **namespaced** field (``parent.field``, e.g. ``project.start_date``) is a related
    record's value, supplied by the caller in ``context``; it's never on the record or in
    the incoming changes."""
    if field in changes:
        return changes[field]
    if context and field in context:
        return context[field]
    if "." in field:
        return None  # namespaced parent field the caller didn't supply → skip the rule
    return getattr(record, field, None) if record is not None else None


def _years_ago(today: date, years: int) -> date:
    """``today`` shifted back ``years`` years, clamping a Feb-29 anchor to Feb-28."""
    return _shared_years_ago(today, years)


def _date_of(value):
    """A datetime column under a date rule must compare, never raise —
    the shared policy from clock.date_of."""
    return _shared_date_of(value)


def _not_future(vals: list, today: date, rule) -> bool:
    return _date_of(vals[0]) <= today


def _not_past(vals: list, today: date, rule) -> bool:
    return _date_of(vals[0]) >= today


def _order(vals: list, today: date, rule) -> bool:
    return _date_of(vals[0]) <= _date_of(vals[1])


def _max_age(vals: list, today: date, rule) -> bool:
    return _date_of(vals[0]) >= _years_ago(today, rule.params["years"])


_KINDS = {
    "not_future": _not_future,
    "not_past": _not_past,
    "order": _order,
    "max_age": _max_age,
}


def evaluate(
    entity: str,
    record: Optional[Any],
    changes: Mapping[str, Any],
    context: Optional[Mapping[str, Any]] = None,
) -> list[Violation]:
    """Return the constraint violations for applying ``changes`` to ``record`` (``None``
    on create). ``context`` supplies related-record values for namespaced fields (see
    ``_effective``). Only rules whose fields the edit touches, and whose values are all
    present, are checked."""
    today = _clock_today()
    violations: list[Violation] = []
    for rule in rules_for(entity):
        if not any(f in changes for f in rule.fields):
            continue
        vals = [_effective(record, changes, context, f) for f in rule.fields]
        if any(v is None for v in vals):
            continue
        if not _KINDS[rule.kind](vals, today, rule):
            violations.append(Violation(rule.target, rule.code, rule.message))
    return violations


def assert_rules_known() -> None:
    """Fail loud at startup if any declared rule references a field not registered in the
    catalog (a typo, or a renamed model field). The host calls this from its wiring after
    declaring rules and registering fields."""
    for rule in declared_rules():
        for field in rule.fields:
            # A namespaced `parent.field` is validated against the parent entity's
            # registered fields; a plain field against the rule's own entity.
            if "." in field:
                ns_entity, ns_field = field.split(".", 1)
                known = catalog.is_known(ns_entity, ns_field)
            else:
                ns_entity, ns_field = rule.entity, field
                known = catalog.is_known(rule.entity, field)
            if not known:
                raise ValueError(
                    f"validation rule '{rule.code}' references unknown field "
                    f"'{ns_entity}.{ns_field}'"
                )
        if rule.kind not in _KINDS:
            raise ValueError(
                f"validation rule '{rule.code}' has unknown kind '{rule.kind}'"
            )
