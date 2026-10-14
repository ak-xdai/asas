"""Framework glue: turn engine ``Violation``s into FastAPI's native 422 envelope
(``{"detail": [{"loc", "msg", "type"}, ...]}``) so the frontend handles our semantic
constraint errors and Pydantic's body-shape errors through one code path."""

from typing import Any, Optional

from fastapi import HTTPException

from .engine import Violation, evaluate


def to_http(violations: list[Violation]) -> HTTPException:
    detail = []
    for v in violations:
        entry = {"loc": ["body", v.field], "msg": v.message, "type": f"value_error.{v.code}"}
        # Additive keys for translating clients (DR 0004); absent on legacy
        # violations, so existing consumers see the exact same payload.
        if getattr(v, "message_key", ""):
            entry["message_key"] = v.message_key
        if getattr(v, "params", None):
            entry["params"] = v.params
        detail.append(entry)
    return HTTPException(status_code=422, detail=detail)


def enforce(results) -> None:
    """The 'if all pass, proceed; else report everything' step for check-style
    validation (DR 0004): drop the passes (``None``), and if any violations
    remain raise one 422 carrying every one of them — the user fixes all
    mistakes in a single round trip, never one per resubmit.

    ``results`` is any iterable mixing ``Violation`` and ``None``, typically a
    list of ``validate.<check>(...)`` calls."""
    problems = [r for r in results if r is not None]
    if problems:
        raise to_http(problems)


def raise_if_invalid(
    entity: str,
    record: Optional[Any],
    changes: dict[str, Any],
    context: Optional[dict[str, Any]] = None,
) -> None:
    """Evaluate the entity's rules for this edit and raise a 422 if anything fails.
    ``context`` carries related-record values for cross-entity rules (namespaced fields).
    Call in create/update routers right before applying the changes."""
    violations = evaluate(entity, record, changes, context)
    if violations:
        raise to_http(violations)
