"""Idempotent seeding of the starter lookup types and values.

Run on startup (after ``migrate``). Safe to call repeatedly: types are matched by ``key``
and values by ``code`` within a type, so nothing is duplicated.
"""

from typing import Optional

from sqlmodel import Session, select

from .data.countries import COUNTRIES
from .models import (
    LookupAlias,
    LookupTranslation,
    LookupType,
    LookupValue,
    SortMode,
    TypeScope,
)

# Closed (admin-managed) lists. Each value: code, en, ar.
_SALUTATION = [
    ("mr", "Mr.", "السيد"),
    ("mrs", "Mrs.", "السيدة"),
    ("ms", "Ms.", "الآنسة/السيدة"),
    ("miss", "Miss", "الآنسة"),
    ("dr", "Dr.", "الدكتور"),
    ("prof", "Prof.", "الأستاذ"),
    ("eng", "Eng.", "المهندس"),
    ("sheikh", "Sheikh", "الشيخ"),
    ("sir", "Sir", "سير"),
]

# Salutations that appear in the member's display name ("Dr. Jane"); everyday honorifics
# (mr/mrs/…) stay form-only. Seeded as `show_in_name` meta, backfilled only while the key
# is absent — an admin's explicit true/false edit is never overwritten.
_SALUTATION_IN_NAME = ("dr", "prof", "eng", "sheikh", "sir")

_GENDER = [
    ("male", "Male", "ذكر"),
    ("female", "Female", "أنثى"),
    ("other", "Other", "آخر"),
]

_MARITAL = [
    ("single", "Single", "أعزب"),
    ("married", "Married", "متزوج"),
    ("divorced", "Divorced", "مطلّق"),
    ("widowed", "Widowed", "أرمل"),
]

# Salary currencies (WXL-180) — closed list, ISO 4217 codes; AED first (the default).
_CURRENCY = [
    ("AED", "UAE Dirham", "درهم إماراتي"),
    ("USD", "US Dollar", "دولار أمريكي"),
    ("EUR", "Euro", "يورو"),
    ("GBP", "Pound Sterling", "جنيه إسترليني"),
    ("SAR", "Saudi Riyal", "ريال سعودي"),
    ("QAR", "Qatari Riyal", "ريال قطري"),
    ("KWD", "Kuwaiti Dinar", "دينار كويتي"),
    ("BHD", "Bahraini Dinar", "دينار بحريني"),
    ("OMR", "Omani Rial", "ريال عماني"),
    ("EGP", "Egyptian Pound", "جنيه مصري"),
    ("INR", "Indian Rupee", "روبية هندية"),
]

# Contract / engagement type (how the org engages a person). Synonyms are seeded as
# aliases so search resolves "staff aug", "SOW", "contractor", … to the right code.
# Each value: code, en, ar, aliases.
# Social platforms (WXL-198) — closed list backing member_social.platform_code. A new
# platform is a lookup row, not a migration. Each value: code, en, ar.
# Emergency-contact relationship (WXL-198) — closed list. Each value: code, en, ar.
# Project health (RAG) — a closed list. ``meta.tone`` drives the badge color in the UI,
# so admins can add or recolor values without a code change. Each value: code, en, ar, tone.
# Work-item statuses (WXL-203) — closed, admin-extensible list. ``meta.category`` is the
# FIXED engine vocabulary (backlog|unstarted|started|completed|canceled) that drives board
# columns, rollups and lifecycle timestamps; ``meta.tone`` the badge color. Admins may add
# or rename statuses freely (vocabulary); the engine keys only on category — the one idea
# Linear, Jira, Asana and Azure DevOps all converge on (docs/src/content/docs/architecture/decisions/0008-work-items.md §4.2).
# Each value: code, en, ar, category, tone.
# Work-item types (WXL-203) — closed but admin-extensible; seeded with the generic `task`
# only (no dev jargon like bug/story — owner decision 2026-07-09). Type NEVER drives
# behavior; it's classification vocabulary.
# Risk & issue register categories — closed, admin-managed lists. Each value: code, en, ar.

def ensure_type(session: Session, **kwargs) -> LookupType:
    t = session.exec(
        select(LookupType).where(LookupType.key == kwargs["key"])
    ).first()
    # Effective scope (issue #35): the explicit declaration, else the stored
    # one for an existing type — an idempotent boot re-registration that omits
    # scope must not judge is_open against the platform default — else the
    # platform default for a new type.
    explicit = kwargs.get("scope")
    if explicit is not None:
        scope = TypeScope(explicit)
    else:
        scope = t.scope if t else TypeScope.platform
    # An open list means org users add values, which only an org-owned type
    # can host — a platform type is never open.
    if kwargs.get("is_open") and scope is not TypeScope.org:
        raise ValueError(
            f"lookup type {kwargs.get('key')!r}: is_open=True requires "
            "scope='org' — platform types never accept org-added values"
        )
    if t:
        if explicit is not None and t.scope is not scope:
            # A silently ignored mismatch would let a host believe its
            # declaration took effect. Changing a type's scope moves ownership
            # of every value (platform rows become an unserved template, or
            # vice versa) — that is a deliberate data migration, never an
            # ensure_type side effect.
            raise ValueError(
                f"lookup type {kwargs['key']!r} already exists with scope "
                f"'{t.scope.value}', not '{scope.value}' — changing a type's "
                "scope is a data migration, not something ensure_type does"
            )
        return t
    t = LookupType(**kwargs)
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


