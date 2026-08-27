"""The executor — single active node per instance (owner ruling 2026-07-10).

Instances walk their pinned definition version node by node. Auto nodes
(start/system/notify/end) advance inline; approval and request_info nodes park
the walk until decisions/answers arrive, then ``decide``/``answer_info`` resume
it — events, never live waits.

The engine is framework-agnostic: it raises ``EngineError(code, message)`` and
the router maps codes to HTTP statuses. It never imports app models; who-may-act
context (admin?) is passed in by the caller.
"""

from datetime import datetime
from typing import Any, Optional

from sqlmodel import Session, select

from . import events, registry
from .conditions import evaluate
from .definitions import get_active_definition, resolve_definition
from .models import (
    AssigneeStatus,
    ExecutionStatus,
    InfoRequest,
    InfoRequestStatus,
    InstanceStatus,
    NodeAssignee,
    NodeDecision,
    NodeExecution,
    NodeType,
    ProcessDefinition,
    ProcessInstance,
    ProcessNode,
    ProcessTransition,
    Verdict,
    merge_result,
)

# The outcome recorded when a negative verdict has no matching transition (the
# designed fail-safe in _advance_or_fail). Exported, because a host comparing
# `instance.outcome` would otherwise restate the literal — and a typo there
# silently treats every rejection as an approval.
REJECTED_OUTCOME = "rejected"


