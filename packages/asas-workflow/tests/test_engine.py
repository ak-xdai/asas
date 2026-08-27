"""Engine semantics on a bare session: the happy walk (open → approve →
complete, with completion callback + events), quorum all, negative veto,
request-info round-trip, floor fail-closed, cancel. Condensed from Teamy's
tests/test_workflow.py (WXL-213) with the extraction (TEAMY-478) — the full
integration suite stays in Teamy."""

import uuid

import pytest

import asas_workflow as workflow
from asas_workflow import DefinitionSpec, NodeSpec, NodeType, TransitionSpec, Verdict
from asas_workflow.engine import EngineError
from asas_workflow.models import InstanceStatus


def _key(prefix: str = "proc") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def _principal(*member_ids: int) -> str:
    """Register a throwaway assignee resolver returning a fixed member set."""
    name = f"t_{uuid.uuid4().hex[:8]}"
    members = set(member_ids)
    workflow.register_assignee_resolver(name, lambda session, et, eid: members)
    return name


def _simple_spec(key, principal, *, quorum="any", node_cfg=None, outcome="approved"):
    """start -> approval -> end."""
    cfg = {"principals": [principal], "quorum": quorum, **(node_cfg or {})}
    return DefinitionSpec(
        key=key,
        name="Test process",
        entity_type="project",
        nodes=(
            NodeSpec("start", "Start", NodeType.start),
            NodeSpec("review", "Review", NodeType.approval, cfg),
            NodeSpec("done", "Done", NodeType.end, {"outcome": outcome}),
        ),
        transitions=(TransitionSpec("start", "review"), TransitionSpec("review", "done")),
    )


def _seed(session, spec):
    workflow.register_definition(spec)
    workflow.seed_definitions(session)


def _open(session, key, *, initiated_by=1, entity_id=10, data=None):
    return workflow.open_instance(
        session,
        process_key=key,
        entity_type="project",
        entity_id=entity_id,
        subject_snapshot={"title": "thing"},
        data=data,
        initiated_by=initiated_by,
    )


def test_open_approve_complete_fires_callback_and_events(session, floor):
    key = _key()
    _seed(session, _simple_spec(key, _principal(2)))
    outcomes = []
    workflow.register_completion_callback(
        key, lambda s, instance, outcome: outcomes.append(outcome)
    )
    log = []
    workflow.subscribe(lambda kind, payload: log.append(kind))

    instance = _open(session, key)
    assert instance.status == InstanceStatus.running
    assert instance.current_node_key == "review"

    instance = workflow.decide(
        session, instance, actor_id=2, verdict=Verdict.positive
    )
    assert instance.status == InstanceStatus.completed
    assert instance.outcome == "approved"
    assert outcomes == ["approved"]
    assert "process.opened" in log and "process.completed" in log
    assert "approval.activated" in log


def test_quorum_all_waits_for_every_assignee(session, floor):
    key = _key()
    _seed(session, _simple_spec(key, _principal(2, 3), quorum="all"))
    instance = _open(session, key)
    instance = workflow.decide(session, instance, actor_id=2, verdict=Verdict.positive)
    assert instance.status == InstanceStatus.running  # 3 still pending
    instance = workflow.decide(session, instance, actor_id=3, verdict=Verdict.positive)
    assert instance.status == InstanceStatus.completed


def test_negative_verdict_vetoes(session, floor):
    key = _key()
    _seed(session, _simple_spec(key, _principal(2, 3), quorum="all"))
    instance = _open(session, key)
    instance = workflow.decide(
        session, instance, actor_id=2, verdict=Verdict.negative, comment="veto"
    )
    assert instance.status == InstanceStatus.completed
    assert instance.outcome == "rejected"


def test_non_assignee_cannot_decide(session, floor):
    key = _key()
    _seed(session, _simple_spec(key, _principal(2)))
    instance = _open(session, key)
    with pytest.raises(EngineError):
        workflow.decide(session, instance, actor_id=7, verdict=Verdict.positive)


def test_empty_resolution_fails_closed_to_floor(session, floor):
    """No resolved assignees -> the registered floor is asked, never silent
    auto-approve."""
    key = _key()
    _seed(session, _simple_spec(key, _principal()))  # resolver returns empty set
    instance = _open(session, key)
    execution = instance.executions[-1]
    assignees = {a.member_id for a in execution.assignees}
    assert assignees == {floor}
    instance = workflow.decide(session, instance, actor_id=floor, verdict=Verdict.positive)
    assert instance.status == InstanceStatus.completed


