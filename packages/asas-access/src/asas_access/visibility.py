"""Record-level visibility (Part B). Generalizes the per-entity private-record rule
(originally ``serialize.can_view_team``) so any entity exposing a ``visibility`` attribute
reuses one implementation. Owning modules register which relationship principals may see a
private record of their type (e.g. the teams module registers ``team_lead``/``team_member``).

Like the rest of ``app.access``, this stays framework/config-agnostic: enforce on/off is
the caller's concern, and visibility is read off the record by value (no enum import)."""

from typing import Any, Optional

from sqlmodel import Session

from .actions import action_allowed
from .mac import mac_allows
from .principals import ROLE_ADMIN, held_principals

# The org-wide "see any private record" right beyond the admin floor. Config, not
# code (TEAMY-453): seeded to People Ops (absorbing the old hr half of the floor);
# registered in access_wiring._ACTION_VERBS like every other verb.
VIEW_PRIVATE = "record.view_private"

# Visibility values entities store (compared by value, so no enum coupling).
PUBLIC = "public"
PRIVATE = "private"

# entity_type -> principals that may view a private record (beyond the admin/hr floor).
_PRIVATE_VIEWERS: dict[str, set[str]] = {}


def register_private_viewers(entity_type: str, principals) -> None:
    """Register (or extend) the relationship principals allowed to see a *private*
    record of ``entity_type``."""
    _PRIVATE_VIEWERS.setdefault(entity_type, set()).update(principals)


def _visibility_of(record: Any) -> Any:
    v = getattr(record, "visibility", None)
    return getattr(v, "value", v)  # Enum -> value; plain str/None passes through


def can_view_record(
    session: Session,
    user: Any,
    entity_type: str,
    record: Any,
    *,
    record_org_id: Optional[int] = None,
) -> bool:
    """Whether ``user`` may see ``record``. The **mandatory** layer runs first
    (DR 0021): a caller failing the record's clearance/markings never reaches the
    discretionary logic below — no admin floor applies there. Then: public (or no
    ``visibility``) ⇒ everyone; private ⇒ the admin floor, holders of
    ``record.view_private`` (grant rows — People Ops by default), or any
    registered relationship principal (e.g. the team's lead or members).
    ``None`` user sees only public records."""
    if not mac_allows(
        session, user, entity_type, record, record_org_id=record_org_id
    ):
        return False
    if _visibility_of(record) != PRIVATE:
        return True
    held = held_principals(user, entity_type, record, session)
    if ROLE_ADMIN in held:
        return True
    if _PRIVATE_VIEWERS.get(entity_type, set()) & held:
        return True
    return action_allowed(session, user, VIEW_PRIVATE)
