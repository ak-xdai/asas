"""Field-permission engine: baseline vs allowlist, the admin floor, org-scoped
override rows, no-op changes, redaction with companions. Engine behaviors ported
from Teamy's integration suite (WXL-112) with the extraction (TEAMY-477)."""

from types import SimpleNamespace

import asas_access as access
from asas_access.models import FieldPermission

from conftest import make_user


def _grant(session, entity, field, action, principal, org_id=None):
    session.add(
        FieldPermission(
            entity_type=entity, field=field, action=action,
            principal=principal, org_id=org_id,
        )
    )
    session.commit()
    access.invalidate_cache()


def test_unconfigured_field_defers_to_baseline(session):
    record = SimpleNamespace(id=1)
    assert access.can_edit_field(session, make_user("viewer"), "member", "name", record)


def test_rows_form_allowlist_and_admin_floor(session):
    record = SimpleNamespace(id=1)
    _grant(session, "member", "salary", "edit", "people_ops")
    assert not access.can_edit_field(session, make_user("member"), "member", "salary", record)
    assert access.can_edit_field(session, make_user("admin"), "member", "salary", record)
    # A group key held via a global source satisfies the allowlist.
    access.register_global_source(lambda user, s: {"people_ops"})
    assert access.can_edit_field(session, make_user("member"), "member", "salary", record)


def test_relationship_resolver_grants(session):
    _grant(session, "member", "phone", "view", access.SELF)
    access.register_resolver(
        "member", access.SELF,
        lambda user, rec, s: user.member_id == rec.id,
    )
    record = SimpleNamespace(id=7)
    assert access.can_view_field(session, make_user(member_id=7), "member", "phone", record)
    assert not access.can_view_field(session, make_user(member_id=8), "member", "phone", record)


def test_resolver_errors_never_grant(session):
    _grant(session, "member", "phone", "view", access.SELF)

    def _boom(user, rec, s):
        raise RuntimeError("resolver exploded")

    access.register_resolver("member", access.SELF, _boom)
    assert not access.can_view_field(
        session, make_user(member_id=7), "member", "phone", SimpleNamespace(id=7)
    )


def test_org_scoped_rows_do_not_leak(session):
    """An org's own grant applies only there; a field whose only rows belong to
    OTHER orgs stays baseline for this caller."""
    record = SimpleNamespace(id=1)
    _grant(session, "member", "notes", "edit", "member", org_id=2)
    # Caller in org 1: only org-2 rows exist -> baseline (allowed).
    assert access.can_edit_field(session, make_user("viewer", org_id=1), "member", "notes", record)
    # Caller in org 2: the allowlist applies — member yes, viewer no.
    assert access.can_edit_field(session, make_user("member", org_id=2), "member", "notes", record)
    assert not access.can_edit_field(session, make_user("viewer", org_id=2), "member", "notes", record)


def test_forbidden_edits_skips_noop_changes(session):
    _grant(session, "member", "salary", "edit", "people_ops")
    record = SimpleNamespace(id=1, salary=100, name="A")
    user = make_user("member")
    changes = {"salary": 100, "name": "B"}  # salary unchanged -> allowed no-op
    assert access.forbidden_edits(session, user, "member", record, changes) == []
    changes = {"salary": 200}
    assert access.forbidden_edits(session, user, "member", record, changes) == ["salary"]


def test_redact_view_nulls_restricted_and_companions(session):
    _grant(session, "member", "salary_code", "view", "people_ops")
    record = SimpleNamespace(id=1)
    read_model = SimpleNamespace(salary_code="X1", salary_label="Grade X1", name="A")
    access.redact_view(
        session, make_user("member"), "member", read_model, record,
        also_null={"salary_code": ["salary_label"]},
    )
    assert read_model.salary_code is None
    assert read_model.salary_label is None
    assert read_model.name == "A"
    # An admin viewer keeps everything.
    intact = SimpleNamespace(salary_code="X1", salary_label="Grade X1", name="A")
    access.redact_view(
        session, make_user("admin"), "member", intact, record,
        also_null={"salary_code": ["salary_label"]},
    )
    assert intact.salary_code == "X1"


def test_redact_view_redacts_a_mapping(session):
    """A dict projection must be redacted, not silently passed through.

    Before mappings were supported, `hasattr` matched nothing on a dict, the
    model came back unchanged, and the restricted field reached the caller with
    no error — a redaction function failing open, which is the worst direction
    for one to fail in.
    """
    _grant(session, "member", "salary_code", "view", "people_ops")
    record = SimpleNamespace(id=1)
    read_model = {"salary_code": "X1", "salary_label": "Grade X1", "name": "A"}

    access.redact_view(
        session, make_user("member"), "member", read_model, record,
        also_null={"salary_code": ["salary_label"]},
    )

    assert read_model["salary_code"] is None
    assert read_model["salary_label"] is None
    assert read_model["name"] == "A"


def test_redact_view_leaves_a_permitted_mapping_intact(session):
    _grant(session, "member", "salary_code", "view", "people_ops")
    record = SimpleNamespace(id=1)
    read_model = {"salary_code": "X1", "name": "A"}

    access.redact_view(session, make_user("admin"), "member", read_model, record)

    assert read_model["salary_code"] == "X1"


def test_redact_view_refuses_a_shape_it_cannot_redact(session):
    """Loud beats silent when there is something to protect.

    A list has neither attributes nor keys, so redaction cannot be applied.
    Returning it unchanged would disclose the restricted field.
    """
    import pytest

    _grant(session, "member", "salary_code", "view", "people_ops")
    record = SimpleNamespace(id=1)

    with pytest.raises(TypeError) as exc:
        access.redact_view(session, make_user("member"), "member", ["X1"], record)

    assert "salary_code" in str(exc.value)


def test_redact_view_is_a_noop_when_nothing_is_restricted(session):
    """No view rows for the entity means no work — and no type demands either.

    This is the common case (most entities restrict nothing), so it must not
    start raising on shapes it never had to touch.
    """
    assert access.redact_view(session, make_user("member"), "member", ["x"], None) == ["x"]


def test_seed_is_idempotent_and_validates_fields(session):
    import pytest

    access.register_fields("member", ["salary"])
    defaults = [("member", "salary", "edit", "people_ops")]
    access.seed_field_permissions(session, defaults)
    access.seed_field_permissions(session, defaults)  # second run adds nothing
    from sqlmodel import select

    rows = session.exec(select(FieldPermission)).all()
    assert len(rows) == 1
    with pytest.raises(ValueError, match="unknown field"):
        access.seed_field_permissions(session, [("member", "typo", "edit", "admin")])


def test_org_scoped_row_does_not_block_platform_default_seed(session):
    from sqlmodel import select

    access.register_fields("member", ["salary"])
    # An org customizes the grant before the platform default exists.
    session.add(
        FieldPermission(
            entity_type="member",
            field="salary",
            action="edit",
            principal="people_ops",
            org_id=2,
        )
    )
    session.commit()
    access.invalidate_cache()
    # A later-added platform default for the same key must still seed — the
    # org-scoped row must not satisfy the existence check.
    access.seed_field_permissions(session, [("member", "salary", "edit", "people_ops")])
    rows = session.exec(select(FieldPermission)).all()
    assert {(r.principal, r.org_id) for r in rows} == {("people_ops", 2), ("people_ops", None)}
    # And re-seeding stays idempotent against the platform row itself.
    access.seed_field_permissions(session, [("member", "salary", "edit", "people_ops")])
    assert len(session.exec(select(FieldPermission)).all()) == 2