def test_request_info_roundtrip(session, floor):
    key = _key()
    _seed(session, _simple_spec(key, _principal(2), node_cfg={"allow_request_info": True}))
    instance = _open(session, key)
    req = workflow.request_info(
        session, instance, actor_id=2, asked_of=5, question="Why?"
    )
    assert req.status.value == "pending"
    instance = workflow.answer_info(session, req, actor_id=5, answer="Because.")
    # The approval is still pending; the answer lands in process data.
    instance = workflow.decide(session, instance, actor_id=2, verdict=Verdict.positive)
    assert instance.status == InstanceStatus.completed


def test_cancel_stops_a_running_instance(session, floor):
    key = _key()
    _seed(session, _simple_spec(key, _principal(2)))
    instance = _open(session, key)
    instance = workflow.cancel(session, instance, actor_id=1)
    assert instance.status == InstanceStatus.canceled
    with pytest.raises(EngineError):
        workflow.decide(session, instance, actor_id=2, verdict=Verdict.positive)


def test_ensure_definition_versions_on_change(session):
    key = _key()
    spec = _simple_spec(key, _principal(2))
    first = workflow.ensure_definition(session, spec)
    again = workflow.ensure_definition(session, spec)
    assert again.id == first.id  # unchanged spec -> same version
    changed = _simple_spec(key, _principal(2), outcome="green-lit")
    second = workflow.ensure_definition(session, changed)
    assert second.version == first.version + 1


def test_validate_definition_rejects_unknown_resolver(session):
    key = _key()
    spec = _simple_spec(key, "never_registered_principal")
    with pytest.raises(Exception):
        workflow.ensure_definition(session, spec)


# ── the org axis reaches the floor (issue #31: audit defect T-4) ─────────────


def test_instance_org_reaches_the_floor(session):
    """Defect T-4: open_instance had no way to set ProcessInstance.org_id, so
    both resolve_floor call sites always received None — the org-scoped
    approver floor could never actually scope, and one org's approvals could
    land in another org's inboxes. The org now arrives explicitly (or via the
    resolver) and flows to the floor."""
    seen_orgs = []

    def per_org_floor(s, org_id=None):
        seen_orgs.append(org_id)
        return {900 + (org_id or 0)}

    workflow.register_floor_resolver(per_org_floor)
    key = _key()
    _seed(session, _simple_spec(key, _principal()))  # empty resolution -> floor
    instance = workflow.open_instance(
        session,
        process_key=key,
        entity_type="project",
        entity_id=10,
        subject_snapshot={"title": "thing"},
        initiated_by=1,
        org_id=7,
    )
    assert instance.org_id == 7
    assert seen_orgs == [7]  # the floor was asked FOR org 7, not unscoped
    execution = instance.executions[-1]
    assert {a.member_id for a in execution.assignees} == {907}


def test_org_resolver_supplies_the_org_when_not_passed(session):
    from asas_workflow import engine

    workflow.register_floor_resolver(lambda s, org_id=None: {999})
    workflow.configure_org_resolver(lambda s: 5)
    try:
        key = _key()
        _seed(session, _simple_spec(key, _principal(3)))
        instance = _open(session, key)
        assert instance.org_id == 5
        # explicit parameter beats the resolver
        key2 = _key()
        _seed(session, _simple_spec(key2, _principal(3)))
        explicit = workflow.open_instance(
            session,
            process_key=key2,
            entity_type="project",
            entity_id=11,
            subject_snapshot={"title": "thing"},
            initiated_by=1,
            org_id=8,
        )
        assert explicit.org_id == 8
    finally:
        engine._org_resolver = None


def test_explicit_none_org_stays_platform_scoped(session):
    """An explicit org_id=None is a deliberate platform-scoped instance — the
    resolver must not replace it (only an OMITTED org_id consults it)."""
    workflow.register_floor_resolver(lambda s, org_id=None: {999})
    workflow.configure_org_resolver(lambda s: 5)
    key = _key()
    _seed(session, _simple_spec(key, _principal(3)))
    instance = workflow.open_instance(
        session,
        process_key=key,
        entity_type="project",
        entity_id=12,
        subject_snapshot={"title": "thing"},
        initiated_by=1,
        org_id=None,
    )
    assert instance.org_id is None
