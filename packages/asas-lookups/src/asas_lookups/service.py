"""Lookup query, resolve and admin logic.

v1 keeps it simple: list/search loads a type's values into memory and filters in
Python. That's fine for the small/medium types we have (gender, marital status,
countries ~hundreds). When a large open type (e.g. universities, ~25k) lands, switch
``list_values`` to SQL-side filtering + pagination — the API contract won't change.
"""

import hashlib
import re
from datetime import datetime
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from .models import (
    LookupAlias,
    LookupStatus,
    LookupTranslation,
    LookupType,
    LookupValue,
    SortMode,
)
from .schemas import (
    LookupValueRead,
    TranslationIn,
)

DEFAULT_LANG = "en"

# ---------- per-org values (WXL-241, TENANCY_RESEARCH.md §7) ----------
# NULL org_id = platform default; an org row shadows a global row with the same code.
# The package stays self-contained: app wiring configures how to read the caller's
# org from a session (tenancy context); unconfigured/no-org sessions (seeds, boot)
# see and create GLOBAL rows only.

_org_resolver: Optional[Callable[[Session], Optional[int]]] = None


def configure_org_resolver(fn: Optional[Callable[[Session], Optional[int]]]) -> None:
    global _org_resolver
    _org_resolver = fn


def _current_org(session: Session) -> Optional[int]:
    return _org_resolver(session) if _org_resolver is not None else None


def _org_scoped(stmt, session: Session):
    """Restrict a LookupValue select to rows the caller may see: platform defaults
    plus their own org's rows. Tree-aware by design: when sub-orgs arrive, this walks
    ``parent_org_id`` up before falling back to NULL — today the chain is one org."""
    org = _current_org(session)
    if org is None:
        return stmt.where(LookupValue.org_id.is_(None))
    return stmt.where(or_(LookupValue.org_id.is_(None), LookupValue.org_id == org))


def _prefer_org(values: list) -> list:
    """Collapse code duplicates (org override + global) keeping the org row."""
    by_code: dict = {}
    for v in values:  # org rows win regardless of input order
        held = by_code.get(v.code)
        if held is None or (held.org_id is None and v.org_id is not None):
            by_code[v.code] = v
    return list(by_code.values())


# ---------- type / value lookups ----------


def get_type(session: Session, key: str) -> LookupType:
    t = session.exec(select(LookupType).where(LookupType.key == key)).first()
    if not t:
        raise HTTPException(status_code=404, detail=f"Unknown lookup type '{key}'")
    return t


def _value_by_code(session: Session, type_id: int, code: str) -> Optional[LookupValue]:
    stmt = _org_scoped(
        select(LookupValue).where(
            LookupValue.type_id == type_id, LookupValue.code == code
        ),
        session,
    ).order_by(LookupValue.org_id.is_(None))  # org override (False) sorts first
    return session.exec(stmt).first()


def value_exists(session: Session, type_key: str, code: str) -> bool:
    """Whether a value with this code exists on the type (org overrides honored).
    False for an unknown type — callers validating references get one seam."""
    t = session.exec(select(LookupType).where(LookupType.key == type_key)).first()
    return t is not None and _value_by_code(session, t.id, code) is not None


def _translation_for(value: LookupValue, lang: str) -> Optional[LookupTranslation]:
    by_lang = {t.lang: t for t in value.translations}
    return (
        by_lang.get(lang)
        or by_lang.get(DEFAULT_LANG)
        or (value.translations[0] if value.translations else None)
    )


def _label_str(value: LookupValue, lang: str) -> str:
    t = _translation_for(value, lang)
    return t.label if t else value.code


# ---------- serialization ----------