def ensure_value(
    session: Session,
    type_id: int,
    code: str,
    translations: list[tuple[str, str]],
    *,
    sort_order: int = 0,
    aliases: Optional[list[str]] = None,
    meta: Optional[dict] = None,
) -> bool:
    """Create the value + translations + aliases if it doesn't already exist.

    Returns True if seeding changed anything (new value inserted, or a label-less
    value healed), False otherwise — callers use this to know whether to bump the
    type ``version`` (which busts read-API ETags).
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
        for lang, label in translations:
            session.add(
                LookupTranslation(value_id=existing.id, lang=lang, label=label)
            )
        present = {
            a.alias
            for a in session.exec(
                select(LookupAlias).where(LookupAlias.value_id == existing.id)
            )
        }
        for alias in aliases or []:
            if alias not in present:
                session.add(LookupAlias(value_id=existing.id, alias=alias))
        session.commit()
        return True
    value = LookupValue(
        type_id=type_id, code=code, sort_order=sort_order, meta=meta or {}
    )
    session.add(value)
    # flush (not commit) assigns value.id while keeping value + translations +
    # aliases in one transaction — a crash mid-seed can't strand a bare value.
    session.flush()
    for lang, label in translations:
        session.add(LookupTranslation(value_id=value.id, lang=lang, label=label))
    for alias in aliases or []:
        session.add(LookupAlias(value_id=value.id, alias=alias))
    session.commit()
    return True


def bump_version_if(session: Session, type_: LookupType, added: int) -> None:
    """Bump the type version when seeding inserted new values, so the read-API ETag
    (keyed on the version) changes and clients don't serve a stale cached list."""
    if added:
        type_.version += 1
        session.add(type_)
        session.commit()


def seed_lookups(session: Session) -> None:
    # --- Closed lists (curated order) ---
    salutation = ensure_type(
        session,
        key="salutation",
        name="Salutation",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        ensure_value(
            session, salutation.id, code, [("en", en), ("ar", ar)], sort_order=i
        )
        for i, (code, en, ar) in enumerate(_SALUTATION)
    )
    flagged = 0
    for code in _SALUTATION_IN_NAME:
        v = session.exec(
            select(LookupValue).where(
                LookupValue.type_id == salutation.id,
                LookupValue.code == code,
                LookupValue.org_id.is_(None),  # the seed owns platform rows only
            )
        ).first()
        if v is not None and "show_in_name" not in (v.meta or {}):
            v.meta = {**(v.meta or {}), "show_in_name": True}
            session.add(v)
            flagged += 1
    if flagged:
        session.commit()
    bump_version_if(session, salutation, added + flagged)

    gender = ensure_type(
        session,
        key="gender",
        name="Gender",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        ensure_value(session, gender.id, code, [("en", en), ("ar", ar)], sort_order=i)
        for i, (code, en, ar) in enumerate(_GENDER)
    )
    bump_version_if(session, gender, added)

    marital = ensure_type(
        session,
        key="marital_status",
        name="Marital status",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        ensure_value(session, marital.id, code, [("en", en), ("ar", ar)], sort_order=i)
        for i, (code, en, ar) in enumerate(_MARITAL)
    )
    bump_version_if(session, marital, added)

    currency = ensure_type(
        session,
        key="currency",
        name="Currency",
        code_system="iso4217",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        ensure_value(
            session, currency.id, code, [("en", en), ("ar", ar)], sort_order=i
        )
        for i, (code, en, ar) in enumerate(_CURRENCY)
    )
    bump_version_if(session, currency, added)

    # Project roles were an open lookup type here from TEAMY-291 until TEAMY-487
    # promoted them to a first-class `project_role` entity in the host (Teamy) —
    # the seed is retired; hosts adopt any surviving lookup values into their own
    # catalog at boot (idempotent) and this library never re-creates the type.
    #
    # TEAMY-803 took that further: seventeen types that were either a host's
    # domain objects (work items, project health, risks) or a value set someone
    # chose rather than one the world agrees on (contract types, social
    # platforms, next-of-kin relationships, and the open CV vocabularies) left
    # for the same reason. What remains below is standards-based or near-
    # universal to any people system. A host's own words belong to the host —
    # the library seeding them meant a second host inherited the first one's
    # product vocabulary without asking.

    # --- Countries & nationalities (alphabetical, ISO codes) ---
    country = ensure_type(
        session,
        key="country",
        name="Country",
        code_system="ISO3166-1A2",
        default_sort=SortMode.label,
    )
    nationality = ensure_type(
        session,
        key="nationality",
        name="Nationality",
        code_system="ISO3166-1A2",
        default_sort=SortMode.label,
    )
    country_added = 0
    nationality_added = 0
    for c in COUNTRIES:
        country_added += ensure_value(
            session,
            country.id,
            c["code"],
            [("en", c["country_en"]), ("ar", c["country_ar"])],
            aliases=c["aliases"],
            meta={"iso2": c["code"]},
        )
        nationality_added += ensure_value(
            session,
            nationality.id,
            c["code"],
            [("en", c["nat_en"]), ("ar", c["nat_ar"])],
            aliases=c["aliases"],
            meta={"iso2": c["code"]},
        )
    bump_version_if(session, country, country_added)
    bump_version_if(session, nationality, nationality_added)
