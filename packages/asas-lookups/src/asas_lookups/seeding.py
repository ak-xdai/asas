"""Idempotent seeding of the starter lookup types and values.

Run on startup (after ``migrate``). Safe to call repeatedly: types are matched by ``key``
and values by ``code`` within a type, so nothing is duplicated.
"""

from datetime import date
from pathlib import Path
from typing import Any, Optional, Sequence, Tuple, Union

from sqlmodel import Session, select

from .data import SeedType, load, types
from .models import (
    LookupAlias,
    LookupStatus,
    LookupTranslation,
    LookupType,
    LookupValue,
    SortMode,
    TypeScope,
)


def ensure_type(
    session: Session,
    *,
    key: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_open: bool = False,
    is_hierarchical: bool = False,
    code_system: Optional[str] = None,
    default_sort: SortMode = SortMode.label,
    scope: Optional[TypeScope | str] = None,
    **extra: Any,
) -> LookupType:
    """Create the lookup type if it does not exist; return it either way.

    ``key`` is the type's stable machine name (``"nationality"``,
    ``"ticket_priority"``) and is what everything else references — **not**
    ``code``, which is the field on a *value*. Getting those two the wrong way
    round is the common first mistake, and it used to surface as a bare
    ``KeyError: 'key'`` because this function forwarded ``**kwargs`` straight to
    the model and named nothing.

    ``name`` defaults to ``key`` so a caller seeding its own vocabulary can
    supply one argument. ``default_sort`` picks label order or explicit
    ``sort_order``.

    ``scope`` declares who owns the values (issue #35) and is **never
    inferred**. Omitting it keeps an existing type's stored scope and defaults a
    new one to ``platform``. ``is_open`` — a list org users may extend — is only
    legal on an org-scoped type.

    ``**extra`` remains only for model fields with no reason to be promoted; the
    parameters above are the contract, and are what ``inspect.signature`` shows.

    Idempotent, and **matched on key alone**: an existing type is returned
    unchanged, so this never rewrites a deployment's edited label.
    """
    t = session.exec(select(LookupType).where(LookupType.key == key)).first()
    # Effective scope (issue #35): the explicit declaration, else the stored
    # one for an existing type — an idempotent boot re-registration that omits
    # scope must not judge is_open against the platform default — else the
    # platform default for a new type.
    explicit = scope
    effective_scope = (
        TypeScope(explicit) if explicit is not None
        else (t.scope if t else TypeScope.platform)
    )
    # An open list means org users add values, which only an org-owned type
    # can host — a platform type is never open.
    if is_open and effective_scope is not TypeScope.org:
        raise ValueError(
            f"lookup type {key!r}: is_open=True requires "
            "scope='org' — platform types never accept org-added values"
        )
    if t:
        if explicit is not None and t.scope is not effective_scope:
            # A silently ignored mismatch would let a host believe its
            # declaration took effect. Changing a type's scope moves ownership
            # of every value (platform rows become an unserved template, or
            # vice versa) — that is a deliberate data migration, never an
            # ensure_type side effect.
            raise ValueError(
                f"lookup type {key!r} already exists with scope "
                f"'{t.scope.value}', not '{effective_scope.value}' — changing a type's "
                "scope is a data migration, not something ensure_type does"
            )
        return t
    t = LookupType(
        key=key,
        name=name if name is not None else key,
        description=description,
        is_open=is_open,
        is_hierarchical=is_hierarchical,
        code_system=code_system,
        default_sort=default_sort,
        scope=effective_scope,
        **extra,
    )
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def seed_org_lookups(session: Session, org_id: int) -> int:
    """Copy every org-scoped type's platform-held starter template into
    ``org_id``-owned rows (issue #35). The host calls this at org creation;
    it is presence-idempotent per (type, code), so re-running — or
    backfilling an existing org — never duplicates and never overwrites the
    org's own edits. Returns the number of values created.

    Platform template rows are never served to org reads; after this call the
    org owns its list outright (template drift is accepted by design).
    Hierarchies survive the copy: parent pointers are remapped to the org's
    own copies in a second pass."""
    created = 0
    types = session.exec(
        select(LookupType).where(LookupType.scope == TypeScope.org)
    ).all()
    for t in types:
        templates = session.exec(
            select(LookupValue).where(
                LookupValue.type_id == t.id, LookupValue.org_id.is_(None)
            )
        ).all()
        tmpl_by_id = {tmpl.id: tmpl for tmpl in templates}
        own_by_code = {
            row.code: row
            for row in session.exec(
                select(LookupValue).where(
                    LookupValue.type_id == t.id, LookupValue.org_id == org_id
                )
            ).all()
        }
        copies: dict[int, LookupValue] = {}  # template id -> org copy
        type_created = 0
        for tmpl in templates:
            if tmpl.code in own_by_code:
                continue
            copy = LookupValue(
                type_id=t.id,
                code=tmpl.code,
                org_id=org_id,
                status=tmpl.status,
                is_default=tmpl.is_default,
                sort_order=tmpl.sort_order,
                valid_from=tmpl.valid_from,
                valid_to=tmpl.valid_to,
                meta=dict(tmpl.meta or {}),
            )
            session.add(copy)
            # One flush per row: a multi-row VALUES insert casts to the
            # native enum type name, which the migration-built Postgres
            # schema (VARCHAR columns) doesn't have.
            session.flush()
            for tr in tmpl.translations:
                session.add(
                    LookupTranslation(
                        value_id=copy.id,
                        lang=tr.lang,
                        label=tr.label,
                        short_label=tr.short_label,
                    )
                )
            for a in tmpl.aliases:
                session.add(LookupAlias(value_id=copy.id, alias=a.alias, lang=a.lang))
            copies[tmpl.id] = copy
            type_created += 1
        def org_row_for(template_id: Optional[int]) -> Optional[LookupValue]:
            # A template row id resolved to the org's own row: the copy made
            # in this call, or the row the org already had for that code
            # (which idempotency skipped).
            if template_id is None:
                return None
            row = copies.get(template_id)
            if row is None:
                ref = tmpl_by_id.get(template_id)
                if ref is not None:
                    row = own_by_code.get(ref.code)
            return row

        # Second pass: parent and supersede pointers land on the org's own
        # rows — never back into the template.
        for tmpl in templates:
            copy = copies.get(tmpl.id)
            if copy is None:
                continue
            parent = org_row_for(tmpl.parent_id)
            if parent is not None:
                copy.parent_id = parent.id
            successor = org_row_for(tmpl.superseded_by_id)
            if successor is not None:
                copy.superseded_by_id = successor.id
            if parent is not None or successor is not None:
                session.add(copy)
        if type_created:
            # The read-API ETag keys on the type version: without a bump, an
            # org that cached a response before being seeded (e.g. an empty
            # list) would keep revalidating to 304 against stale content.
            t.version += 1
            session.add(t)
        created += type_created
    session.commit()
    return created