def serialize_value(
    value: LookupValue, lang: str, code_map: Optional[dict[int, str]] = None
) -> LookupValueRead:
    t = _translation_for(value, lang)
    parent_code = None
    if value.parent_id is not None and code_map is not None:
        parent_code = code_map.get(value.parent_id)
    return LookupValueRead(
        code=value.code,
        label=t.label if t else value.code,
        short_label=t.short_label if t else None,
        lang=t.lang if t else DEFAULT_LANG,
        is_default=value.is_default,
        sort_order=value.sort_order,
        status=value.status.value,
        parent_code=parent_code,
        meta=value.meta or {},
        aliases=sorted(a.alias for a in value.aliases),
    )


# ---------- listing / search ----------


def list_values(
    session: Session,
    type_: LookupType,
    lang: str,
    active_only: bool,
    q: Optional[str],
    parent_code: Optional[str],
    page: int,
    page_size: int,
) -> tuple[int, list[LookupValueRead]]:
    # Eager-load what _matches/_label_str/serialize_value read per value — lazy
    # loading here is an N+1 (2 queries × every value of the type) that turns
    # multi-thousand-row open types (skill, company) into seconds on Postgres.
    stmt = _org_scoped(
        select(LookupValue)
        .options(
            selectinload(LookupValue.translations),
            selectinload(LookupValue.aliases),
        )
        .where(LookupValue.type_id == type_.id),
        session,
    )
    if parent_code:
        # All visible rows carrying the code: children may reference either the
        # global parent's id or an org override's id (copy-on-write).
        parents = session.exec(
            _org_scoped(
                select(LookupValue).where(
                    LookupValue.type_id == type_.id, LookupValue.code == parent_code
                ),
                session,
            )
        ).all()
        parent_ids = [p.id for p in parents] or [-1]
        stmt = stmt.where(LookupValue.parent_id.in_(parent_ids))
    values = _prefer_org(list(session.exec(stmt).all()))

    # Status filters AFTER the shadow collapse (DR 0001 T3): an org's
    # deprecated override must keep shadowing its global row — filtering in SQL
    # first dropped the override and let the platform value reappear for the
    # very tenant that retired it (tombstone semantics).
    if active_only:
        values = [v for v in values if v.status == LookupStatus.active]

    if q:
        needle = q.strip().lower()
        values = [v for v in values if _matches(v, needle)]

    if type_.default_sort == SortMode.sort_order:
        values.sort(key=lambda v: (v.sort_order, _label_str(v, lang).lower()))
    else:
        values.sort(key=lambda v: _label_str(v, lang).lower())

    total = len(values)
    start = (page - 1) * page_size
    window = values[start : start + page_size]

    code_map = {v.id: v.code for v in session.exec(
        _org_scoped(select(LookupValue).where(LookupValue.type_id == type_.id), session)
    ).all()}
    return total, [serialize_value(v, lang, code_map) for v in window]


def _matches(value: LookupValue, needle: str) -> bool:
    hay = [value.code.lower()]
    hay += [t.label.lower() for t in value.translations]
    hay += [t.short_label.lower() for t in value.translations if t.short_label]
    hay += [a.alias.lower() for a in value.aliases]
    return any(needle in h for h in hay)


def get_value_read(
    session: Session, type_: LookupType, code: str, lang: str, follow_supersede: bool
) -> LookupValueRead:
    value = _value_by_code(session, type_.id, code)
    if not value:
        raise HTTPException(
            status_code=404, detail=f"Unknown code '{code}' in '{type_.key}'"
        )
    if follow_supersede and value.superseded_by_id is not None:
        # Scoped, not session.get (DR 0001 T3): a pointer into a row outside
        # the caller's visible set (another org's override) behaves as absent
        # rather than leaking that org's labels.
        replacement = session.exec(
            _org_scoped(
                select(LookupValue).where(LookupValue.id == value.superseded_by_id),
                session,
            )
        ).first()
        if replacement:
            value = replacement
    code_map = {value.id: value.code}
    if value.parent_id is not None:
        parent = session.get(LookupValue, value.parent_id)
        if parent:
            code_map[parent.id] = parent.code
    return serialize_value(value, lang, code_map)


