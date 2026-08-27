"""Platform lookup values are read-only for organizations (issue #24).

The write path resolves through ``_value_for_write``: with org context a
mutation may touch only rows the caller's org owns — a code that resolves to a
platform row is rejected with 403, never silently redirected to (or mutating)
the row every tenant shares (audit defect T-1 in DR 0001 / #15). Platform
scope (no resolver) keeps editing global rows; orgs keep full control of the
values they created. The seed's idempotency check predicates on
``org_id IS NULL`` so an org-minted row can't suppress a platform default
(defect T-5).
"""

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

import asas_lookups
from asas_lookups import service
from asas_lookups.models import LookupStatus, LookupValue
from asas_lookups.schemas import TranslationIn


def _global_row(s, type_, code):
    return s.exec(
        select(LookupValue).where(
            LookupValue.type_id == type_.id,
            LookupValue.code == code,
            LookupValue.org_id.is_(None),
        )
    ).first()


def _org_rows(s, type_, code):
    return s.exec(
        select(LookupValue).where(
            LookupValue.type_id == type_.id,
            LookupValue.code == code,
            LookupValue.org_id.is_not(None),
        )
    ).all()


def _expect_403(fn, *args, **kwargs):
    with pytest.raises(HTTPException) as exc:
        fn(*args, **kwargs)
    assert exc.value.status_code == 403
    assert "read-only" in exc.value.detail


def test_org_update_of_platform_value_is_rejected(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        _expect_403(
            service.update_value,
            s, type_, "male",
            translations=[TranslationIn(lang="en", label="Renamed")],
            is_default=None, sort_order=None, meta=None,
        )
        g = _global_row(s, type_, "male")
        assert {t.label for t in g.translations} == {"Male", "ذكر"}
        assert _org_rows(s, type_, "male") == []  # nothing materialized either


def test_org_deprecate_of_platform_value_is_rejected(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        _expect_403(
            service.deprecate_value,
            s, type_, "female", valid_to=None, superseded_by=None,
        )
        assert _global_row(s, type_, "female").status == LookupStatus.active


def test_org_alias_ops_on_platform_value_are_rejected(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        _expect_403(service.add_alias, s, type_, "male", "guy", None)
        _expect_403(service.remove_alias, s, type_, "male", "guy")
        assert _global_row(s, type_, "male").aliases == []


def test_org_merge_involving_platform_values_is_rejected(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        # both sides platform
        _expect_403(service.merge_values, s, type_, "other", "male")
        # org-owned source into a platform target is still a platform mutation
        service.create_value(
            s, type_,
            code="org-only",
            translations=[TranslationIn(lang="en", label="Org only")],
            is_default=False, sort_order=0, parent_code=None, meta={}, aliases=[],
        )
        _expect_403(service.merge_values, s, type_, "org-only", "male")
        assert _global_row(s, type_, "male").aliases == []
        assert _global_row(s, type_, "other").status == LookupStatus.active


def test_org_keeps_full_control_of_its_own_values(seeded, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        service.create_value(
            s, type_,
            code="unspecified",
            translations=[TranslationIn(lang="en", label="Unspecified")],
            is_default=False, sort_order=9, parent_code=None, meta={}, aliases=[],
        )
        service.update_value(
            s, type_, "unspecified",
            translations=[TranslationIn(lang="en", label="Prefer not to say")],
            is_default=None, sort_order=None, meta=None,
        )
        service.add_alias(s, type_, "unspecified", "n/a", None)
        value = service.deprecate_value(
            s, type_, "unspecified", valid_to=None, superseded_by=None
        )
        assert value.org_id == 7
        assert value.status == LookupStatus.deprecated

    # another org can't even see it, let alone edit it
    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        with pytest.raises(HTTPException) as exc:
            service.update_value(
                s, type_, "unspecified",
                translations=[TranslationIn(lang="en", label="Hijack")],
                is_default=None, sort_order=None, meta=None,
            )
        assert exc.value.status_code == 404


def test_platform_scope_still_edits_global_rows(seeded, org):
    # org fixture installed but holder unset: resolver answers None = platform
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        value = service.update_value(
            s, type_, "male",
            translations=[TranslationIn(lang="en", label="Male")],
            is_default=None, sort_order=1, meta=None,
        )
        assert value.org_id is None
        service.add_alias(s, type_, "male", "m", None)
        assert [a.alias for a in _global_row(s, type_, "male").aliases] == ["m"]


def test_seed_restores_platform_row_despite_org_row(seeded, org):
    """Defect T-5: an org-minted row sharing a seed code must not suppress the
    platform default — re-seeding heals the global row."""
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        g = _global_row(s, type_, "female")
        s.delete(g)  # simulate the platform row lost / pre-seed state
        s.commit()

    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        service.create_value(
            s, type_,
            code="female",
            translations=[TranslationIn(lang="en", label="Org female")],
            is_default=False, sort_order=0, parent_code=None, meta={}, aliases=[],
        )

    org.org_id = None
    with Session(seeded) as s:
        asas_lookups.seed(s)  # must recreate the platform row, not be suppressed
        type_ = service.get_type(s, "gender")
        g = _global_row(s, type_, "female")
        assert g is not None
        assert {t.label for t in g.translations} == {"Female", "أنثى"}
        orgs = _org_rows(s, type_, "female")
        assert len(orgs) == 1  # the org's own row untouched
        assert {t.label for t in orgs[0].translations} == {"Org female"}


# ── read-side scoping and legacy shadows (issue #33: audit defect T-8) ───────


def test_supersede_pointer_never_leaks_foreign_org_labels(seeded, org):
    """Defect T-8: the supersede follow was a bare session.get — a pointer
    landing in another org's row served that org's labels to a stranger. A
    pointer outside the caller's visible set behaves as absent."""
    from asas_lookups.models import LookupTranslation

    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        foreign = LookupValue(type_id=type_.id, code="org9-secret", org_id=9)
        s.add(foreign)
        s.flush()
        s.add(LookupTranslation(value_id=foreign.id, lang="en", label="Org9 Secret"))
        mine = LookupValue(
            type_id=type_.id, code="legacy", org_id=7, superseded_by_id=foreign.id
        )
        s.add(mine)
        s.flush()
        s.add(LookupTranslation(value_id=mine.id, lang="en", label="My Legacy"))
        s.commit()

    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        read = service.get_value_read(s, type_, "legacy", "en", True)
        assert read.label == "My Legacy"  # not "Org9 Secret"

    # the pointer still follows normally inside the caller's own visible set
    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        s.add(
            LookupValue(
                type_id=type_.id, code="old9", org_id=9,
                superseded_by_id=s.exec(
                    select(LookupValue).where(LookupValue.code == "org9-secret")
                ).first().id,
            )
        )
        s.commit()
        read = service.get_value_read(s, type_, "old9", "en", True)
        assert read.label == "Org9 Secret"


def test_find_org_shadows_lists_only_legacy_collisions(seeded, org):
    with Session(seeded) as s:
        assert service.find_org_shadows(s) == []
        type_ = service.get_type(s, "gender")
        # a legacy shadow (predates #26's guards) and a harmless org-only value
        s.add(LookupValue(type_id=type_.id, code="male", org_id=7))
        s.add(LookupValue(type_id=type_.id, code="org-only", org_id=7))
        s.commit()
        assert service.find_org_shadows(s) == [("gender", "male", 7)]