#: One alias as seeded: a bare string means "any language", a pair pins it to one.
#: The bare form is kept because it is what every existing caller passes.
AliasSpec = Union[str, Tuple[str, Optional[str]]]


def ensure_value(
    session: Session,
    type_id: int,
    code: str,
    translations: list[tuple[str, str]],
    *,
    sort_order: int = 0,
    aliases: Optional[Sequence[AliasSpec]] = None,
    meta: Optional[dict] = None,
    short_labels: Optional[dict[str, str]] = None,
    is_default: bool = False,
    status: Optional[LookupStatus | str] = None,
    valid_from: Optional[date] = None,
    valid_to: Optional[date] = None,
) -> bool:
    """Create the value + translations + aliases if it doesn't already exist.

    Returns True if seeding changed anything (new value inserted, or a label-less
    value healed), False otherwise — callers use this to know whether to bump the
    type ``version`` (which busts read-API ETags).

    Every keyword after ``translations`` writes a column of ``lookup_value`` (or
    of its translation rows) and applies **on insert only**: an existing value is
    never rewritten, so a deployment's own edits survive re-seeding. The two
    self-referencing columns, ``parent_id`` and ``superseded_by_id``, are not
    here — they point at other values by code, which is a resolution the caller
    has to do once every row exists (see ``_link_pass``).

    ``short_labels`` is a separate mapping rather than a third tuple element so
    the ``list[(lang, label)]`` shape every existing caller passes keeps working.
    """
    # Platform rows only (issue #24; DR 0001 T7): the seed runs with no org
    # context and owns only org-less rows — an org-minted row with the same
    # code must not suppress the platform default forever (audit defect T-5).
    existing = session.exec(
        select(LookupValue).where(
            LookupValue.type_id == type_id,
            LookupValue.code == code,
            LookupValue.org_id.is_(None),
        )
    ).first()
    if existing:
        has_labels = session.exec(
            select(LookupTranslation).where(
                LookupTranslation.value_id == existing.id
            )
        ).first()
        if has_labels is not None:
            return False
        # A value with zero translations is the leftover of a seed that died
        # between inserting the value and its labels (pre-fix two-commit window).
        # Backfill labels + aliases; values with any labels are never touched,
        # so admin label edits survive.
        _add_translations(session, existing.id, translations, short_labels)
        present = {
            a.alias
            for a in session.exec(
                select(LookupAlias).where(LookupAlias.value_id == existing.id)
            )
        }
        for alias, lang in _alias_pairs(aliases):
            if alias not in present:
                session.add(
                    LookupAlias(value_id=existing.id, alias=alias, lang=lang)
                )
        session.commit()
        return True
    value = LookupValue(
        type_id=type_id,
        code=code,
        sort_order=sort_order,
        meta=meta or {},
        is_default=is_default,
        valid_from=valid_from,
        valid_to=valid_to,
        **({"status": LookupStatus(status)} if status is not None else {}),
    )
    session.add(value)
    # flush (not commit) assigns value.id while keeping value + translations +
    # aliases in one transaction — a crash mid-seed can't strand a bare value.
    session.flush()
    _add_translations(session, value.id, translations, short_labels)
    for alias, lang in _alias_pairs(aliases):
        session.add(LookupAlias(value_id=value.id, alias=alias, lang=lang))
    session.commit()
    return True