def label_maps(
    session: Session, type_keys: list[str], lang: str = DEFAULT_LANG
) -> dict[str, dict[str, str]]:
    """{type_key: {code: label}} for the given types — fetch once, resolve many.

    Used by member serialization/listing to label identity codes and skills without an
    N+1 per record."""
    out: dict[str, dict[str, str]] = {}
    for key in type_keys:
        t = session.exec(select(LookupType).where(LookupType.key == key)).first()
        if not t:
            out[key] = {}
            continue
        values = session.exec(
            _org_scoped(
                select(LookupValue)
                # _label_str reads translations — eager-load or it's an N+1 per value.
                .options(selectinload(LookupValue.translations))
                .where(LookupValue.type_id == t.id),
                session,
            ).order_by(LookupValue.org_id.is_(None).desc())  # global first, org overwrites
        ).all()
        out[key] = {v.code: _label_str(v, lang) for v in values}
    return out


def value_meta_map(session: Session, type_key: str) -> dict[str, dict]:
    """{code: meta} for every value of a type — fetch once, resolve many (the bulk
    sibling of ``value_meta``, mirroring ``label_maps``; used by work-item serialization
    to read ``work_item_status`` category/tone without an N+1 per row)."""
    t = session.exec(select(LookupType).where(LookupType.key == type_key)).first()
    if not t:
        return {}
    values = session.exec(
        _org_scoped(select(LookupValue).where(LookupValue.type_id == t.id), session)
        .order_by(LookupValue.org_id.is_(None).desc())  # global first, org overwrites
    ).all()
    return {v.code: (v.meta or {}) for v in values}


def value_meta(session: Session, type_key: str, code: Optional[str]) -> dict:
    """The ``meta`` dict for one value; empty when the type/code is unknown or null."""
    if not code:
        return {}
    t = session.exec(select(LookupType).where(LookupType.key == type_key)).first()
    if not t:
        return {}
    value = _value_by_code(session, t.id, code)
    return (value.meta or {}) if value else {}


def get_or_create_value(
    session: Session, type_key: str, label: str, lang: str = DEFAULT_LANG
) -> str:
    """Return the code for ``label`` in an open type, creating the value if needed.

    Used when a user adds a free-form value (e.g. a skill). Idempotent by derived code."""
    t = get_type(session, type_key)
    label = label.strip()
    code = slugify(label)
    existing = _value_by_code(session, t.id, code)
    if existing:
        return existing.code
    value = create_value(
        session,
        t,
        code=code,
        translations=[TranslationIn(lang=lang, label=label)],
        is_default=False,
        sort_order=0,
        parent_code=None,
        meta={},
        aliases=[],
    )
    return value.code


def find_value_code(session: Session, type_key: str, code: str) -> Optional[str]:
    """Return ``code`` if such a value exists in ``type_key``, else None.

    A read-only companion to :func:`get_or_create_value` for callers that hold a
    stored code and must not create anything (e.g. code-pinned skill upserts)."""
    t = get_type(session, type_key)
    value = _value_by_code(session, t.id, code)
    return value.code if value else None


def resolve_codes(
    session: Session, type_: LookupType, codes: list[str], lang: str
) -> dict[str, Optional[str]]:
    wanted = set(codes)
    rows = session.exec(
        _org_scoped(
            select(LookupValue)
            .options(selectinload(LookupValue.translations))
            .where(LookupValue.type_id == type_.id, LookupValue.code.in_(wanted)),
            session,
        ).order_by(LookupValue.org_id.is_(None).desc())  # global first, org overwrites
    ).all()
    found = {v.code: _label_str(v, lang) for v in rows}
    return {code: found.get(code) for code in codes}


# ---------- admin / mutations ----------

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.strip().lower()).strip("-")
    if slug:
        return slug
    # A label with no Latin letters/digits (e.g. an Arabic organization or skill
    # name) would otherwise collapse to one shared code and collide — derive a
    # stable code from the text instead. (Mirrored in the WXL-182 migration.)
    digest = hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"x{digest}"


