"""Oracle's ``q`` filter grammar, as a real Fusion instance implements it, and
the small readers every row needs.

The traps, verified against a live Fusion HCM pod:

- **AND is ``;``.** The literal word ``AND`` returns an empty body.
- **There is no working OR.** ``OR``, ``IN (...)`` and a comma-separated value
  list all silently match NOTHING: no error, just an empty collection. So a
  clause list is AND-only, a facet carries at most one value, and "several ids"
  means several requests (see :class:`~asas_oracle_hcm.OracleLookups`).
- **LIKE takes ``%`` wildcards** and is case-insensitive on ``Title``.
- **A value is single-quoted, and there is no escape form**, so an embedded
  quote would break the expression. :func:`literal` strips it.
- **Equality is case-sensitive.** An address stored as ``Jane@X.com`` does not
  match ``jane@x.com``.
"""

from __future__ import annotations

from typing import Any


def literal(value: object) -> str:
    """A value made safe to embed in a ``q`` clause: its single quotes removed,
    since Oracle offers no way to escape one."""
    return str(value).replace("'", "")


def eq(field: str, value: object, *, quote: bool = True) -> str:
    """``field='value'``. Pass ``quote=False`` for a bare numeric id, the form
    the id lookups have always used (``PersonId=300000008607150``)."""
    v = literal(value)
    return f"{field}='{v}'" if quote else f"{field}={v}"


def like(field: str, term: object) -> str:
    """A substring match: ``field LIKE '%term%'``."""
    return f"{field} LIKE '%{literal(term)}%'"


def and_(*clauses: str | None) -> str | None:
    """Join clauses with Oracle's AND (``;``), skipping empty ones. ``None``
    when nothing is being filtered, so it can be passed straight as ``q``."""
    live = [c for c in clauses if c]
    return ";".join(live) if live else None


# --- row readers ---------------------------------------------------------------


def text(row: dict[str, Any], key: str) -> str:
    """A field as text. Oracle sends ``null`` for "unset" on almost every
    optional field; this reads it as the empty string."""
    value = row.get(key)
    return "" if value is None else str(value)


def flag(row: dict[str, Any], key: str) -> bool:
    """A yes/no field. Oracle mixes real JSON booleans (on some resources) with
    ``"Y"`` / ``"N"`` strings (on others) for the same idea."""
    value = row.get(key)
    if isinstance(value, bool):
        return value
    return str(value).strip().upper() in {"Y", "YES", "TRUE"}


def integer(row: dict[str, Any], key: str) -> int | None:
    """A whole-number field, or ``None`` when it is absent or not a number."""
    value = row.get(key)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def child_items(row: dict[str, Any], child: str) -> list[dict[str, Any]]:
    """The rows of an expanded child (``expand=<child>``). Some resources send
    the child as a bare list and others as a collection object with
    ``items``; this reads both."""
    raw = row.get(child)
    if isinstance(raw, dict):
        raw = raw.get("items")
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []
