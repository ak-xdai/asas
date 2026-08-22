"""Write-path org scoping (DR 0001 T3/T4/T7, PR-T2): an org-context mutation
must never touch the platform-global row — it lands on the caller's org
override, materialized copy-on-write when none exists. The two-org recipe
(T10): two orgs + the platform, asserting no write or read leaks either way.

Regression coverage for audit defects T-1 (org writes mutate global rows),
T-5 (seed suppressed by an org row), T-7 (deprecated override un-shadows the
platform row), T-8 (supersede follow leaks across orgs)."""

from sqlmodel import Session, select

import asas_lookups
from asas_lookups import service
from asas_lookups.models import LookupStatus, LookupTranslation, LookupValue
from asas_lookups.schemas import TranslationIn


def _global_row(s, type_, code):
    return s.exec(
        select(LookupValue).where(
            LookupValue.type_id == type_.id,
            LookupValue.code == code,
            LookupValue.org_id.is_(None),
        )
    ).first()


def _org_row(s, type_, code, org_id):
    return s.exec(
        select(LookupValue).where(
            LookupValue.type_id == type_.id,
            LookupValue.code == code,
            LookupValue.org_id == org_id,
        )
    ).first()


def _label(s, type_, code, lang="en"):
    return service.get_value_read(s, type_, code, lang, False).label


# ---------- T-1: update / alias ops land on the org copy, never the global row


def test_org_update_copies_on_write(seeded, org):
    """Org 7 relabels a platform value → a new org-7 override carries the edit;
    the global row (and org 9's and the platform's view) is untouched."""
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        service.update_value(
            s, type_, "male",
            translations=[TranslationIn(lang="en", label="Male (org 7)")],
            is_default=None, sort_order=None, meta={"restricted": True},
        )
        assert _label(s, type_, "male") == "Male (org 7)"
        g = _global_row(s, type_, "male")
        assert g is not None and (g.meta or {}) == {}
        assert {t.label for t in g.translations} != {"Male (org 7)"}
        o = _org_row(s, type_, "male", 7)
        assert o is not None and o.meta == {"restricted": True}

    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        assert _label(s, type_, "male") == "Male"
    org.org_id = None
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        assert _label(s, type_, "male") == "Male"


def test_org_alias_ops_copy_on_write(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        service.add_alias(s, type_, "male", "gentleman", None)
        g = _global_row(s, type_, "male")
        assert [a.alias for a in g.aliases] == []  # global haystack untouched
        assert "gentleman" in service.get_value_read(s, type_, "male", "en", False).aliases

    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        assert "gentleman" not in service.get_value_read(s, type_, "male", "en", False).aliases

    # removing an inherited alias also copies first: org 7 loses it, org 9 keeps it
    org.org_id = None
    with Session(seeded) as s:
        type_ = service.get_type(s, "country")
        service.add_alias(s, type_, "TR", "Turkey", None)  # platform alias
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "country")
        service.remove_alias(s, type_, "TR", "Turkey")
        assert "Turkey" not in service.get_value_read(s, type_, "TR", "en", False).aliases
    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "country")
        assert "Turkey" in service.get_value_read(s, type_, "TR", "en", False).aliases


def test_platform_session_still_edits_global(seeded, org):
    """No org context = platform scope: the mutation lands on the global row and
    every org sees it (the explicit affordance, DR 0001 T7)."""
    org.org_id = None
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        service.update_value(
            s, type_, "male",
            translations=[TranslationIn(lang="en", label="Male (platform v2)")],
            is_default=None, sort_order=None, meta=None,
        )
        g = _global_row(s, type_, "male")
        assert {t.label for t in g.translations} >= {"Male (platform v2)"}
    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        assert _label(s, type_, "male") == "Male (platform v2)"


# ---------- T-1 + T-7: deprecation is an org tombstone, and it actually hides


def test_org_deprecate_is_a_tombstone(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        service.deprecate_value(s, type_, "male", valid_to=None, superseded_by=None)
        g = _global_row(s, type_, "male")
        assert g.status == LookupStatus.active  # platform row untouched
        o = _org_row(s, type_, "male", 7)
        assert o is not None and o.status == LookupStatus.deprecated
        # T-7: the deprecated override SHADOWS the global row out of the active
        # list — it must not un-shadow and let the platform value reappear.
        total, items = service.list_values(s, type_, "en", True, None, None, 1, 100)
        assert "male" not in {v.code for v in items}

    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        total, items = service.list_values(s, type_, "en", True, None, None, 1, 100)
        assert "male" in {v.code for v in items}  # other tenants keep the value


def test_org_merge_copies_both_sides(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        service.merge_values(s, type_, "male", "other")
        # both platform rows untouched
        assert _global_row(s, type_, "male").status == LookupStatus.active
        assert [a.alias for a in _global_row(s, type_, "other").aliases] == []
        # org 7 sees the merge: male deprecated, its label an alias on other
        o = _org_row(s, type_, "male", 7)
        assert o is not None and o.status == LookupStatus.deprecated
        assert "Male" in service.get_value_read(s, type_, "other", "en", False).aliases
    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        total, items = service.list_values(s, type_, "en", True, None, None, 1, 100)
        assert "male" in {v.code for v in items}


# ---------- T-8: supersede follow never crosses into another org's rows


def test_supersede_follow_is_scoped(seeded, org):
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        foreign = LookupValue(type_id=type_.id, org_id=7, code="org7-secret")
        s.add(foreign)
        s.commit()
        s.refresh(foreign)
        s.add(LookupTranslation(value_id=foreign.id, lang="en", label="Org 7 secret"))
        g = _global_row(s, type_, "male")
        g.superseded_by_id = foreign.id
        s.add(g)
        s.commit()

    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        read = service.get_value_read(s, type_, "male", "en", True)
        assert read.label != "Org 7 secret"  # invisible pointer behaves as absent
    org.org_id = None
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        read = service.get_value_read(s, type_, "male", "en", True)
        assert read.label != "Org 7 secret"


# ---------- T-5: the seed only trusts platform rows


def test_seed_creates_global_despite_org_row(migrated, org):
    """An org-minted row must not suppress the platform seed of the same code:
    seeding predicates its existence check on org_id IS NULL."""
    with Session(migrated) as s:
        asas_lookups.seed(s)  # first seed: platform rows
        type_ = service.get_type(s, "gender")
        g = _global_row(s, type_, "male")
        # simulate the T-5 scenario: wipe the global row, leave an org row behind
        s.delete(g)
        s.add(LookupValue(type_id=type_.id, org_id=5, code="male"))
        s.commit()
        asas_lookups.seed(s)  # re-seed must heal the platform default
        assert _global_row(s, type_, "male") is not None
    org.org_id = 9
    with Session(migrated) as s:
        type_ = service.get_type(s, "gender")
        total, items = service.list_values(s, type_, "en", True, None, None, 1, 100)
        assert "male" in {v.code for v in items}