def _touch_type(type_: LookupType) -> None:
    type_.version += 1
    type_.updated_at = datetime.utcnow()


def _apply_translations(
    session: Session, value: LookupValue, translations: list[TranslationIn]
) -> None:
    by_lang = {t.lang: t for t in value.translations}
    for incoming in translations:
        existing = by_lang.get(incoming.lang)
        if existing:
            existing.label = incoming.label
            existing.short_label = incoming.short_label
            session.add(existing)
        else:
            session.add(
                LookupTranslation(
                    value_id=value.id,
                    lang=incoming.lang,
                    label=incoming.label,
                    short_label=incoming.short_label,
                )
            )


def _value_for_write(session: Session, type_: LookupType, code: str) -> LookupValue:
    """Resolve the row a mutation may touch (DR 0001 T4). The write path never
    reuses the read path's selection: ``_value_by_code`` answers "what does the
    caller SEE" (org row OR global fallback), and mutating its fallback is how
    one org's edit used to land on the platform row every tenant shares.

    With org context: the caller's own org row — materialized copy-on-write
    from the global row when the org has no override yet. Without context
    (platform scope, DR 0001 T7): the global row only. 404 when nothing the
    caller may write exists."""
    org = _current_org(session)
    base = select(LookupValue).where(
        LookupValue.type_id == type_.id, LookupValue.code == code
    )
    if org is not None:
        own = session.exec(base.where(LookupValue.org_id == org)).first()
        if own:
            return own
    global_row = session.exec(base.where(LookupValue.org_id.is_(None))).first()
    if global_row is None:
        raise HTTPException(status_code=404, detail=f"Unknown code '{code}'")
    if org is None:
        return global_row
    return _materialize_override(session, global_row, org)


def _materialize_override(
    session: Session, global_row: LookupValue, org: int
) -> LookupValue:
    """Copy-on-write (DR 0001 D2): clone the platform row — translations and
    aliases included, so labels and the search haystack survive the shadow —
    as the caller's org override. The mutation then applies to the copy and
    the global row stays untouched for every other tenant."""
    copy = LookupValue(
        type_id=global_row.type_id,
        code=global_row.code,
        org_id=org,
        parent_id=global_row.parent_id,
        status=global_row.status,
        is_default=global_row.is_default,
        sort_order=global_row.sort_order,
        valid_from=global_row.valid_from,
        valid_to=global_row.valid_to,
        superseded_by_id=global_row.superseded_by_id,
        meta=dict(global_row.meta or {}),
    )
    session.add(copy)
    session.flush()  # id for the child rows
    for t in global_row.translations:
        session.add(
            LookupTranslation(
                value_id=copy.id, lang=t.lang, label=t.label, short_label=t.short_label
            )
        )
    for a in global_row.aliases:
        session.add(LookupAlias(value_id=copy.id, alias=a.alias, lang=a.lang))
    session.flush()
    session.refresh(copy)
    return copy


def create_value(
    session: Session,
    type_: LookupType,
    *,
    code: Optional[str],
    translations: list[TranslationIn],
    is_default: bool,
    sort_order: int,
    parent_code: Optional[str],
    meta: dict,
    aliases: list[str],
) -> LookupValue:
    if not translations:
        raise HTTPException(status_code=422, detail="At least one translation is required")

    if not code:
        # Derive a stable code from the English (or first) label for open lists.
        basis = next(
            (t.label for t in translations if t.lang == DEFAULT_LANG),
            translations[0].label,
        )
        code = slugify(basis)

    if _value_by_code(session, type_.id, code):
        raise HTTPException(
            status_code=409, detail=f"code '{code}' already exists in '{type_.key}'"
        )

    parent_id = None
    if parent_code:
        parent = _value_by_code(session, type_.id, parent_code)
        if not parent:
            raise HTTPException(status_code=422, detail=f"Unknown parent '{parent_code}'")
        parent_id = parent.id

    value = LookupValue(
        type_id=type_.id,
        code=code,
        parent_id=parent_id,
        is_default=is_default,
        sort_order=sort_order,
        meta=meta or {},
        # Request-scoped creations belong to the caller's org (open-type additions,
        # org-admin values); seed/boot sessions have no resolver → global (WXL-241).
        org_id=_current_org(session),
    )
    session.add(value)
    session.commit()
    session.refresh(value)

    _apply_translations(session, value, translations)
    for alias in aliases:
        session.add(LookupAlias(value_id=value.id, alias=alias))
    _touch_type(type_)
    session.add(type_)
    session.commit()
    session.refresh(value)
    return value


