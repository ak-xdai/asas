"""The mandatory access layer (DR 0021): clearance rank ordering, marking AND
semantics, fail-closed edges, NO admin floor, and the MAC-then-DAC ordering
inside can_view_record."""

from types import SimpleNamespace

from sqlmodel import Session

import asas_access as access
from asas_access import mac
from asas_access.models import RecordMarking, SubjectClearance, SubjectMarking

from conftest import make_user

ORG = 1


def _record(classification=None, *, id=7, org_id=ORG, **extra):
    return SimpleNamespace(id=id, org_id=org_id, classification_code=classification, **extra)


def _seed_levels(session: Session, org_id=ORG):
    access.ensure_clearance_levels(
        session,
        org_id,
        {
            "public": ("Public", 0),
            "internal": ("Internal", 10),
            "confidential": ("Confidential", 20),
            "restricted": ("Restricted", 30),
        },
    )
    session.commit()


def _subject_source(session: Session):
    """A host-style subject source reading the engine tables keyed by
    user_id == org_membership_id (test simplification of the host join)."""

    def source(user, s):
        from sqlmodel import select

        membership_id = user.user_id
        clearance = s.exec(
            select(SubjectClearance).where(
                SubjectClearance.org_membership_id == membership_id
            )
        ).first()
        marks = {
            row.marking_code
            for row in s.exec(
                select(SubjectMarking).where(
                    SubjectMarking.org_membership_id == membership_id
                )
            ).all()
        }
        return (clearance.level_code if clearance else None, marks)

    access.register_subject_source(source)


def _assign(session, user, *, level=None, markings=()):
    if level:
        session.add(
            SubjectClearance(org_membership_id=user.user_id, level_code=level)
        )
    for code in markings:
        session.add(
            SubjectMarking(org_membership_id=user.user_id, marking_code=code)
        )
    session.commit()


def _stamp_markings(session, record, *codes, entity_type="project"):
    for code in codes:
        session.add(
            RecordMarking(
                org_id=record.org_id,
                entity_type=entity_type,
                record_id=record.id,
                marking_code=code,
            )
        )
    session.commit()
    access.invalidate_record_markings(session)


def test_unregistered_entity_and_unclassified_record_always_pass(session):
    # Nothing registered: even a stamped-looking record passes for anonymous.
    assert access.mac_allows(session, None, "project", _record("restricted"))
    access.register_classified_entity("project")
    # Registered but unclassified: dormant, everyone passes.
    assert access.mac_allows(session, None, "project", _record(None))
    assert access.mac_allows(session, make_user("viewer"), "project", _record(None))
    assert access.mac_allows(session, None, "project", None)


def test_clearance_rank_ordering(session):
    access.register_classified_entity("project")
    _seed_levels(session)
    _subject_source(session)
    cleared = make_user("member", user_id=11)
    _assign(session, cleared, level="confidential")
    assert access.mac_allows(session, cleared, "project", _record("internal"))
    assert access.mac_allows(session, cleared, "project", _record("confidential"))
    assert not access.mac_allows(session, cleared, "project", _record("restricted"))
    # Anonymous never sees classified content.
    assert not access.mac_allows(session, None, "project", _record("internal"))


def test_unassigned_subject_holds_minimum_rank(session):
    access.register_classified_entity("project")
    _seed_levels(session)
    _subject_source(session)
    unassigned = make_user("member", user_id=12)
    assert access.mac_allows(session, unassigned, "project", _record("public"))
    assert not access.mac_allows(session, unassigned, "project", _record("internal"))


def test_admins_are_bound_no_floor(session):
    """Owner ruling 1: the admin tier does NOT pierce the MAC layer."""
    access.register_classified_entity("project")
    _seed_levels(session)
    _subject_source(session)
    admin = make_user("admin", user_id=13)
    assert not access.mac_allows(session, admin, "project", _record("restricted"))
    record = _record(None)
    _stamp_markings(session, record, "ma_2026")
    assert not access.mac_allows(session, admin, "project", record)
    # Read in (the audited break-glass path) — now allowed.
    _assign(session, admin, markings=["ma_2026"])
    assert access.mac_allows(session, admin, "project", record)


def test_markings_require_all(session):
    access.register_classified_entity("project")
    _subject_source(session)
    record = _record(None)
    _stamp_markings(session, record, "ma_2026", "board")
    partial = make_user("member", user_id=14)
    _assign(session, partial, markings=["ma_2026"])
    full = make_user("member", user_id=15)
    _assign(session, full, markings=["ma_2026", "board", "extra"])
    assert not access.mac_allows(session, partial, "project", record)
    assert access.mac_allows(session, full, "project", record)


def test_markings_and_clearance_compose(session):
    access.register_classified_entity("project")
    _seed_levels(session)
    _subject_source(session)
    record = _record("confidential")
    _stamp_markings(session, record, "ma_2026")
    marked_not_cleared = make_user("member", user_id=16)
    _assign(session, marked_not_cleared, level="internal", markings=["ma_2026"])
    cleared_not_marked = make_user("member", user_id=17)
    _assign(session, cleared_not_marked, level="restricted")
    both = make_user("member", user_id=18)
    _assign(session, both, level="confidential", markings=["ma_2026"])
    assert not access.mac_allows(session, marked_not_cleared, "project", record)
    assert not access.mac_allows(session, cleared_not_marked, "project", record)
    assert access.mac_allows(session, both, "project", record)


def test_fail_closed_edges(session):
    access.register_classified_entity("project")
    _seed_levels(session)
    _subject_source(session)
    user = make_user("member", user_id=19)
    _assign(session, user, level="restricted")
    # Stamp referencing a deleted level denies even the top-cleared subject.
    assert not access.mac_allows(session, user, "project", _record("nonexistent"))
    # A raising subject source yields the empty profile — the subject drops to
    # the minimum rank (public passes, anything above denies) and holds no
    # markings. Fail-closed, never fail-open.
    access.register_subject_source(lambda u, s: 1 / 0)
    assert access.mac_allows(session, user, "project", _record("public"))
    assert not access.mac_allows(session, user, "project", _record("internal"))
    # No source registered at all: same minimum-rank profile (host must wire).
    mac._subject_source = None
    assert not access.mac_allows(session, user, "project", _record("internal"))


def test_marked_only_record_without_org_fails_closed(session):
    """A record with markings but no resolvable org (no org_id attribute on the
    record — e.g. a child row — and an anonymous/system caller) must DENY, not
    silently skip the marking check (code-review finding: fail-open)."""
    access.register_classified_entity("document")
    _subject_source(session)
    doc = SimpleNamespace(id=41, classification_code=None)  # no org_id attr
    session.add(
        RecordMarking(
            org_id=ORG, entity_type="document", record_id=41, marking_code="vault"
        )
    )
    session.commit()
    assert not access.mac_allows(session, None, "document", doc)
    unmarked_user = make_user("admin", user_id=30)  # org resolves via the user
    assert not access.mac_allows(session, unmarked_user, "document", doc)
    holder = make_user("member", user_id=31)
    _assign(session, holder, markings=["vault"])
    assert access.mac_allows(session, holder, "document", doc)
    # Unmarked record without org stays allowed (dormant layer).
    plain = SimpleNamespace(id=42, classification_code=None)
    assert access.mac_allows(session, None, "document", plain)


def test_can_view_record_runs_mac_first(session):
    """MAC-then-DAC: a PUBLIC record that is classified stays invisible to the
    uncleared; the private-record admin floor never applies to the MAC part."""
    access.register_actions([access.VIEW_PRIVATE])
    access.register_classified_entity("project")
    _seed_levels(session)
    _subject_source(session)
    record = _record("restricted", visibility=access.PUBLIC)
    assert not access.can_view_record(session, make_user("admin", user_id=20), "project", record)
    cleared = make_user("member", user_id=21)
    _assign(session, cleared, level="restricted")
    assert access.can_view_record(session, cleared, "project", record)
    # Private + classified: both layers must pass — cleared non-viewer still blocked.
    private = _record("internal", id=8, visibility=access.PRIVATE)
    assert not access.can_view_record(session, cleared, "project", private)
    cleared_admin = make_user("admin", user_id=22)
    _assign(session, cleared_admin, level="internal")
    assert access.can_view_record(session, cleared_admin, "project", private)


def test_record_marking_cache_is_per_session(session):
    access.register_classified_entity("project")
    _subject_source(session)
    record = _record(None)
    user = make_user("member", user_id=23)
    assert access.mac_allows(session, user, "project", record)  # warms the cache
    _stamp_markings(session, record, "board")  # invalidates via helper
    assert not access.mac_allows(session, user, "project", record)


def test_ensure_clearance_levels_idempotent(session):
    """Presence-idempotent: re-running adds nothing and never updates existing
    rows (org edits win). NOTE: it does not remember deletions — hosts gate
    re-application with a stamp (the DR 0020 bootstrap pattern) so an org's
    deleted levels don't resurrect at boot."""
    _seed_levels(session)
    _seed_levels(session)  # second call adds nothing
    from sqlmodel import select

    from asas_access.models import ClearanceLevel

    rows = session.exec(select(ClearanceLevel).where(ClearanceLevel.org_id == ORG)).all()
    assert len(rows) == 4
    # Org renames a level; ensure() must not clobber the edit.
    renamed = next(r for r in rows if r.code == "internal")
    renamed.name = "Org-wide"
    session.add(renamed)
    session.commit()
    _seed_levels(session)
    rows = session.exec(select(ClearanceLevel).where(ClearanceLevel.org_id == ORG)).all()
    assert next(r for r in rows if r.code == "internal").name == "Org-wide"


# ── the org axis fails closed (issue #29: audit defects T-3 and T-9) ─────────


def test_marked_record_without_org_denies_the_foreign_caller(session):
    """Defect T-3: a record carrying no org_id used to have the CALLER's org
    substituted — an org-2 subject viewing an org-1 record looked up markings
    under org 2, found none, and a marked-only record read as unclassified:
    access granted. The record's stamps decide, whatever org the caller is
    from."""
    access.register_classified_entity("project")
    _seed_levels(session, org_id=ORG)
    _subject_source(session)
    orphan = _record(None, org_id=None)  # child row that never carried the org
    session.add(
        RecordMarking(
            org_id=ORG, entity_type="project", record_id=orphan.id, marking_code="hr"
        )
    )
    session.commit()
    access.invalidate_record_markings(session)
    outsider = make_user("member", user_id=99, org_id=2)  # holds no markings
    assert not access.mac_allows(session, outsider, "project", orphan)


def test_stamped_record_with_unresolvable_org_denies_not_caller_catalog(session):
    """Defect T-3, rank half: with the caller's org substituted, the record's
    classification was ranked against the CALLER's org catalog — per-org rank
    integers are not comparable across orgs. Unresolvable record org ⇒ deny."""
    access.register_classified_entity("project")
    _seed_levels(session, org_id=2)  # the caller's own catalog knows the code
    _subject_source(session)
    caller = make_user("member", user_id=42, org_id=2)
    _assign(session, caller, level="restricted")
    stamped = _record("restricted", org_id=None)
    assert not access.mac_allows(session, caller, "project", stamped)
    # the explicit parameter resolves it: under org 2's catalog the caller
    # holds the top rank, so the same record now passes
    assert access.mac_allows(
        session, caller, "project", stamped, record_org_id=2
    )


def test_phantom_levels_never_survive_a_rollback(session):
    """Defect T-9: ensure_clearance_levels invalidated the process-global
    level cache at mutation time — a reader between the mutation and a
    rollback cached rows that officially never existed, and served them to
    every session afterwards. Invalidation now waits for the commit."""
    _seed_levels(session, org_id=ORG)  # commits; listener fires; cache cleared
    warm = mac._levels(session)
    assert "phantom" not in warm.get(ORG, {})
    access.ensure_clearance_levels(session, ORG, {"phantom": ("Phantom", 99)})
    mid = mac._levels(session)  # a reader inside the open transaction
    assert "phantom" not in mid.get(ORG, {})  # the old cache still serves
    session.rollback()
    after = mac._levels(session)
    assert "phantom" not in after.get(ORG, {})  # nothing phantom was cached
    access.ensure_clearance_levels(session, ORG, {"phantom": ("Phantom", 99)})
    session.commit()  # NOW the listener invalidates
    assert mac._levels(session)[ORG]["phantom"] == 99
