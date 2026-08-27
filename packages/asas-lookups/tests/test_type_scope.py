"""Explicit per-type scope (issue #35): platform reference data is immutable to
orgs in full — no edits and no additions — while org-scoped vocabularies live
wholly at org level, seeded per org from a platform-held template that org
reads never see."""

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

import asas_lookups
from asas_lookups import ensure_type, ensure_value, seed_org_lookups, service
from asas_lookups.models import LookupType, LookupValue, TypeScope
from asas_lookups.schemas import TranslationIn


@pytest.fixture()
def vocab(seeded):
    """A host-registered org-scoped vocabulary with a two-value template
    (one hierarchical child)."""
    with Session(seeded) as s:
        t = ensure_type(
            s, key="skill", name="Skill", is_open=True, scope=TypeScope.org
        )
        ensure_value(s, t.id, "engineering", [("en", "Engineering")])
        ensure_value(s, t.id, "python", [("en", "Python")], aliases=["py"])
        # hierarchy: python under engineering (template-side parent pointer)
        parent = s.exec(
            select(LookupValue).where(
                LookupValue.type_id == t.id, LookupValue.code == "engineering"
            )
        ).first()
        child = s.exec(
            select(LookupValue).where(
                LookupValue.type_id == t.id, LookupValue.code == "python"
            )
        ).first()
        child.parent_id = parent.id
        s.add(child)
        s.commit()
    return "skill"


def test_existing_types_backfill_to_platform_scope(seeded):
    with Session(seeded) as s:
        for t in s.exec(select(LookupType)).all():
            assert t.scope is TypeScope.platform


def test_org_cannot_add_values_to_a_platform_type(seeded, org):
    """The other half of read-only (issue #35): previously an org could still
    MINT its own row on any type as long as the code was free."""
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, "gender")
        with pytest.raises(HTTPException) as exc:
            service.create_value(
                s, type_,
                code="org-extra",
                translations=[TranslationIn(lang="en", label="Org extra")],
                is_default=False, sort_order=0, parent_code=None, meta={}, aliases=[],
            )
        assert exc.value.status_code == 403
        assert "platform type" in exc.value.detail


def test_template_rows_are_never_served_to_orgs(seeded, vocab, org):
    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, vocab)
        total, items = service.list_values(s, type_, "en", True, None, None, 1, 50)
        assert total == 0 and items == []  # unseeded org: nothing, not the template
        with pytest.raises(HTTPException) as exc:
            service.get_value_read(s, type_, "python", "en", False)
        assert exc.value.status_code == 404  # template existence never leaks

    org.org_id = None  # platform view = template management
    with Session(seeded) as s:
        type_ = service.get_type(s, vocab)
        total, _ = service.list_values(s, type_, "en", True, None, None, 1, 50)
        assert total == 2


def test_seed_org_lookups_copies_the_template_per_org(seeded, vocab, org):
    with Session(seeded) as s:
        assert seed_org_lookups(s, 7) == 2
        assert seed_org_lookups(s, 7) == 0  # presence-idempotent

    org.org_id = 7
    with Session(seeded) as s:
        type_ = service.get_type(s, vocab)
        total, items = service.list_values(s, type_, "en", True, None, None, 1, 50)
        assert total == 2
        by_code = {v.code: v for v in items}
        assert by_code["python"].parent_code == "engineering"  # remapped parent
        assert by_code["python"].aliases == ["py"]
        # the copies are the org's own rows, and the parent pointer targets
        # the org's copy, not the template row
        python = s.exec(
            select(LookupValue).where(
                LookupValue.type_id == type_.id,
                LookupValue.code == "python",
                LookupValue.org_id == 7,
            )
        ).first()
        parent = s.get(LookupValue, python.parent_id)
        assert parent.org_id == 7

        # and the org owns them outright: edit + deprecate work
        service.update_value(
            s, type_, "python",
            translations=[TranslationIn(lang="en", label="Python 3")],
            is_default=None, sort_order=None, meta=None,
        )
        assert service.get_value_read(s, type_, "python", "en", False).label == "Python 3"

    # another org is untouched until seeded itself
    org.org_id = 9
    with Session(seeded) as s:
        type_ = service.get_type(s, vocab)
        total, _ = service.list_values(s, type_, "en", True, None, None, 1, 50)
        assert total == 0
        assert seed_org_lookups(s, 9) == 2  # gets the pristine template
        assert service.get_value_read(s, type_, "python", "en", False).label == "Python"


def test_open_lists_require_org_scope(seeded, client):
    with Session(seeded) as s:
        with pytest.raises(ValueError, match="scope"):
            ensure_type(s, key="bad", name="Bad", is_open=True)
    resp = client.post(
        "/admin/lookup-types",
        json={"key": "bad", "name": "Bad", "is_open": True},
    )
    assert resp.status_code == 422
    ok = client.post(
        "/admin/lookup-types",
        json={"key": "good", "name": "Good", "is_open": True, "scope": "org"},
    )
    assert ok.status_code == 201
    assert ok.json()["scope"] == "org"
