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
  quote ends the literal and an embedded ``;`` starts another clause: either
  changes what the query MEANS. :func:`literal` refuses both
  (:class:`OracleQueryError`) instead of guessing, and an UNQUOTED value (a
  bare numeric id) must be a plain id token. ``strip_quotes=True`` is the
  explicit opt-in for dropping quotes from free text such as a name, accepting
  that ``O'Brien`` is then searched as ``OBrien``.
- **Equality is case-sensitive.** An address stored as ``Jane@X.com`` does not
  match ``jane@x.com``.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import OracleQueryError

#: What an UNQUOTED value may be: an id or a number, nothing that could end the
#: clause or start another.
_TOKEN = re.compile(r"[A-Za-z0-9_.:+-]+")
#: Field names: Oracle attribute names, never caller text.
_FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_.]*")


def _field(name: str) -> str:
    if not _FIELD.fullmatch(name or ""):
        raise OracleQueryError(f"not an attribute name: {name!r}")
    return name


def literal(value: object, *, strip_quotes: bool = False) -> str:
    """A value checked for embedding in a quoted ``q`` literal.

    Refuses a single quote (it would end the literal) and a ``;`` (it would
    start another clause), because Oracle offers no way to escape either.
    ``strip_quotes=True`` drops quotes instead of refusing them, for free text
    where a near match is acceptable; a ``;`` is refused either way."""
    text = str(value)
    if ";" in text:
        raise OracleQueryError("a q value cannot contain ';' (it would start another clause)")
    if "'" in text:
        if not strip_quotes:
            raise OracleQueryError(
                "a q value cannot contain a single quote (Oracle has no escape for it); "
                "pass strip_quotes=True to search without it"
            )
        text = text.replace("'", "")
    return text


def eq(field: str, value: object, *, quote: bool = True, strip_quotes: bool = False) -> str:
    """``field='value'``. Pass ``quote=False`` for a bare numeric id, the form
    the id lookups use (``PersonId=300000008607150``); the value must then be a
    plain id token, or :class:`OracleQueryError` is raised."""
    name = _field(field)
    if not quote:
        token = str(value)
        if not _TOKEN.fullmatch(token):
            raise OracleQueryError(f"an unquoted q value must be a plain id: {token!r}")
        return f"{name}={token}"
    return f"{name}='{literal(value, strip_quotes=strip_quotes)}'"


def like(field: str, term: object, *, strip_quotes: bool = False) -> str:
    """A substring match: ``field LIKE '%term%'``. A ``%`` or ``_`` inside the
    term is a wildcard to Oracle, as in SQL."""
    return f"{_field(field)} LIKE '%{literal(term, strip_quotes=strip_quotes)}%'"


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
