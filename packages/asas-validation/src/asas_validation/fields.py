"""Known-field registry. The host registers the valid
field names per entity so a rule that references a typo'd field fails loud at startup
instead of silently never firing. Registration is how field names become checkable — see ``assert_rules_known``."""

_FIELDS: dict[str, set[str]] = {}


def register_fields(entity_type: str, fields) -> None:
    """Register (or extend) the set of known field names for ``entity_type``."""
    _FIELDS.setdefault(entity_type, set()).update(fields)


def known_fields(entity_type: str) -> set[str]:
    return set(_FIELDS.get(entity_type, set()))


def is_known(entity_type: str, field: str) -> bool:
    """True if ``field`` is registered for ``entity_type``. An entity type that hasn't
    registered yet is treated as known (so rules only get checked once app code opts in)."""
    fields = _FIELDS.get(entity_type)
    return fields is None or field in fields
