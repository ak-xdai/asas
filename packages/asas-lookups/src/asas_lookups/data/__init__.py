"""Seed data, as data.

All of it lives in one file, ``seed.json``, beside this module. Adding, correcting
or removing seeded vocabulary is a JSON edit and nothing else -- no Python change,
no migration, no release of the loader. That is the point of the split: the values
a package ships are *content*, and content buried in source code cannot be
reviewed, diffed or translated by anyone who does not read Python.

## The contract

``$schema`` names and versions the shape, so a file and a loader that disagree say
so instead of half-working. The current contract is ``lookup_seed_v1``.

    {
      "$schema": "lookup_seed_v1",
      "description": "...",
      "types": [
        {
          "key": "gender",
          "name": "Gender",
          "description": "Gender identity options",
          "is_open": false,
          "is_hierarchical": false,
          "code_system": "internal",
          "scope": "platform",
          "default_sort": "sort_order",
          "values": [
            {
              "code": "M",
              "status": "active",
              "is_default": false,
              "sort_order": 1,
              "valid_from": null,
              "valid_to": null,
              "superseded_by": null,
              "parent_code": null,
              "meta": {},
              "translations": [
                {"lang": "en", "label": "Male",  "short_label": "M"},
                {"lang": "ar", "label": "ذكر",   "short_label": null}
              ],
              "aliases": [
                {"lang": "en", "alias": "Man"}
              ]
            }
          ]
        }
      ]
    }

The nesting mirrors the tables: a ``type`` entry is a ``lookup_type`` row, each of
its ``values`` a ``lookup_value``, each ``translations`` entry a
``lookup_translation`` and each ``aliases`` entry a ``lookup_alias``. Only ``key``,
``name`` and a value's ``code`` plus at least one translation are required;
everything else takes the column default.

``parent_code`` and ``superseded_by`` name a **code** within the same type, not an
id, and may name one that appears later in the array -- both are resolved in a
second pass once every row exists. Both are filled only while NULL, so a row an
admin re-parented is never moved back.

## What a file may NOT set

``id``, ``created_at`` and ``updated_at`` belong to the database. ``version``
belongs to ``bump_version_if`` -- it is the number the read API's ETag is keyed on,
so it records what seeding *did*, and a file that declared it would be asserting a
cache state it cannot know. ``org_id`` is always NULL from a seed: for a platform
type that is the row every org reads, and for an org type it is the template
``seed_org_lookups`` copies. All of them are refused by name, with the reason, so
nobody writes one and believes it took effect.

``sort_order`` may be set and usually should be for a curated list. Omitted, a
type sorted by ``sort_order`` takes the array index -- the order a reader sees in
the file -- and a ``label``-sorted type leaves the column at 0, since nothing
reads it.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterator, NamedTuple, Optional

_HERE = Path(__file__).parent
SEED_FILE = _HERE / "seed.json"

#: The contract this loader implements. A file naming anything else is refused
#: rather than read optimistically: the shape is the one thing a seed file cannot
#: be wrong about quietly.
SCHEMA = "lookup_seed_v1"

#: Mirrors ``models.LookupStatus``. Spelt out rather than imported so the loader
#: stays importable without the ORM.
_STATUSES = {"active", "deprecated"}

_TYPE_KEYS = frozenset(
    {
        "key",
        "name",
        "description",
        "is_open",
        "is_hierarchical",
        "code_system",
        "scope",
        "default_sort",
        "values",
    }
)

_VALUE_KEYS = frozenset(
    {
        "code",
        "status",
        "is_default",
        "sort_order",
        "valid_from",
        "valid_to",
        "superseded_by",
        "parent_code",
        "meta",
        "translations",
        "aliases",
    }
)

_TRANSLATION_KEYS = frozenset({"lang", "label", "short_label"})
_ALIAS_KEYS = frozenset({"lang", "alias"})

#: Real columns a file may not set, each with the reason. Named separately from an
#: ordinary typo so the error explains rather than just refusing.
_FORBIDDEN = {
    "id": "the database assigns it",
    "created_at": "the database assigns it",
    "updated_at": "the database assigns it",
    "type_id": "it comes from the type this value sits under",
    "value_id": "it comes from the value this row sits under",
    "org_id": (
        "a seed owns platform rows only — for a platform type that IS the row "
        "every org reads, and for an org type it is the template "
        "seed_org_lookups copies per org"
    ),
    "parent_id": "name the parent by its code, as 'parent_code'",
    "superseded_by_id": "name the replacement by its code, as 'superseded_by'",
    "version": (
        "bump_version_if owns it — it records what seeding did, and the read "
        "API's ETag is keyed on it, so a file cannot assert it"
    ),
    "labels": "translations are a list of {lang, label}, under 'translations'",
    "label": "a translation carries 'label'; the list is 'translations'",
    "short_labels": "a translation carries 'short_label'",
    "parent": "the key is 'parent_code'",
}


class SeedValue(NamedTuple):
    """One ``lookup_value`` and the rows hanging off it, as the file declares them."""

    code: str
    translations: list[dict[str, Any]]
    aliases: list[dict[str, Any]]
    meta: dict[str, Any]
    status: Optional[str]
    is_default: bool
    sort_order: Optional[int]
    valid_from: Optional[str]
    valid_to: Optional[str]
    parent_code: Optional[str]
    superseded_by: Optional[str]


class SeedType(NamedTuple):
    """One ``lookup_type`` and its values, parsed and checked."""

    key: str
    name: str
    description: Optional[str]
    is_open: bool
    is_hierarchical: bool
    code_system: Optional[str]
    scope: Optional[str]
    default_sort: Optional[str]
    values: list[SeedValue]


def _reject_unknown(
    file_name: str, where: str, obj: dict[str, Any], allowed: frozenset[str]
) -> None:
    """Refuse a key the loader does not read, rather than dropping it in silence.

    This is the safety net the move to JSON removed. A typo in a Python literal
    was a ``NameError``; a typo in JSON is a key nobody reads, so ``lables`` would
    leave a value with no label at all and raise nothing.
    """
    for key in obj:
        if key in allowed:
            continue
        reason = _FORBIDDEN.get(key)
        if reason:
            raise ValueError(f"{file_name}: {where} sets {key!r} — {reason}")
        raise ValueError(
            f"{file_name}: {where} has unknown key {key!r}; "
            f"expected any of {sorted(allowed)}"
        )


def _parse_value(file_name: str, type_key: str, raw: dict[str, Any]) -> SeedValue:
    code = raw.get("code")
    if not code:
        raise ValueError(f"{file_name}: type {type_key!r} has a value with no 'code'")
    where = f"{type_key}:{code}"
    _reject_unknown(file_name, f"value {where!r}", raw, _VALUE_KEYS)

    translations = raw.get("translations") or []
    if not translations:
        raise ValueError(f"{file_name}: {where} has no translations")
    langs: set[str] = set()
    for entry in translations:
        _reject_unknown(file_name, f"translation on {where!r}", entry, _TRANSLATION_KEYS)
        lang, label = entry.get("lang"), entry.get("label")
        if not lang:
            raise ValueError(f"{file_name}: a translation on {where} has no 'lang'")
        if not label:
            raise ValueError(f"{file_name}: the {lang!r} translation on {where} has no 'label'")
        if lang in langs:
            # uq_translation_value_lang would refuse the second row mid-seed,
            # leaving the type half-written.
            raise ValueError(f"{file_name}: {where} has two {lang!r} translations")
        langs.add(lang)

    aliases = raw.get("aliases") or []
    for entry in aliases:
        _reject_unknown(file_name, f"alias on {where!r}", entry, _ALIAS_KEYS)
        if not entry.get("alias"):
            raise ValueError(f"{file_name}: an alias on {where} has no 'alias'")

    status = raw.get("status")
    if status is not None and status not in _STATUSES:
        raise ValueError(
            f"{file_name}: {where} has status {status!r}; "
            f"expected one of {sorted(_STATUSES)}"
        )

    for field in ("valid_from", "valid_to"):
        value = raw.get(field)
        if value is None:
            continue
        try:
            date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError(
                f"{file_name}: {where} has {field}={value!r}; "
                "expected an ISO date, YYYY-MM-DD"
            ) from None

    return SeedValue(
        code=code,
        translations=translations,
        aliases=aliases,
        meta=raw.get("meta") or {},
        status=status,
        is_default=bool(raw.get("is_default", False)),
        sort_order=raw.get("sort_order"),
        valid_from=raw.get("valid_from"),
        valid_to=raw.get("valid_to"),
        parent_code=raw.get("parent_code"),
        superseded_by=raw.get("superseded_by"),
    )


def _parse_type(file_name: str, raw: dict[str, Any]) -> SeedType:
    for required in ("key", "name"):
        if not raw.get(required):
            raise ValueError(
                f"{file_name}: a type has no {required!r} (got {sorted(raw)})"
            )
    key = raw["key"]
    _reject_unknown(file_name, f"type {key!r}", raw, _TYPE_KEYS)

    if raw.get("is_open") and raw.get("scope") != "org":
        # ensure_type raises the same way, but a file is worth failing before it
        # reaches a database: an open list means org users add values, and only an
        # org-owned type can host that.
        raise ValueError(
            f"{file_name}: type {key!r} sets is_open with scope "
            f"{raw.get('scope', 'platform')!r} — an open list requires scope 'org'"
        )

    values = [_parse_value(file_name, key, v) for v in raw.get("values") or []]

    codes = [v.code for v in values]
    if len(set(codes)) != len(codes):
        dupes = sorted({c for c in codes if codes.count(c) > 1})
        raise ValueError(f"{file_name}: type {key!r} has duplicate code(s) {dupes}")

    # Cross-value pointers, checked once every code in the type is known, so a
    # broken file fails before it touches a database.
    known = set(codes)
    hierarchical = bool(raw.get("is_hierarchical"))
    for value in values:
        for field, target in (
            ("parent_code", value.parent_code),
            ("superseded_by", value.superseded_by),
        ):
            if target is None:
                continue
            if target == value.code:
                raise ValueError(
                    f"{file_name}: {key}:{value.code} sets {field} to itself"
                )
            if target not in known:
                raise ValueError(
                    f"{file_name}: {key}:{value.code} sets {field}={target!r}, "
                    f"which is not a code of type {key!r}"
                )
        if value.parent_code and not hierarchical:
            # parent_id on a flat type populates a column no read path walks — a
            # hierarchy that silently is not one.
            raise ValueError(
                f"{file_name}: {key}:{value.code} sets a parent_code, but type "
                f"{key!r} is not declared is_hierarchical"
            )

    return SeedType(
        key=key,
        name=raw["name"],
        description=raw.get("description"),
        is_open=bool(raw.get("is_open", False)),
        is_hierarchical=hierarchical,
        code_system=raw.get("code_system"),
        scope=raw.get("scope"),
        default_sort=raw.get("default_sort"),
        values=values,
    )


def load(path: Path = SEED_FILE) -> list[SeedType]:
    """Parse and check one seed file. Takes a path so a host can load its own."""
    doc = json.loads(path.read_text(encoding="utf-8"))

    declared = doc.get("$schema")
    if declared != SCHEMA:
        raise ValueError(
            f"{path.name}: $schema is {declared!r}; this loader reads {SCHEMA!r}"
        )

    raw_types = doc.get("types")
    if not raw_types:
        raise ValueError(f"{path.name}: no types declared")

    types = [_parse_type(path.name, t) for t in raw_types]
    keys = [t.key for t in types]
    if len(set(keys)) != len(keys):
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        # The second entry would silently reuse the first's type row and append
        # its values to that vocabulary.
        raise ValueError(f"{path.name}: type key {dupes} declared twice")
    return types


def types() -> Iterator[SeedType]:
    """Every type shipped in ``seed.json``, in file order.

    Order carries no meaning -- the types are independent and none references
    another -- but it is stable, so a seed run is reproducible and surrogate ids
    do not shuffle between boots of different releases.
    """
    yield from load()
