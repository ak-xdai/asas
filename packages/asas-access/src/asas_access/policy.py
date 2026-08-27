"""The field-permission engine: load policy rows (cached), and answer can-view /
can-edit for a (user, record, field). Framework-agnostic — raises nothing HTTP; callers
map :func:`forbidden_edits` results to a 403. Enforcement on/off (``auth_enforce``) is
the caller's concern, keeping this module free of app config."""

from typing import Any, Optional
from collections.abc import MutableMapping

from sqlmodel import Session, select

from .models import FieldPermission
from .principals import ROLE_ADMIN, held_principals

# {(entity_type, field, action): {(principal, org_id), ...}} — rebuilt lazily,
# invalidated on write. org_id None = platform default (applies everywhere);
# an org id scopes the grant to that org (WXL-241 slot, activated by the groups
# admin UI TEAMY-454 — an org's grant edits must never leak to other orgs).
_cache: Optional[dict[tuple[str, str, str], set[tuple[str, Optional[int]]]]] = None


def invalidate_cache() -> None:
    """Drop the in-memory policy cache (call after seeding/editing rows)."""
    global _cache
    _cache = None


def _policy(
    session: Session,
) -> dict[tuple[str, str, str], set[tuple[str, Optional[int]]]]:
    global _cache
    if _cache is None:
        cache: dict[tuple[str, str, str], set[tuple[str, Optional[int]]]] = {}
        for fp in session.exec(select(FieldPermission)).all():
            cache.setdefault((fp.entity_type, fp.field, fp.action), set()).add(
                (fp.principal, fp.org_id)
            )
        _cache = cache
    return _cache


def _allowed(
    session: Session, user: Any, entity_type: str, field: str, action: str, record: Any
) -> bool:
    rows = _policy(session).get((entity_type, field, action))
    if rows is None:
        return True  # unconfigured → defer to the caller's baseline rule
    org_id = getattr(user, "org_id", None)
    allowed = {p for (p, o) in rows if o is None or o == org_id}
    if not allowed:
        return True  # only other orgs' overrides exist → baseline here too
    held = held_principals(user, entity_type, record, session)
    if ROLE_ADMIN in held:  # admin floor — never lock admins out via config
        return True
    return bool(allowed & held)


def can_edit_field(
    session: Session, user: Any, entity_type: str, field: str, record: Any
) -> bool:
    return _allowed(session, user, entity_type, field, "edit", record)


def can_view_field(
    session: Session, user: Any, entity_type: str, field: str, record: Any
) -> bool:
    return _allowed(session, user, entity_type, field, "view", record)


def forbidden_edits(
    session: Session,
    user: Any,
    entity_type: str,
    record: Any,
    changes: dict[str, Any],
) -> list[str]:
    """Of the proposed ``changes`` (field -> new value), the fields ``user`` may not
    edit. Only an *actual* change is checked — submitting a field's current value is a
    no-op and always allowed (so clients can safely round-trip read-only fields)."""
    forbidden: list[str] = []
    for field, new_value in changes.items():
        if getattr(record, field, None) == new_value:
            continue
        if not can_edit_field(session, user, entity_type, field, record):
            forbidden.append(field)
    return forbidden


def view_restricted_fields(session: Session, entity_type: str) -> set[str]:
    """The fields of ``entity_type`` that carry any ``view`` policy rows (i.e. are not
    visible to everyone). Fields with no view rows are unrestricted and skipped."""
    return {
        field
        for (et, field, action) in _policy(session)
        if et == entity_type and action == "view"
    }


def redact_view(
    session: Session,
    user: Any,
    entity_type: str,
    read_model: Any,
    record: Any,
    *,
    also_null: Optional[dict[str, list[str]]] = None,
) -> Any:
    """Null out (in place) the fields of ``read_model`` that ``user`` may not view, per
    the ``view`` policy resolved against ``record``. Keeps the read model's shape stable
    (sets to ``None`` rather than dropping keys). ``also_null`` maps a restricted source
    field to companion fields to null with it (e.g. a ``*_code`` field's resolved
    ``*_label``). Returns ``read_model`` for chaining.

    ``read_model`` may be an **object** (attributes) or a **mapping** (keys). Both
    are redacted; a field the projection simply does not carry is skipped, which
    is the normal case for a read model that never included a restricted field.

    Supporting mappings is not a convenience. Until it was added, a plain
    ``dict`` matched nothing, was returned unchanged, and the restricted field
    reached the caller with no error — the failure mode of a redaction function
    is silent disclosure, so anything it cannot redact must be loud instead.
    Hence the ``TypeError`` below for a shape that is neither.
    """
    also_null = also_null or {}
    restricted = view_restricted_fields(session, entity_type)
    if not restricted:
        return read_model

    is_mapping = isinstance(read_model, MutableMapping)
    if not is_mapping and not hasattr(read_model, "__dict__") and not hasattr(
        read_model, "__slots__"
    ):
        raise TypeError(
            f"redact_view cannot redact a {type(read_model).__name__}: pass an object "
            f"with attributes or a mutable mapping. Returning it unredacted would "
            f"disclose {sorted(restricted)}."
        )

    for field in restricted:
        if can_view_field(session, user, entity_type, field, record):
            continue
        for target in (field, *also_null.get(field, [])):
            if is_mapping:
                if target in read_model:
                    read_model[target] = None
            elif hasattr(read_model, target):
                setattr(read_model, target, None)
    return read_model
