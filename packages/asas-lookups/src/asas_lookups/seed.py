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

_TEAM_CATEGORY = [
    ("functional", "Functional", "وظيفي"),
    ("product", "Product", "منتج"),
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
_CONTRACT_TYPE = [
    (
        "direct_hire",
        "Direct Hire",
        "تعيين مباشر",
        ["employee", "permanent", "fte", "full-time", "badged"],
    ),
    (
        "contingent_worker",
        "Contingent Worker",
        "عامل مؤقت",
        [
            "long-term contractor",
            "contractor",
            "staff augmentation",
            "staff aug",
            "outsourced staff",
            "agency",
            "secondee",
        ],
    ),
    (
        "outsourced_service",
        "Outsourced Service (SOW)",
        "خدمة مُسندة",
        [
            "sow",
            "statement of work",
            "managed service",
            "vendor",
            "project contract",
            "deliverable-based",
            "outsourced delivery",
        ],
    ),
]

# Social platforms (WXL-198) — closed list backing member_social.platform_code. A new
# platform is a lookup row, not a migration. Each value: code, en, ar.
_SOCIAL_PLATFORM = [
    ("linkedin", "LinkedIn", "لينكد إن"),
    ("x", "X (Twitter)", "إكس (تويتر)"),
    ("instagram", "Instagram", "إنستغرام"),
    ("facebook", "Facebook", "فيسبوك"),
    ("github", "GitHub", "غيت هاب"),
    ("youtube", "YouTube", "يوتيوب"),
    ("tiktok", "TikTok", "تيك توك"),
    ("website", "Website", "موقع إلكتروني"),
    ("other", "Other", "آخر"),
]

# Emergency-contact relationship (WXL-198) — closed list. Each value: code, en, ar.
_CONTACT_RELATIONSHIP = [
    ("spouse", "Spouse", "الزوج/الزوجة"),
    ("parent", "Parent", "أحد الوالدين"),
    ("sibling", "Sibling", "الأخ/الأخت"),
    ("child", "Child", "الابن/الابنة"),
    ("relative", "Relative", "قريب"),
    ("friend", "Friend", "صديق"),
    ("colleague", "Colleague", "زميل"),
    ("other", "Other", "آخر"),
]

# Project health (RAG) — a closed list. ``meta.tone`` drives the badge color in the UI,
# so admins can add or recolor values without a code change. Each value: code, en, ar, tone.
_PROJECT_HEALTH = [
    ("on_track", "On track", "على المسار", "success"),
    ("at_risk", "At risk", "في خطر", "warning"),
    ("delayed", "Delayed", "متأخر", "danger"),
    ("on_hold", "On hold", "معلّق", "neutral"),
]

# Work-item statuses (WXL-203) — closed, admin-extensible list. ``meta.category`` is the
# FIXED engine vocabulary (backlog|unstarted|started|completed|canceled) that drives board
# columns, rollups and lifecycle timestamps; ``meta.tone`` the badge color. Admins may add
# or rename statuses freely (vocabulary); the engine keys only on category — the one idea
# Linear, Jira, Asana and Azure DevOps all converge on (docs/src/content/docs/architecture/decisions/0008-work-items.md §4.2).
# Each value: code, en, ar, category, tone.
_WORK_ITEM_STATUS = [
    ("backlog", "Backlog", "قائمة الأعمال", "backlog", "neutral"),
    ("todo", "Todo", "للتنفيذ", "unstarted", "neutral"),
    ("in_progress", "In Progress", "قيد التنفيذ", "started", "info"),
    ("in_review", "In Review", "قيد المراجعة", "started", "warning"),
    ("done", "Done", "منجز", "completed", "success"),
    ("canceled", "Canceled", "ملغى", "canceled", "neutral"),
]

# Work-item types (WXL-203) — closed but admin-extensible; seeded with the generic `task`
# only (no dev jargon like bug/story — owner decision 2026-07-09). Type NEVER drives
# behavior; it's classification vocabulary.
_WORK_ITEM_TYPE = [
    ("task", "Task", "مهمة"),
]

# Risk & issue register categories — closed, admin-managed lists. Each value: code, en, ar.
_RISK_CATEGORY = [
    ("technical", "Technical", "تقني"),
    ("schedule", "Schedule", "الجدول الزمني"),
    ("cost", "Cost", "التكلفة"),
    ("resource", "Resource", "الموارد"),
    ("scope", "Scope", "النطاق"),
    ("external", "External", "خارجي"),
]

_ISSUE_CATEGORY = [
    ("technical", "Technical", "تقني"),
    ("process", "Process", "العملية"),
    ("resource", "Resource", "الموارد"),
    ("quality", "Quality", "الجودة"),
    ("external", "External", "خارجي"),
]


def _ensure_type(session: Session, **kwargs) -> LookupType:
    t = session.exec(
        select(LookupType).where(LookupType.key == kwargs["key"])
    ).first()
    if t:
        return t
    t = LookupType(**kwargs)
    session.add(t)
    session.commit()
    session.refresh(t)
    return t


def _ensure_value(
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
    # Platform rows only (DR 0001 T7): the seed runs with no org context and
    # must not let an org-minted row of the same code suppress the platform
    # default — the two can coexist by design (the partial uniques).
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


def _bump_version_if(session: Session, type_: LookupType, added: int) -> None:
    """Bump the type version when seeding inserted new values, so the read-API ETag
    (keyed on the version) changes and clients don't serve a stale cached list."""
    if added:
        type_.version += 1
        session.add(type_)
        session.commit()


def seed_lookups(session: Session) -> None:
    # --- Closed lists (curated order) ---
    salutation = _ensure_type(
        session,
        key="salutation",
        name="Salutation",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(
            session, salutation.id, code, [("en", en), ("ar", ar)], sort_order=i
        )
        for i, (code, en, ar) in enumerate(_SALUTATION)
    )
    flagged = 0
    for code in _SALUTATION_IN_NAME:
        v = session.exec(
            select(LookupValue).where(
                LookupValue.type_id == salutation.id, LookupValue.code == code
            )
        ).first()
        if v is not None and "show_in_name" not in (v.meta or {}):
            v.meta = {**(v.meta or {}), "show_in_name": True}
            session.add(v)
            flagged += 1
    if flagged:
        session.commit()
    _bump_version_if(session, salutation, added + flagged)

    gender = _ensure_type(
        session,
        key="gender",
        name="Gender",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(session, gender.id, code, [("en", en), ("ar", ar)], sort_order=i)
        for i, (code, en, ar) in enumerate(_GENDER)
    )
    _bump_version_if(session, gender, added)

    marital = _ensure_type(
        session,
        key="marital_status",
        name="Marital status",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(session, marital.id, code, [("en", en), ("ar", ar)], sort_order=i)
        for i, (code, en, ar) in enumerate(_MARITAL)
    )
    _bump_version_if(session, marital, added)

    team_category = _ensure_type(
        session,
        key="team_category",
        name="Team category",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(
            session, team_category.id, code, [("en", en), ("ar", ar)], sort_order=i
        )
        for i, (code, en, ar) in enumerate(_TEAM_CATEGORY)
    )
    _bump_version_if(session, team_category, added)

    # Org/edu open types (WXL-182): values are user-created (get_or_create on write,
    # backfilled from historical free text by the WXL-183 migration) — only the type
    # registrations live here. One type per field so each picker offers plausible
    # values only (no PhD from Microsoft).
    for key, name in (
        ("company", "Company"),
        ("degree", "Degree"),
        ("field_of_study", "Field of study"),
        ("education_institution", "Education institution"),
        ("awarding_body", "Awarding body"),
        ("training_provider", "Training provider"),
    ):
        _ensure_type(
            session,
            key=key,
            name=name,
            is_open=True,
            code_system="internal",
            default_sort=SortMode.label,
        )

    currency = _ensure_type(
        session,
        key="currency",
        name="Currency",
        code_system="iso4217",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(
            session, currency.id, code, [("en", en), ("ar", ar)], sort_order=i
        )
        for i, (code, en, ar) in enumerate(_CURRENCY)
    )
    _bump_version_if(session, currency, added)

    project_health = _ensure_type(
        session,
        key="project_health",
        name="Project health",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(
            session,
            project_health.id,
            code,
            [("en", en), ("ar", ar)],
            sort_order=i,
            meta={"tone": tone},
        )
        for i, (code, en, ar, tone) in enumerate(_PROJECT_HEALTH)
    )
    _bump_version_if(session, project_health, added)

    work_item_status = _ensure_type(
        session,
        key="work_item_status",
        name="Work item status",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(
            session,
            work_item_status.id,
            code,
            [("en", en), ("ar", ar)],
            sort_order=i,
            meta={"category": category, "tone": tone},
        )
        for i, (code, en, ar, category, tone) in enumerate(_WORK_ITEM_STATUS)
    )
    _bump_version_if(session, work_item_status, added)

    work_item_type = _ensure_type(
        session,
        key="work_item_type",
        name="Work item type",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(
            session, work_item_type.id, code, [("en", en), ("ar", ar)], sort_order=i
        )
        for i, (code, en, ar) in enumerate(_WORK_ITEM_TYPE)
    )
    _bump_version_if(session, work_item_type, added)

    for type_key, type_name, values in (
        ("risk_category", "Risk category", _RISK_CATEGORY),
        ("issue_category", "Issue category", _ISSUE_CATEGORY),
        ("social_platform", "Social platform", _SOCIAL_PLATFORM),
        ("contact_relationship", "Contact relationship", _CONTACT_RELATIONSHIP),
    ):
        lt = _ensure_type(
            session,
            key=type_key,
            name=type_name,
            code_system="internal",
            default_sort=SortMode.sort_order,
        )
        added = sum(
            _ensure_value(session, lt.id, code, [("en", en), ("ar", ar)], sort_order=i)
            for i, (code, en, ar) in enumerate(values)
        )
        _bump_version_if(session, lt, added)

    contract_type = _ensure_type(
        session,
        key="contract_type",
        name="Contract Type",
        code_system="internal",
        default_sort=SortMode.sort_order,
    )
    added = sum(
        _ensure_value(
            session,
            contract_type.id,
            code,
            [("en", en), ("ar", ar)],
            sort_order=i,
            aliases=aliases,
        )
        for i, (code, en, ar, aliases) in enumerate(_CONTRACT_TYPE)
    )
    _bump_version_if(session, contract_type, added)

    # Office locations (WXL-198) — closed, admin-managed via /admin/lookups; seeded
    # empty (each org defines its own offices). Backs member_contact.office_location_code.
    _ensure_type(
        session,
        key="office_location",
        name="Office location",
        description="Admin-managed list of the org's offices/sites.",
        code_system="internal",
        default_sort=SortMode.label,
    )

    # --- Open lists (users add values via the app) ---
    _ensure_type(
        session,
        key="skill",
        name="Skill",
        description="Open list — new skills are created on first use.",
        is_open=True,
        code_system="internal",
        default_sort=SortMode.label,
    )

    # Project roles were an open lookup type here from TEAMY-291 until TEAMY-487
    # promoted them to a first-class `project_role` entity in the host (Teamy) —
    # the seed is retired; hosts adopt any surviving lookup values into their own
    # catalog at boot (idempotent) and this library never re-creates the type.

    # --- Countries & nationalities (alphabetical, ISO codes) ---
    country = _ensure_type(
        session,
        key="country",
        name="Country",
        code_system="ISO3166-1A2",
        default_sort=SortMode.label,
    )
    nationality = _ensure_type(
        session,
        key="nationality",
        name="Nationality",
        code_system="ISO3166-1A2",
        default_sort=SortMode.label,
    )
    country_added = 0
    nationality_added = 0
    for c in COUNTRIES:
        country_added += _ensure_value(
            session,
            country.id,
            c["code"],
            [("en", c["country_en"]), ("ar", c["country_ar"])],
            aliases=c["aliases"],
            meta={"iso2": c["code"]},
        )
        nationality_added += _ensure_value(
            session,
            nationality.id,
            c["code"],
            [("en", c["nat_en"]), ("ar", c["nat_ar"])],
            aliases=c["aliases"],
            meta={"iso2": c["code"]},
        )
    _bump_version_if(session, country, country_added)
    _bump_version_if(session, nationality, nationality_added)
