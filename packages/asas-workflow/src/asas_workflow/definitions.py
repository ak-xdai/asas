"""Definition specs, graph validation, and the DB seed loader.

Definitions live in the DB (owner ruling 2026-07-10) but v1 definitions are
declared in code as ``DefinitionSpec``s by their owning modules (wiring calls
``register_definition``) and upserted by ``seed_definitions`` at startup —
idempotent, house seed pattern. The admin editor UI is a fast-follow; when it
lands it writes the same tables and runs the same ``validate_definition``.

Versioning (Camunda consensus): a definition row set is immutable once an
instance references it. ``seed_definitions`` compares a canonical spec hash —
unchanged spec = no-op; changed spec = archive the old version, insert
``(key, version+1)``. In-flight instances stay pinned to their version.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlmodel import Session, select

from . import registry
from .conditions import check_condition_shape, condition_fields
from .models import (
    DefinitionStatus,
    NodeType,
    ProcessBinding,
    ProcessDefinition,
    ProcessNode,
    ProcessTransition,
)

QUORUMS = ("any", "all")


@dataclass(frozen=True)
class NodeSpec:
    key: str
    name: str
    type: NodeType
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionSpec:
    from_key: str
    to_key: str
    condition: Optional[dict] = None  # None = default/else branch


@dataclass(frozen=True)
class DefinitionSpec:
    key: str
    name: str
    entity_type: str
    nodes: tuple[NodeSpec, ...]
    transitions: tuple[TransitionSpec, ...]  # declared order = evaluation order
    data_fields: tuple[str, ...] = ()
    # What the process is FOR (TEAMY-293). None/omitted = the key itself, which
    # also marks the definition as its purpose's DEFAULT (key == purpose).
    # Variants set purpose to the default's key (e.g. "project_change_set").
    purpose: Optional[str] = None

    @property
    def resolved_purpose(self) -> str:
        return self.purpose or self.key


_SPECS: dict[str, DefinitionSpec] = {}


def register_definition(spec: DefinitionSpec) -> None:
    """Owning modules register their process specs here (from wiring), before
    ``seed_definitions`` runs in init_db."""
    _SPECS[spec.key] = spec


def registered_specs() -> list[DefinitionSpec]:
    return list(_SPECS.values())


def clear_registered_specs() -> None:
    """Test helper."""
    _SPECS.clear()


# ── validation (fails loud, mirrors validation.assert_rules_known) ────────────


def validate_definition(spec: DefinitionSpec, session: Optional[Session] = None) -> None:
    """Raise ValueError listing every problem with the graph. Runs at seed/publish
    time — after wiring, so handler/principal registries are populated. With a
    ``session``, namespaced principals (``ns:param``) also get their param checked
    (e.g. `project_role:<code>` verifies the lookup code exists) — seed/publish
    always passes one; session-less calls skip only that DB-backed check."""
    problems: list[str] = []
    keys = [n.key for n in spec.nodes]
    by_key = {n.key: n for n in spec.nodes}

    if len(keys) != len(set(keys)):
        problems.append("duplicate node keys")
    starts = [n for n in spec.nodes if n.type == NodeType.start]
    ends = [n for n in spec.nodes if n.type == NodeType.end]
    if len(starts) != 1:
        problems.append(f"exactly one start node required (found {len(starts)})")
    if not ends:
        problems.append("at least one end node required")

    # Node config checks against the live registries.
    for n in spec.nodes:
        cfg = n.config or {}
        if n.type == NodeType.approval:
            principals = cfg.get("principals")
            if not principals or not isinstance(principals, list):
                problems.append(f"approval node '{n.key}' needs a non-empty principals list")
            else:
                for p in principals:
                    if not registry.has_assignee_resolver(p):
                        problems.append(
                            f"approval node '{n.key}' references unregistered principal '{p}'"
                        )
                    elif session is not None:
                        param_problem = registry.check_principal_param(session, p)
                        if param_problem:
                            problems.append(
                                f"approval node '{n.key}' principal '{p}': {param_problem}"
                            )
            if cfg.get("quorum", "any") not in QUORUMS:
                problems.append(f"approval node '{n.key}' quorum must be one of {QUORUMS}")
        elif n.type == NodeType.system:
            handler_key = cfg.get("handler_key")
            if not handler_key or not registry.has_system_handler(handler_key):
                problems.append(
                    f"system node '{n.key}' references unregistered handler {handler_key!r}"
                )
        elif n.type == NodeType.request_info:
            if not cfg.get("prompt"):
                problems.append(f"request_info node '{n.key}' needs a prompt")
            principal = cfg.get("principal")
            if not principal or not registry.has_assignee_resolver(principal):
                problems.append(
                    f"request_info node '{n.key}' references unregistered principal {principal!r}"
                )
            elif session is not None:
                param_problem = registry.check_principal_param(session, principal)
                if param_problem:
                    problems.append(
                        f"request_info node '{n.key}' principal '{principal}': {param_problem}"
                    )
        elif n.type == NodeType.notify:
            if not cfg.get("event"):
                problems.append(f"notify node '{n.key}' needs an event name")
        elif n.type == NodeType.end:
            if not cfg.get("outcome"):
                problems.append(f"end node '{n.key}' needs an outcome")

    # Transition checks.
    out: dict[str, list[TransitionSpec]] = {k: [] for k in keys}
    for t in spec.transitions:
        if t.from_key not in by_key or t.to_key not in by_key:
            problems.append(f"transition {t.from_key!r} -> {t.to_key!r} references unknown node")
            continue
        if by_key[t.from_key].type == NodeType.end:
            problems.append(f"end node '{t.from_key}' must not have out-transitions")
            continue
        out[t.from_key].append(t)
        for problem in check_condition_shape(t.condition):
            problems.append(f"transition {t.from_key!r} -> {t.to_key!r}: {problem}")
        unknown = _unknown_condition_fields(t.condition, spec, keys)
        for f in sorted(unknown):
            problems.append(
                f"transition {t.from_key!r} -> {t.to_key!r} reads undeclared field '{f}' "
                "(declare it in data_fields or reference a node result)"
            )

    for n in spec.nodes:
        if n.type == NodeType.end:
            continue
        outs = out.get(n.key, [])
        if not outs:
            problems.append(f"node '{n.key}' has no out-transition (dead end)")
            continue
        conditional = [t for t in outs if t.condition is not None]
        defaults = [t for t in outs if t.condition is None]
        # Default/else required wherever conditions exist, except approval nodes,
        # where an unmatched NEGATIVE verdict fail-safes to instance rejection —
        # but the positive path must still be covered (a default, or the node has
        # only an unconditional transition).
        if conditional and not defaults and n.type != NodeType.approval:
            problems.append(
                f"node '{n.key}' has conditional out-transitions but no default/else branch"
            )
        if defaults and outs and outs[-1].condition is not None:
            problems.append(f"node '{n.key}': the default/else transition must be declared last")
        if len(defaults) > 1:
            problems.append(f"node '{n.key}' has more than one default/else transition")

    # Reachability over the full edge set (conditions ignored).
    if starts and not problems:
        edges: dict[str, set[str]] = {k: set() for k in keys}
        for t in spec.transitions:
            edges[t.from_key].add(t.to_key)
        reachable = _closure(edges, {starts[0].key})
        unreachable = set(keys) - reachable
        if unreachable:
            problems.append(f"nodes unreachable from start: {sorted(unreachable)}")
        end_keys = {n.key for n in ends}
        reverse: dict[str, set[str]] = {k: set() for k in keys}
        for t in spec.transitions:
            reverse[t.to_key].add(t.from_key)
        # Approval nodes reach an end implicitly via the negative fail-safe.
        can_finish = _closure(reverse, end_keys) | {
            n.key for n in spec.nodes if n.type == NodeType.approval
        }
        stuck = reachable - can_finish
        if stuck:
            problems.append(f"nodes from which no end is reachable: {sorted(stuck)}")

    # A variant (purpose != key) must run on the same subject type as its
    # purpose's default — callbacks and callers assume one entity_type per purpose.
    if session is not None and spec.resolved_purpose != spec.key:
        default = get_active_definition(session, spec.resolved_purpose)
        if default is not None and default.entity_type != spec.entity_type:
            problems.append(
                f"variant runs on '{spec.entity_type}' but its purpose "
                f"'{spec.resolved_purpose}' default runs on '{default.entity_type}'"
            )

    if problems:
        raise ValueError(
            f"invalid process definition '{spec.key}': " + "; ".join(problems)
        )


def _unknown_condition_fields(
    condition: Optional[dict], spec: DefinitionSpec, node_keys: list[str]
) -> set[str]:
    declared = set(spec.data_fields)
    unknown = set()
    for f in condition_fields(condition):
        if f in declared:
            continue
        # Node results are implicitly declared: nodes.<known_key>.<anything>.
        parts = f.split(".")
        if len(parts) >= 3 and parts[0] == "nodes" and parts[1] in node_keys:
            continue
        unknown.add(f)
    return unknown


def _closure(edges: dict[str, set[str]], seeds: set[str]) -> set[str]:
    seen = set(seeds)
    frontier = list(seeds)
    while frontier:
        for nxt in edges.get(frontier.pop(), ()):  # noqa: B909 - snapshot not needed
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return seen


# ── seed loader (idempotent; versioning on change) ─────────────────────────────


def spec_hash(spec: DefinitionSpec) -> str:
    canonical = json.dumps(
        {
            "key": spec.key,
            "name": spec.name,
            "entity_type": spec.entity_type,
            "purpose": spec.resolved_purpose,
            "data_fields": list(spec.data_fields),
            "nodes": [
                {"key": n.key, "name": n.name, "type": n.type.value, "config": n.config}
                for n in spec.nodes
            ],
            "transitions": [
                {"from": t.from_key, "to": t.to_key, "condition": t.condition}
                for t in spec.transitions
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def get_active_definition(session: Session, key: str) -> Optional[ProcessDefinition]:
    return session.exec(
        select(ProcessDefinition)
        .where(ProcessDefinition.key == key)
        .where(ProcessDefinition.status == DefinitionStatus.active)
        .order_by(ProcessDefinition.version.desc())  # defensive; one active per key
    ).first()


# ── purpose & binding resolution (TEAMY-293) ───────────────────────────────────


def active_definitions_for_purpose(session: Session, purpose: str) -> list[ProcessDefinition]:
    """Every active definition serving a purpose (default + variants), default first."""
    rows = session.exec(
        select(ProcessDefinition)
        .where(ProcessDefinition.purpose == purpose)
        .where(ProcessDefinition.status == DefinitionStatus.active)
        .order_by(ProcessDefinition.key)
    ).all()
    return sorted(rows, key=lambda d: (d.key != purpose, d.key))


def get_binding(
    session: Session, entity_type: str, entity_id: int, purpose: str
) -> Optional[ProcessBinding]:
    return session.exec(
        select(ProcessBinding)
        .where(ProcessBinding.entity_type == entity_type)
        .where(ProcessBinding.entity_id == entity_id)
        .where(ProcessBinding.purpose == purpose)
    ).first()


def set_binding(
    session: Session,
    entity_type: str,
    entity_id: int,
    purpose: str,
    definition_key: Optional[str],
) -> Optional[ProcessBinding]:
    """Bind a subject to a variant (or clear with None → back to the default).
    The key must have an active definition serving the purpose. Commits."""
    if definition_key is not None:
        definition = get_active_definition(session, definition_key)
        if definition is None:
            raise ValueError(f"No active definition {definition_key!r}")
        if definition.purpose != purpose:
            raise ValueError(
                f"Definition {definition_key!r} serves purpose {definition.purpose!r}, "
                f"not {purpose!r}"
            )
    binding = get_binding(session, entity_type, entity_id, purpose)
    if definition_key is None:
        if binding is not None:
            session.delete(binding)
            session.commit()
        return None
    if binding is None:
        binding = ProcessBinding(
            entity_type=entity_type, entity_id=entity_id, purpose=purpose,
            definition_key=definition_key,
        )
    else:
        binding.definition_key = definition_key
    session.add(binding)
    session.commit()
    session.refresh(binding)
    return binding


def resolve_definition(
    session: Session, purpose: str, bound_to: Optional[tuple[str, int]] = None
) -> Optional[ProcessDefinition]:
    """The definition an instance should open on: the subject's binding when one
    exists and still points at an active definition of this purpose, else the
    purpose's default (key == purpose). A stale binding fails safe to the default."""
    if bound_to is not None:
        binding = get_binding(session, bound_to[0], bound_to[1], purpose)
        if binding is not None:
            bound = get_active_definition(session, binding.definition_key)
            if bound is not None and bound.purpose == purpose:
                return bound
    return get_active_definition(session, purpose)