def _alias_pairs(
    aliases: Optional[Sequence[AliasSpec]],
) -> list[tuple[str, Optional[str]]]:
    """Normalise the two accepted alias shapes to ``(alias, lang)``."""
    out: list[tuple[str, Optional[str]]] = []
    for spec in aliases or []:
        if isinstance(spec, str):
            out.append((spec, None))
        else:
            alias, lang = spec
            out.append((alias, lang))
    return out


def _add_translations(
    session: Session,
    value_id: int,
    translations: list[tuple[str, str]],
    short_labels: Optional[dict[str, str]],
) -> None:
    short = short_labels or {}
    for lang, label in translations:
        session.add(
            LookupTranslation(
                value_id=value_id,
                lang=lang,
                label=label,
                short_label=short.get(lang),
            )
        )


def bump_version_if(session: Session, type_: LookupType, added: int) -> None:
    """Bump the type version when seeding inserted new values, so the read-API ETag
    (keyed on the version) changes and clients don't serve a stale cached list."""
    if added:
        type_.version += 1
        session.add(type_)
        session.commit()


def _seed_type(session: Session, spec: SeedType) -> None:
    """Seed one type from the file: the ``lookup_type`` row, then its values.

    Two behaviours the hand-written per-type blocks carried are kept and
    generalised:

    * **Curated order** -- a value that names its own ``sort_order`` gets it. One
      that does not takes the array index when the type is sorted by
      ``sort_order``, so the order a reader sees in the file is the order the API
      returns, and 0 when the type is sorted by label, where nothing reads it.
    * **Meta backfill** -- ``meta`` reaches a NEW row through ``ensure_value``, but
      an existing row is only ever topped up with keys it does not already have.
      That rule arrived for the salutations' ``show_in_name`` flag, added after
      those rows had shipped, and it is a backfill rather than a write because an
      admin's explicit ``true``/``false`` must survive the next boot.
    """
    type_ = ensure_type(
        session,
        key=spec.key,
        name=spec.name,
        description=spec.description,
        code_system=spec.code_system,
        default_sort=SortMode(spec.default_sort or SortMode.label),
        scope=TypeScope(spec.scope) if spec.scope else None,
        is_open=spec.is_open,
        is_hierarchical=spec.is_hierarchical,
    )
    curated = type_.default_sort == SortMode.sort_order
    changed = 0

    for index, value in enumerate(spec.values):
        order = value.sort_order
        if order is None:
            order = index if curated else 0
        wrote = ensure_value(
            session,
            type_.id,
            value.code,
            [(t["lang"], t["label"]) for t in value.translations],
            sort_order=order,
            aliases=[(a["alias"], a.get("lang")) for a in value.aliases],
            meta=value.meta,
            short_labels={
                t["lang"]: t["short_label"]
                for t in value.translations
                if t.get("short_label")
            },
            is_default=value.is_default,
            status=value.status,
            valid_from=_as_date(value.valid_from),
            valid_to=_as_date(value.valid_to),
        )
        changed += wrote
        # Only a row this seed did NOT just write can be short of a meta key, and
        # only when an older release of the file lacked it. Skipping the probe
        # otherwise keeps seeding at one query per value rather than two.
        if value.meta and not wrote:
            changed += _backfill_meta(session, type_.id, value.code, value.meta)

    changed += _link_pass(session, type_.id, spec)
    bump_version_if(session, type_, changed)