def update_value(
    session: Session,
    type_: LookupType,
    code: str,
    *,
    translations: Optional[list[TranslationIn]],
    is_default: Optional[bool],
    sort_order: Optional[int],
    meta: Optional[dict],
) -> LookupValue:
    value = _value_for_write(session, type_, code)
    if translations is not None:
        _apply_translations(session, value, translations)
    if is_default is not None:
        value.is_default = is_default
    if sort_order is not None:
        value.sort_order = sort_order
    if meta is not None:
        value.meta = meta
    value.updated_at = datetime.utcnow()
    session.add(value)
    _touch_type(type_)
    session.add(type_)
    session.commit()
    session.refresh(value)
    return value


def deprecate_value(
    session: Session,
    type_: LookupType,
    code: str,
    *,
    valid_to,
    superseded_by: Optional[str],
) -> LookupValue:
    value = _value_for_write(session, type_, code)
    value.status = LookupStatus.deprecated
    value.valid_to = valid_to or datetime.utcnow().date()
    if superseded_by:
        target = _value_by_code(session, type_.id, superseded_by)
        if not target:
            raise HTTPException(
                status_code=422, detail=f"Unknown superseded_by '{superseded_by}'"
            )
        value.superseded_by_id = target.id
    value.updated_at = datetime.utcnow()
    session.add(value)
    _touch_type(type_)
    session.add(type_)
    session.commit()
    session.refresh(value)
    return value


def add_alias(
    session: Session, type_: LookupType, code: str, alias: str, lang: Optional[str]
) -> LookupValue:
    value = _value_for_write(session, type_, code)
    session.add(LookupAlias(value_id=value.id, alias=alias, lang=lang))
    _touch_type(type_)
    session.add(type_)
    session.commit()
    session.refresh(value)
    return value


def remove_alias(
    session: Session, type_: LookupType, code: str, alias: str
) -> LookupValue:
    """Delete every alias row matching ``alias`` on the value (idempotent: removing an
    alias that isn't there is a no-op, not an error)."""
    value = _value_for_write(session, type_, code)
    for row in [a for a in value.aliases if a.alias == alias]:
        session.delete(row)
    _touch_type(type_)
    session.add(type_)
    session.commit()
    session.refresh(value)
    return value


def merge_values(
    session: Session, type_: LookupType, code: str, into: str
) -> LookupValue:
    """Deprecate ``code`` and point it at ``into`` (dedup for open lists). The old label
    is kept as an alias on the target so search still finds it."""
    if code == into:
        raise HTTPException(status_code=422, detail="Cannot merge a value into itself")
    # Both sides are mutations (source deprecates, target gains aliases), so
    # both resolve through the write path — an org merge copies-on-write both
    # rows and leaves the platform values untouched for every other tenant.
    source = _value_for_write(session, type_, code)
    target = _value_for_write(session, type_, into)
    for t in source.translations:
        session.add(LookupAlias(value_id=target.id, alias=t.label, lang=t.lang))
    source.status = LookupStatus.deprecated
    source.superseded_by_id = target.id
    source.valid_to = datetime.utcnow().date()
    source.updated_at = datetime.utcnow()
    session.add(source)
    _touch_type(type_)
    session.add(type_)
    session.commit()
    session.refresh(target)
    return target