def ensure_definition(session: Session, spec: DefinitionSpec) -> ProcessDefinition:
    """Upsert one spec: no-op when the active version matches; otherwise validate,
    archive the current active version, and insert (key, version+1) as active."""
    digest = spec_hash(spec)
    active = get_active_definition(session, spec.key)
    if active is not None and active.spec_hash == digest:
        return active

    validate_definition(spec, session)

    latest_version = session.exec(
        select(ProcessDefinition.version)
        .where(ProcessDefinition.key == spec.key)
        .order_by(ProcessDefinition.version.desc())
    ).first()
    if active is not None:
        active.status = DefinitionStatus.archived
        session.add(active)

    definition = ProcessDefinition(
        key=spec.key,
        version=(latest_version or 0) + 1,
        name=spec.name,
        entity_type=spec.entity_type,
        purpose=spec.resolved_purpose,
        status=DefinitionStatus.active,
        data_fields=list(spec.data_fields),
        spec_hash=digest,
        published_at=datetime.utcnow(),
    )
    session.add(definition)
    session.flush()  # definition.id for child rows
    for n in spec.nodes:
        session.add(
            ProcessNode(
                definition_id=definition.id,
                node_key=n.key,
                name=n.name,
                type=n.type,
                config=n.config or {},
            )
        )
    for i, t in enumerate(spec.transitions):
        session.add(
            ProcessTransition(
                definition_id=definition.id,
                from_node_key=t.from_key,
                to_node_key=t.to_key,
                sort_order=i,
                condition=t.condition,
            )
        )
    session.commit()
    session.refresh(definition)
    return definition


def seed_definitions(session: Session) -> None:
    """Upsert every registered spec (init_db, after wiring). Idempotent."""
    for spec in registered_specs():
        ensure_definition(session, spec)