def _link_pass(session: Session, type_id: int, spec: SeedType) -> int:
    """Second pass: resolve ``parent_code`` and ``superseded_by`` to row ids.

    Runs after every value of the type exists, so a file may name a parent that
    appears later in the array, or a replacement further down. Both pointers are
    filled **only while NULL** -- the same rule as the meta backfill, and for the
    same reason: a row an admin has re-parented must not be moved back at the next
    boot.
    """
    wanted = {
        v.code: (v.parent_code, v.superseded_by)
        for v in spec.values
        if v.parent_code or v.superseded_by
    }
    if not wanted:
        return 0

    rows = {
        r.code: r
        for r in session.exec(
            select(LookupValue).where(
                LookupValue.type_id == type_id, LookupValue.org_id.is_(None)
            )
        )
    }
    changed = 0
    for code, (parent_code, superseded_code) in wanted.items():
        row = rows.get(code)
        if row is None:
            continue
        for field, target_code in (
            ("parent_id", parent_code),
            ("superseded_by_id", superseded_code),
        ):
            if not target_code or getattr(row, field) is not None:
                continue
            target = rows.get(target_code)
            if target is None:
                raise ValueError(
                    f"{spec.key}:{code} points at {target_code!r}, which this "
                    "type has no value for"
                )
            setattr(row, field, target.id)
            session.add(row)
            changed = 1
    if changed:
        session.commit()
    return changed


def _as_date(raw: Optional[str]) -> Optional[date]:
    """An ISO date from the file, or None. Validated at load, so this only parses."""
    return date.fromisoformat(raw) if raw else None


def _backfill_meta(session: Session, type_id: int, code: str, meta: dict) -> int:
    """Add absent ``meta`` keys to an already-seeded platform row. Never overwrite.

    Returns 1 if anything was added, so the caller bumps the type version and the
    read-API ETag changes for clients holding the old list.
    """
    row = session.exec(
        select(LookupValue).where(
            LookupValue.type_id == type_id,
            LookupValue.code == code,
            LookupValue.org_id.is_(None),  # the seed owns platform rows only
        )
    ).first()
    if row is None:
        return 0
    current = row.meta or {}
    missing = {k: v for k, v in meta.items() if k not in current}
    if not missing:
        return 0
    row.meta = {**current, **missing}
    session.add(row)
    session.commit()
    return 1


def seed_lookups(session: Session) -> None:
    """Seed every type shipped in ``asas_lookups/data/seed.json``.

    What is shipped is standards-based or near-universal to any people system:
    salutation, gender, marital status, currency, country, nationality. A host's
    own words belong to the host -- TEAMY-803 removed seventeen types that were
    either a host's domain objects or a value set someone had merely chosen,
    because a second host was inheriting the first one's product vocabulary
    without asking. Seed your own with ``seed_file``.
    """
    for spec in types():
        _seed_type(session, spec)


def seed_file(session: Session, path: str | Path) -> None:
    """Seed a HOST's own vocabulary from a file in the same shape as ``seed.json``.

    The README has always told hosts their product's words are theirs to seed, and
    then offered only ``ensure_type`` / ``ensure_value`` -- so every adopting host
    writes the same loop over the same Python literals, which is the shape this
    package just moved its own data out of. This is that loop, once, with the file
    validated on the way in.
    """
    for spec in load(Path(path)):
        _seed_type(session, spec)
