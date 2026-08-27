"""Registration seams — how app code plugs into the engine without the engine
importing app models (mirrors access.principals / changecontrol.registry).

Owning host modules register, from their wiring:

- **System handlers**: ``handler_key -> fn(session, instance) -> dict`` — the dict
  merges into instance.data. A definition naming an unknown handler fails
  validate_definition loud.
- **Completion callbacks**: per process key — ``fn(session, instance, outcome)``;
  the owner applies the effect (flip the ChangeSet, create the membership…). The
  engine decides; owners act.
- **Subject renderers**: per process key — ``fn(session, instance) -> dict`` summary/
  diff structure for the inbox UI; the engine stores no UI.
- **Assignee resolvers**: ``principal -> fn(session, entity_type, entity_id) ->
  set[member_id]`` — the reverse of access's bool resolvers ("who holds this
  principal for this subject"), which don't exist elsewhere; principals keep the
  access vocabulary so they mean one thing platform-wide.
- **Namespaced assignee resolvers** (TEAMY-292): a principal may be
  ``namespace:param`` (e.g. ``project_role:change_manager``) — one registered
  resolver per namespace, ``fn(session, entity_type, entity_id, param) ->
  set[member_id]``, plus an optional ``param_check(session, param) ->
  problem | None`` that validate_definition runs at seed/publish so a typo'd
  param fails loud.
- **Floor resolver**: ``fn(session) -> set[member_id]`` — the admin/hr floor an
  empty/self-excluded resolution fails closed to (never silent auto-approve,
  never stall).
"""

from typing import Any, Callable, Optional

SystemHandler = Callable[[Any, Any], dict]
CompletionCallback = Callable[[Any, Any, str], None]
SubjectRenderer = Callable[[Any, Any], dict]
AssigneeResolver = Callable[[Any, str, int], set]
NamespacedAssigneeResolver = Callable[[Any, str, int, str], set]
ParamCheck = Callable[[Any, str], Optional[str]]  # -> problem string | None
FloorResolver = Callable[[Any, Any], set]  # (session, org_id | None) -> member ids

_SYSTEM_HANDLERS: dict[str, SystemHandler] = {}
_COMPLETION_CALLBACKS: dict[str, CompletionCallback] = {}
_SUBJECT_RENDERERS: dict[str, SubjectRenderer] = {}
_ASSIGNEE_RESOLVERS: dict[str, AssigneeResolver] = {}
_NAMESPACE_RESOLVERS: dict[str, tuple[NamespacedAssigneeResolver, Optional[ParamCheck]]] = {}
_FLOOR_RESOLVER: Optional[FloorResolver] = None

ADMIN_FLOOR = "admin_floor"  # via_principal recorded when resolution fails closed

# Which purposes a subject type can bind a variant for (TEAMY-293) — owning
# wiring registers these; the subject's own API (e.g. /projects/{id}/workflows)
# iterates them.
_BINDABLE_PURPOSES: dict[str, list[str]] = {}


def register_bindable_purpose(entity_type: str, purpose: str) -> None:
    purposes = _BINDABLE_PURPOSES.setdefault(entity_type, [])
    if purpose not in purposes:
        purposes.append(purpose)


def bindable_purposes(entity_type: str) -> list[str]:
    return list(_BINDABLE_PURPOSES.get(entity_type, ()))


def register_system_handler(handler_key: str, fn: SystemHandler) -> None:
    _SYSTEM_HANDLERS[handler_key] = fn


def register_completion_callback(process_key: str, fn: CompletionCallback) -> None:
    """Register the owner's apply-logic for a completed instance.

    Called as ``fn(session, instance, outcome)`` when an instance reaches an end
    node, keyed on the definition's **purpose** (falling back to its key).

    **The callback runs inside the engine's own transaction, and must not
    commit.** That is deliberate: a callback failure then rolls the completion
    back rather than leaving an instance marked complete with none of its
    effects applied. Committing inside the callback silently discards that
    guarantee, and the resulting half-applied state is not recoverable by
    re-running anything.
    """
    _COMPLETION_CALLBACKS[process_key] = fn


def register_subject_renderer(process_key: str, fn: SubjectRenderer) -> None:
    _SUBJECT_RENDERERS[process_key] = fn


def register_assignee_resolver(principal: str, fn: AssigneeResolver) -> None:
    _ASSIGNEE_RESOLVERS[principal] = fn


def register_assignee_resolver_namespace(
    namespace: str, fn: NamespacedAssigneeResolver, param_check: Optional[ParamCheck] = None
) -> None:
    """One resolver for every ``namespace:param`` principal (TEAMY-292)."""
    _NAMESPACE_RESOLVERS[namespace] = (fn, param_check)


def split_principal(principal: str) -> tuple[str, Optional[str]]:
    """``"project_role:change_manager"`` -> ``("project_role", "change_manager")``;
    plain principals return ``(principal, None)``."""
    if ":" in principal:
        namespace, param = principal.split(":", 1)
        return namespace, param
    return principal, None


def register_floor_resolver(fn: FloorResolver) -> None:
    global _FLOOR_RESOLVER
    _FLOOR_RESOLVER = fn


def get_system_handler(handler_key: str) -> SystemHandler:
    try:
        return _SYSTEM_HANDLERS[handler_key]
    except KeyError:  # pragma: no cover - validate_definition prevents this
        raise KeyError(
            f"No system handler registered for {handler_key!r} — register it in "
            "your workflow wiring."
        )


def has_system_handler(handler_key: str) -> bool:
    return handler_key in _SYSTEM_HANDLERS


def completion_callback(process_key: str) -> Optional[CompletionCallback]:
    return _COMPLETION_CALLBACKS.get(process_key)


def subject_renderer(process_key: str) -> Optional[SubjectRenderer]:
    return _SUBJECT_RENDERERS.get(process_key)


def has_assignee_resolver(principal: str) -> bool:
    if principal in _ASSIGNEE_RESOLVERS:
        return True
    namespace, param = split_principal(principal)
    return param is not None and param != "" and namespace in _NAMESPACE_RESOLVERS


def check_principal_param(session: Any, principal: str) -> Optional[str]:
    """Run the namespace's param_check (seed/publish-time); None = fine. Plain
    principals and namespaces without a check always pass."""
    namespace, param = split_principal(principal)
    if param is None or namespace not in _NAMESPACE_RESOLVERS:
        return None
    _fn, param_check = _NAMESPACE_RESOLVERS[namespace]
    return param_check(session, param) if param_check is not None else None


def resolve_assignees(session: Any, principal: str, entity_type: str, entity_id: int) -> set:
    fn = _ASSIGNEE_RESOLVERS.get(principal)
    if fn is not None:
        return set(fn(session, entity_type, entity_id) or set())
    namespace, param = split_principal(principal)
    if param is not None and namespace in _NAMESPACE_RESOLVERS:
        ns_fn, _check = _NAMESPACE_RESOLVERS[namespace]
        return set(ns_fn(session, entity_type, entity_id, param) or set())
    # pragma: no cover - validate_definition prevents this
    raise KeyError(
        f"No assignee resolver registered for principal {principal!r} — register "
        "it in your workflow wiring."
    )


def resolve_floor(session: Any, org_id: Any = None) -> set:
    """The fail-closed decision floor, scoped to ``org_id`` when given — a
    workflow in one org must never land in another org's approval inboxes
    (PR #135 review)."""
    if _FLOOR_RESOLVER is None:
        raise RuntimeError(
            "No admin-floor resolver registered — the host wiring must call "
            "register_floor_resolver() before the engine runs."
        )
    return set(_FLOOR_RESOLVER(session, org_id) or set())
