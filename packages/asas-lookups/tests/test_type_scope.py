"""Explicit per-type scope (issue #35): platform reference data is immutable to
orgs in full — no edits and no additions — while org-scoped vocabularies live
wholly at org level, seeded per org from a platform-held template that org
reads never see."""

import pytest
import sqlalchemy as sa
from alembic import command
from fastapi import HTTPException
from sqlmodel import Session, select

import asas_lookups
from asas_lookups import ensure_type, ensure_value, seed_org_lookups, service
from asas_lookups.migrate import _config
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


def _plant_pre_scope_types(engine):
    """Bring the schema to the baseline (pre-scope) and plant a closed and a
    legacy *open* type the way a pre-0002 database held them."""
    command.upgrade(_config(engine), "0001")
    lt = sa.table(
        "lookup_type",
        sa.column("key"), sa.column("name"), sa.column("is_open"),
        sa.column("is_hierarchical"), sa.column("default_sort"),
        sa.column("version"), sa.column("created_at"), sa.column("updated_at"),
    )
    from datetime import datetime

    now = datetime.utcnow()
    with engine.begin() as conn:
        for key, is_open in (("closed_legacy", False), ("open_legacy", True)):
            conn.execute(
                lt.insert().values(
                    key=key, name=key, is_open=is_open, is_hierarchical=False,
                    default_sort="label", version=1, created_at=now, updated_at=now,
                )
            )


def test_migration_backfills_legacy_open_types_as_org(engine):
    """A pre-0002 open list means org users add values — only an org-owned
    type can host that. Backfilling it as 'platform' would turn every
    get-or-create on it into a 403."""
    _plant_pre_scope_types(engine)
    asas_lookups.migrate(engine)
    with Session(engine) as s:
        by_key = {t.key: t for t in s.exec(select(LookupType)).all()}
        assert by_key["closed_legacy"].scope is TypeScope.platform
        assert by_key["open_legacy"].scope is TypeScope.org


def test_migration_resumes_over_a_nullable_scope_column(engine):
    """Column presence is not completed migration state: a scope column that
    already exists but still holds NULLs (a partially applied run, or a host
    chain that added it nullable) must still be backfilled and tightened."""
    _plant_pre_scope_types(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE lookup_type ADD COLUMN scope VARCHAR"))
    asas_lookups.migrate(engine)
    with Session(engine) as s:
        by_key = {t.key: t for t in s.exec(select(LookupType)).all()}
        assert by_key["closed_legacy"].scope is TypeScope.platform
        assert by_key["open_legacy"].scope is TypeScope.org
    scope_col = next(
        c for c in sa.inspect(engine).get_columns("lookup_type")
        if c["name"] == "scope"
    )
    assert scope_col["nullable"] is False


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


def test_seed_remaps_parent_to_an_existing_org_row(seeded, vocab, org):
    """Idempotency skips a code the org already owns — but a newly copied
    child whose template parent is that skipped code must still point at the
    org's existing row, not end up rootless (and unrepairable on re-run)."""
    org.org_id = 7
    with Session(seeded) as s:  # the org minted its own "engineering" first
        type_ = service.get_type(s, vocab)
        service.create_value(
            s, type_,
            code="engineering",
            translations=[TranslationIn(lang="en", label="Engineering (ours)")],
            is_default=False, sort_order=0, parent_code=None, meta={}, aliases=[],
        )

    with Session(seeded) as s:
        assert seed_org_lookups(s, 7) == 1  # only "python" is copied
        type_ = service.get_type(s, vocab)
        rows = {
            v.code: v
            for v in s.exec(
                select(LookupValue).where(
                    LookupValue.type_id == type_.id, LookupValue.org_id == 7
                )
            ).all()
        }
        assert rows["python"].parent_id == rows["engineering"].id


def test_seed_remaps_supersede_pointer_to_org_copy(seeded, vocab, org):
    """A deprecated template value keeps its replacement link after the copy:
    ``superseded_by_id`` is remapped into the org-owned set the same way
    ``parent_id`` is, so supersession still resolves for the org."""
    from asas_lookups.models import LookupStatus

    with Session(seeded) as s:  # template: "cobol" deprecated, points at "python"
        type_ = service.get_type(s, vocab)
        ensure_value(s, type_.id, "cobol", [("en", "COBOL")])
        tmpl = {
            v.code: v
            for v in s.exec(
                select(LookupValue).where(
                    LookupValue.type_id == type_.id, LookupValue.org_id.is_(None)
                )
            ).all()
        }
        tmpl["cobol"].status = LookupStatus.deprecated
        tmpl["cobol"].superseded_by_id = tmpl["python"].id
        s.add(tmpl["cobol"])
        s.commit()

    with Session(seeded) as s:
        assert seed_org_lookups(s, 7) == 3
        type_ = service.get_type(s, vocab)
        rows = {
            v.code: v
            for v in s.exec(
                select(LookupValue).where(
                    LookupValue.type_id == type_.id, LookupValue.org_id == 7
                )
            ).all()
        }
        assert rows["cobol"].status == LookupStatus.deprecated
        assert rows["cobol"].superseded_by_id == rows["python"].id


def test_seed_org_lookups_bumps_type_version(seeded, vocab):
    """The read-API ETag keys on the type version: a seed that creates rows
    must bump it, or an org that cached a pre-seed (empty) response keeps
    revalidating to 304 against stale content. An idempotent re-run that
    creates nothing must NOT bump."""
    with Session(seeded) as s:
        before = service.get_type(s, vocab).version
        assert seed_org_lookups(s, 7) == 2
        assert service.get_type(s, vocab).version == before + 1
        assert seed_org_lookups(s, 7) == 0
        assert service.get_type(s, vocab).version == before + 1


def test_open_lists_require_org_scope(seeded, client):
    with Session(seeded) as s:
        with pytest.raises(ValueError, match="scope"):
            ensure_type(s, key="bad", name="Bad", is_open=True)
    resp = client.post(
        "/admin/lookup-types",
        json={"key": "bad", "name": "Bad", "is_open": True},
    )
    assert resp.status_code == 422
    # unknown scope is rejected by schema validation (the field is TypeScope)
    resp = client.post(
        "/admin/lookup-types",
        json={"key": "bad", "name": "Bad", "scope": "galaxy"},
    )
    assert resp.status_code == 422
    ok = client.post(
        "/admin/lookup-types",
        json={"key": "good", "name": "Good", "is_open": True, "scope": "org"},
    )
    assert ok.status_code == 201
    assert ok.json()["scope"] == "org"