class EngineError(Exception):
    """Domain error; ``code`` is machine-readable for the router's HTTP mapping."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _nodes_by_key(definition: ProcessDefinition) -> dict[str, ProcessNode]:
    return {n.node_key: n for n in definition.nodes}


def _transitions_from(definition: ProcessDefinition, node_key: str) -> list[ProcessTransition]:
    outs = [t for t in definition.transitions if t.from_node_key == node_key]
    return sorted(outs, key=lambda t: t.sort_order)


def _select_next(
    definition: ProcessDefinition, node_key: str, data: dict, *, include_default: bool = True
) -> Optional[str]:
    """First transition (declared order) whose condition matches; None if none do.

    ``include_default=False`` skips the unconditional default/else branch — on an
    approval node that branch IS the positive path, so a negative verdict may only
    follow transitions that explicitly match (else the rejected fail-safe fires)."""
    for t in _transitions_from(definition, node_key):
        if t.condition is None and not include_default:
            continue
        if evaluate(t.condition, data):
            return t.to_node_key
    return None


# ── opening ────────────────────────────────────────────────────────────────────

# (session) -> org of the current unit of work, or None (platform scope).
# The T2 resolver shape from DR 0001 — same contract as asas-lookups.
_org_resolver = None

# open_instance's "org_id not passed" sentinel: an explicit org_id=None is a
# deliberate platform-scoped instance and must never be replaced by the
# resolver's org (the asas-ratelimit clock-sentinel pattern).
_ORG_UNSET: Any = object()


def configure_org_resolver(fn) -> None:
    """Host hook supplying the current org (``Callable[[Session], Optional[int]]``,
    or None to unconfigure). Consulted per ``open_instance`` when no explicit
    ``org_id`` is passed."""
    global _org_resolver
    _org_resolver = fn


def open_instance(
    session: Session,
    *,
    process_key: Optional[str] = None,
    purpose: Optional[str] = None,
    bound_to: Optional[tuple[str, int]] = None,
    entity_type: str,
    entity_id: int,
    subject_snapshot: dict,
    data: Optional[dict] = None,
    initiated_by: Optional[int] = None,
    supersedes_id: Optional[int] = None,
    org_id: Optional[int] = _ORG_UNSET,
) -> ProcessInstance:
    """Open a walk. Callers name either an exact ``process_key``, or a ``purpose``
    (+ optional ``bound_to`` subject) and let binding resolution pick the variant
    (TEAMY-293): the subject's ProcessBinding when set, else the purpose's default."""
    if (process_key is None) == (purpose is None):
        raise EngineError("unknown_process", "Pass exactly one of process_key / purpose")
    if purpose is not None:
        definition = resolve_definition(session, purpose, bound_to)
    else:
        definition = get_active_definition(session, process_key)
    label = process_key or purpose
    if definition is None:
        raise EngineError("unknown_process", f"No active definition for {label!r}")
    if definition.entity_type != entity_type:
        raise EngineError(
            "entity_mismatch",
            f"Process {label!r} runs on {definition.entity_type!r}, got {entity_type!r}",
        )
    if org_id is _ORG_UNSET:
        org_id = _org_resolver(session) if _org_resolver is not None else None
    instance = ProcessInstance(
        definition_id=definition.id,
        entity_type=entity_type,
        entity_id=entity_id,
        # Stamped explicit-parameter-first, then the resolver (DR 0001 T4,
        # issue #31 / audit defect T-4): with org_id always None, both
        # resolve_floor call sites resolved the UNSCOPED approver floor —
        # the floor exists precisely so one org's approvals never land in
        # another org's inboxes. None remains a real platform scope.
        org_id=org_id,
        subject_snapshot=subject_snapshot or {},
        data=dict(data or {}),
        initiated_by=initiated_by,
        supersedes_id=supersedes_id,
    )
    session.add(instance)
    session.flush()
    events.emit(
        "process.opened",
        {
            "instance_id": instance.id,
            "process_key": definition.key,
            "initiated_by": initiated_by,
            "session": session,
        },
    )
    start = next(n for n in definition.nodes if n.type == NodeType.start)
    _run_from(session, instance, definition, start.node_key)
    session.commit()
    session.refresh(instance)
    return instance


# ── the walk ───────────────────────────────────────────────────────────────────


def _new_execution(session: Session, instance: ProcessInstance, node: ProcessNode) -> NodeExecution:
    prior = session.exec(
        select(NodeExecution)
        .where(NodeExecution.instance_id == instance.id)
        .where(NodeExecution.node_key == node.node_key)
        .order_by(NodeExecution.seq.desc())
    ).first()
    execution = NodeExecution(
        instance_id=instance.id,
        node_key=node.node_key,
        node_type=node.type,
        seq=(prior.seq + 1) if prior else 1,
    )
    session.add(execution)
    session.flush()
    return execution


def _run_from(
    session: Session, instance: ProcessInstance, definition: ProcessDefinition, node_key: str
) -> None:
    """Walk auto nodes until the instance parks (approval/request_info), errors,
    or completes at an end node."""
    nodes = _nodes_by_key(definition)
    current: Optional[str] = node_key
    while current is not None:
        node = nodes.get(current)
        if node is None:  # pragma: no cover - validate_definition prevents this
            raise EngineError("bad_definition", f"Unknown node {current!r}")
        instance.current_node_key = node.node_key
        session.add(instance)
        execution = _new_execution(session, instance, node)
        events.emit(
            "node.activated",
            {
                "instance_id": instance.id,
                "node_key": node.node_key,
                "type": node.type.value,
                "execution_id": execution.id,
                "session": session,
            },
        )

        if node.type in (NodeType.start, NodeType.notify):
            if node.type == NodeType.notify:
                events.emit(
                    (node.config or {}).get("event", "process.notify"),
                    {"instance_id": instance.id, **((node.config or {}).get("params") or {})},
                )
            _complete_execution(session, execution, {})
            current = _advance_or_fail(session, instance, definition, node, positive=True)

        elif node.type == NodeType.system:
            handler = registry.get_system_handler((node.config or {}).get("handler_key"))
            try:
                produced = handler(session, instance) or {}
            except Exception as exc:  # noqa: BLE001 - fail loud to error state
                _fail_instance(session, instance, execution, exc)
                return
            instance.data = {**instance.data, **produced}
            session.add(instance)
            _complete_execution(session, execution, {"produced": sorted(produced.keys())})
            current = _advance_or_fail(session, instance, definition, node, positive=True)

        elif node.type == NodeType.approval:
            _activate_approval(session, instance, execution, node)
            return  # parked until decisions arrive

        elif node.type == NodeType.request_info:
            _activate_request_info(session, instance, execution, node)
            return  # parked until the answer arrives

        elif node.type == NodeType.end:
            _complete_execution(session, execution, {})
            _complete_instance(session, instance, definition, (node.config or {})["outcome"])
            return


def _advance_or_fail(
    session: Session,
    instance: ProcessInstance,
    definition: ProcessDefinition,
    node: ProcessNode,
    *,
    positive: bool,
) -> Optional[str]:
    """Pick the next node. A negative verdict with no matching transition is the
    designed fail-safe (terminate rejected); anything else unmatched is a
    definition bug — fail loud to error state."""
    nxt = _select_next(definition, node.node_key, instance.data, include_default=positive)
    if nxt is not None:
        return nxt
    if not positive:
        _complete_instance(session, instance, definition, REJECTED_OUTCOME)
        return None
    _fail_instance(
        session,
        instance,
        None,
        EngineError("no_transition", f"No transition matched from node '{node.node_key}'"),
    )
    return None


def _complete_execution(session: Session, execution: NodeExecution, result: dict) -> None:
    execution.status = ExecutionStatus.completed
    execution.result = result
    execution.completed_at = datetime.utcnow()
    session.add(execution)
    # Outstanding asks die with the execution (never deleted).
    for assignee in execution.assignees:
        if assignee.status == AssigneeStatus.requested:
            assignee.status = AssigneeStatus.not_required
            session.add(assignee)
    for ask in execution.info_requests:
        if ask.status == InfoRequestStatus.pending:
            ask.status = InfoRequestStatus.withdrawn
            session.add(ask)


def _complete_instance(
    session: Session, instance: ProcessInstance, definition: ProcessDefinition, outcome: str
) -> None:
    instance.status = InstanceStatus.completed
    instance.outcome = outcome
    instance.completed_at = datetime.utcnow()
    instance.current_node_key = None
    session.add(instance)
    # Callbacks key off the PURPOSE (TEAMY-293) so variants share their owner's
    # apply logic; `or key` covers pre-purpose rows not yet re-seeded.
    callback = registry.completion_callback(definition.purpose or definition.key)
    if callback is not None:
        # The owner's effect is part of the same transaction — a callback failure
        # rolls the completion back rather than half-applying it.
        callback(session, instance, outcome)
    events.emit(
        "process.completed",
        {
            "instance_id": instance.id,
            "process_key": definition.key,
            "outcome": outcome,
            "session": session,
        },
    )


def _fail_instance(
    session: Session,
    instance: ProcessInstance,
    execution: Optional[NodeExecution],
    exc: Exception,
) -> None:
    message = str(exc) or exc.__class__.__name__
    if execution is not None:
        execution.status = ExecutionStatus.error
        execution.error = message
        execution.completed_at = datetime.utcnow()
        session.add(execution)
    instance.status = InstanceStatus.error
    instance.error = message
    session.add(instance)
    events.emit("process.error", {"instance_id": instance.id, "error": message})


# ── approval nodes ─────────────────────────────────────────────────────────────


def _activate_approval(
    session: Session, instance: ProcessInstance, execution: NodeExecution, node: ProcessNode
) -> None:
    cfg = node.config or {}
    resolved: dict[int, str] = {}
    for principal in cfg.get("principals", []):
        for member_id in registry.resolve_assignees(
            session, principal, instance.entity_type, instance.entity_id
        ):
            resolved.setdefault(member_id, principal)
    # Self-approval excluded at resolution time (re-checked at decision time).
    if instance.initiated_by is not None:
        resolved.pop(instance.initiated_by, None)
    if not resolved:
        # Fail closed to the admin/hr floor — never silent auto-approve, never stall.
        floor = registry.resolve_floor(session, instance.org_id) - {instance.initiated_by}
        resolved = {m: registry.ADMIN_FLOOR for m in floor}
    if not resolved:
        _fail_instance(
            session,
            instance,
            execution,
            EngineError("empty_resolution", f"No approver resolvable for node '{node.node_key}'"),
        )
        return
    for member_id, principal in sorted(resolved.items()):
        session.add(
            NodeAssignee(execution_id=execution.id, member_id=member_id, via_principal=principal)
        )
    session.flush()
    # After resolution on purpose (node.activated fires before assignees exist):
    # this is the "approvers now waiting" signal the notifications bridge consumes.
    events.emit(
        "approval.activated",
        {
            "instance_id": instance.id,
            "node_key": node.node_key,
            "execution_id": execution.id,
            "assignee_member_ids": sorted(resolved),
            "session": session,
        },
    )


def _activate_request_info(
    session: Session, instance: ProcessInstance, execution: NodeExecution, node: ProcessNode
) -> None:
    """Standalone request_info node: ask the configured principal; first answer
    completes the node (same machinery as the approval-action ask)."""
    cfg = node.config or {}
    members = registry.resolve_assignees(
        session, cfg["principal"], instance.entity_type, instance.entity_id
    )
    if not members:
        members = registry.resolve_floor(session, instance.org_id)
    if not members:
        _fail_instance(
            session,
            instance,
            execution,
            EngineError("empty_resolution", f"Nobody resolvable for node '{node.node_key}'"),
        )
        return
    for member_id in sorted(members):
        session.add(
            NodeAssignee(
                execution_id=execution.id, member_id=member_id, via_principal=cfg["principal"]
            )
        )
        session.add(
            InfoRequest(
                execution_id=execution.id,
                asked_by=instance.initiated_by or 0,
                asked_of=member_id,
                question=cfg["prompt"],
            )
        )
    session.flush()
    events.emit(
        "info.requested",
        {"instance_id": instance.id, "node_key": node.node_key, "session": session},
    )


# ── acting on a parked instance ────────────────────────────────────────────────


def _load_active_execution(session: Session, instance: ProcessInstance) -> NodeExecution:
    execution = session.exec(
        select(NodeExecution)
        .where(NodeExecution.instance_id == instance.id)
        .where(NodeExecution.status == ExecutionStatus.active)
        .with_for_update()  # row-locked on Postgres; no-op on SQLite (single writer)
    ).first()
    if execution is None:
        raise EngineError("not_pending", "Instance has no active step awaiting action")
    return execution


def _require_running(instance: ProcessInstance) -> None:
    if instance.status != InstanceStatus.running:
        raise EngineError("not_running", f"Instance is {instance.status.value}")


def _definition_of(session: Session, instance: ProcessInstance) -> ProcessDefinition:
    return session.get(ProcessDefinition, instance.definition_id)


def decide(
    session: Session,
    instance: ProcessInstance,
    *,
    actor_id: int,
    verdict: Verdict,
    comment: Optional[str] = None,
    actor_is_admin: bool = False,
    on_behalf_of: Optional[int] = None,
) -> ProcessInstance:
    _require_running(instance)
    execution = _load_active_execution(session, instance)
    if execution.node_type != NodeType.approval:
        raise EngineError("not_approval", "The active step is not an approval")
    if verdict not in (Verdict.positive, Verdict.negative):
        raise EngineError("bad_verdict", "decide() takes a final verdict; use request_info()")
    # Self-approval re-checked at decision time (delegation adds the second leg in WXL-217).
    if instance.initiated_by is not None and actor_id == instance.initiated_by:
        raise EngineError("self_approval", "The initiator cannot decide their own request")
    assignee = _assignee_of(execution, actor_id)
    if assignee is None and not actor_is_admin:
        raise EngineError("not_assignee", "Not asked to decide this step")
    if verdict == Verdict.negative and not (comment or "").strip():
        raise EngineError("comment_required", "A comment is required when rejecting")

    session.add(
        NodeDecision(
            execution_id=execution.id,
            actor_id=actor_id,
            verdict=verdict,
            comment=comment,
            on_behalf_of=on_behalf_of,
        )
    )
    if assignee is not None:
        assignee.status = AssigneeStatus.decided
        session.add(assignee)
    session.flush()  # partial unique index rejects a second final verdict by this actor

    definition = _definition_of(session, instance)
    node = _nodes_by_key(definition)[execution.node_key]
    if verdict == Verdict.negative:
        # Any negative is a veto — the node completes immediately (JSM lesson).
        _finish_approval(session, instance, definition, node, execution, Verdict.negative, actor_id)
    else:
        quorum = (node.config or {}).get("quorum", "any")
        if quorum == "any" or _all_assignees_decided_positive(session, execution):
            _finish_approval(
                session, instance, definition, node, execution, Verdict.positive, actor_id
            )
    session.commit()
    session.refresh(instance)
    return instance


def final_decision_of(
    session: Session, instance: ProcessInstance
) -> tuple[Optional[int], Optional[str]]:
    """(actor_id, comment) of the **latest positive or negative NodeDecision**
    recorded against this instance — already flushed when a completion callback
    runs, so callbacks can attribute an outcome to its decider (WXL-215/216 both
    need this). It does not require the instance to have completed, and it does
    not check that this decision is what ended it.

    **This does not tell you how the instance ended**, which the name invites
    you to expect. What settles that is the *completion outcome* — the end
    node's configured ``config["outcome"]``, or :data:`REJECTED_OUTCOME` when a
    negative verdict had no matching transition — and it reaches a completion
    callback as its third argument. It is not ``NodeDecision.verdict``, and a
    truthiness test over this tuple is wrong in both directions.
    """
    row = session.exec(
        select(NodeDecision)
        .join(NodeExecution, NodeDecision.execution_id == NodeExecution.id)
        .where(NodeExecution.instance_id == instance.id)
        .where(NodeDecision.verdict.in_((Verdict.positive, Verdict.negative)))
        .order_by(NodeDecision.id.desc())
    ).first()
    return (row.actor_id, row.comment) if row is not None else (None, None)


def _assignee_of(execution: NodeExecution, actor_id: int) -> Optional[NodeAssignee]:
    for a in execution.assignees:
        if a.member_id == actor_id and a.status == AssigneeStatus.requested:
            return a
    return None


def _all_assignees_decided_positive(session: Session, execution: NodeExecution) -> bool:
    required = [a for a in execution.assignees if a.status != AssigneeStatus.not_required]
    positive_actors = {
        d.actor_id
        for d in session.exec(
            select(NodeDecision)
            .where(NodeDecision.execution_id == execution.id)
            .where(NodeDecision.verdict == Verdict.positive)
        ).all()
    }
    return all(a.member_id in positive_actors for a in required)


def _finish_approval(
    session: Session,
    instance: ProcessInstance,
    definition: ProcessDefinition,
    node: ProcessNode,
    execution: NodeExecution,
    verdict: Verdict,
    decided_by: int,
) -> None:
    result = {"verdict": verdict.value, "decided_by": decided_by}
    _complete_execution(session, execution, result)
    instance.data = merge_result(instance.data, node.node_key, result)
    session.add(instance)
    nxt = _advance_or_fail(
        session, instance, definition, node, positive=(verdict == Verdict.positive)
    )
    if nxt is not None:
        _run_from(session, instance, definition, nxt)


def request_info(
    session: Session,
    instance: ProcessInstance,
    *,
    actor_id: int,
    asked_of: int,
    question: str,
    actor_is_admin: bool = False,
) -> InfoRequest:
    """Approval-action ask: any member may be asked (owner ruling 2026-07-10).
    The node stays active — other assignees may still decide, prior approvals
    stay intact; the asker still owes a final verdict."""
    _require_running(instance)
    execution = _load_active_execution(session, instance)
    if execution.node_type != NodeType.approval:
        raise EngineError("not_approval", "The active step is not an approval")
    definition = _definition_of(session, instance)
    node = _nodes_by_key(definition)[execution.node_key]
    if not (node.config or {}).get("allow_request_info", True):
        raise EngineError("not_allowed", "This step does not allow requesting information")
    if _assignee_of(execution, actor_id) is None and not actor_is_admin:
        raise EngineError("not_assignee", "Not asked to decide this step")
    if not (question or "").strip():
        raise EngineError("question_required", "A question is required")

    session.add(
        NodeDecision(
            execution_id=execution.id,
            actor_id=actor_id,
            verdict=Verdict.request_info,
            comment=question,
        )
    )
    ask = InfoRequest(
        execution_id=execution.id, asked_by=actor_id, asked_of=asked_of, question=question
    )
    session.add(ask)
    session.flush()  # id for the event; emitted pre-commit so subscriber inserts ride along
    events.emit(
        "info.requested",
        {
            "instance_id": instance.id,
            "info_request_id": ask.id,
            "asked_of": asked_of,
            "session": session,
        },
    )
    session.commit()
    session.refresh(ask)
    return ask


def answer_info(
    session: Session,
    ask: InfoRequest,
    *,
    actor_id: int,
    answer: str,
    actor_is_admin: bool = False,
) -> ProcessInstance:
    if ask.status != InfoRequestStatus.pending:
        raise EngineError("not_pending", f"Info request is {ask.status.value}")
    if actor_id != ask.asked_of and not actor_is_admin:
        raise EngineError("not_asked", "Only the asked member can answer")
    if not (answer or "").strip():
        raise EngineError("answer_required", "An answer is required")
    execution = ask.execution
    instance = execution.instance
    _require_running(instance)

    ask.answer = answer
    ask.status = InfoRequestStatus.answered
    ask.answered_at = datetime.utcnow()
    session.add(ask)
    answers = list(instance.data.get("info_requests") or [])
    answers.append(
        {
            "node_key": execution.node_key,
            "question": ask.question,
            "answer": answer,
            "answered_by": actor_id,
        }
    )
    instance.data = {**instance.data, "info_requests": answers}
    session.add(instance)

    if execution.node_type == NodeType.request_info:
        # Standalone node: the first answer completes it and the walk resumes.
        definition = _definition_of(session, instance)
        node = _nodes_by_key(definition)[execution.node_key]
        _complete_execution(session, execution, {"answer": answer, "answered_by": actor_id})
        instance.data = merge_result(
            instance.data, node.node_key, {"answer": answer, "answered_by": actor_id}
        )
        session.add(instance)
        nxt = _advance_or_fail(session, instance, definition, node, positive=True)
        if nxt is not None:
            _run_from(session, instance, definition, nxt)
    events.emit(
        "info.answered",
        {"instance_id": instance.id, "info_request_id": ask.id, "session": session},
    )
    session.commit()
    session.refresh(instance)
    return instance


def cancel(
    session: Session, instance: ProcessInstance, *, actor_id: int, actor_is_admin: bool = False
) -> ProcessInstance:
    _require_running(instance)
    if actor_id != instance.initiated_by and not actor_is_admin:
        raise EngineError("not_initiator", "Only the initiator (or an admin) can cancel")
    for execution in instance.executions:
        if execution.status == ExecutionStatus.active:
            _complete_execution(session, execution, {"canceled": True})
    instance.status = InstanceStatus.canceled
    instance.completed_at = datetime.utcnow()
    instance.current_node_key = None
    session.add(instance)
    session.commit()
    session.refresh(instance)
    events.emit("process.canceled", {"instance_id": instance.id, "by": actor_id})
    return instance


def retry(session: Session, instance: ProcessInstance) -> ProcessInstance:
    """Admin retry of an errored instance: re-activate the failed node as a NEW
    execution (per-activation audit); the old error row stays."""
    if instance.status != InstanceStatus.error:
        raise EngineError("not_error", f"Instance is {instance.status.value}, not error")
    definition = _definition_of(session, instance)
    if instance.current_node_key is None:
        raise EngineError("no_node", "Errored instance has no current node")
    instance.status = InstanceStatus.running
    instance.error = None
    session.add(instance)
    _run_from(session, instance, definition, instance.current_node_key)
    session.commit()
    session.refresh(instance)
    return instance


def reassign(
    session: Session, assignee: NodeAssignee, *, new_member_id: int
) -> NodeAssignee:
    """Admin reassignment (audited): old row flips to not_required, new row links back."""
    if assignee.status != AssigneeStatus.requested:
        raise EngineError("not_pending", f"Assignee is {assignee.status.value}")
    execution = assignee.execution
    if execution.status != ExecutionStatus.active:
        raise EngineError("not_pending", "The step is no longer active")
    assignee.status = AssigneeStatus.not_required
    session.add(assignee)
    replacement = NodeAssignee(
        execution_id=execution.id,
        member_id=new_member_id,
        via_principal=assignee.via_principal,
        reassigned_from=assignee.member_id,
    )
    session.add(replacement)
    session.commit()
    session.refresh(replacement)
    return replacement
