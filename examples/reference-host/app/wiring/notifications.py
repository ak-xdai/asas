"""asas-notifications.

Contract rows: **Routers** (``build_router``), **Schema** (``migrate``),
**Host hooks** (``configure_context_resolver``, ``configure_recipient_filter``).

Also one third of the **escalation composition** (see ``workflow.py``): this
package supplies the *telling*, and knows nothing about approvals.

The recipient filter is the part worth reading twice. A notification is a
**copy** of a fact, made at send time — so if the subject record is restricted,
filtering has to happen *before* the row is written. There is no redaction pass
afterwards, because by then the title is already sitting in someone's inbox.
That is the same rule search has about never indexing restricted fields, and for
the same reason.
"""

from __future__ import annotations

from typing import Iterable, Optional

import asas_access
import asas_notifications as notifications
from sqlmodel import Session

from ..models import DEFAULT_ORG_ID, Agent, Ticket

# Kinds are declared, not invented at the call site: the taxonomy decides how a
# recipient's inbox groups and sorts the row.
KIND_TICKET_ASSIGNED = "ticket.assigned"
KIND_ESCALATION_REQUESTED = "ticket.escalation_requested"
KIND_ESCALATION_DECIDED = "ticket.escalation_decided"
KIND_SLA_BREACHED = "ticket.sla_breached"


def _context_resolver(session: Session) -> Optional[tuple[int, int]]:
    """(org_id, actor_user_id) for the row being written.

    Single-tenant, and this host has no request-scoped actor, so the actor is
    reported as 0 — "the system". A real host would read both off its request
    context. Returning ``None`` is also valid and means "do not stamp".
    """
    return (DEFAULT_ORG_ID, 0)


def _recipient_filter(
    session: Session,
    recipients: Iterable[int],
    entity_type: str,
    entity_id: Optional[int],
    record: object,
) -> set[int]:
    """Drop recipients who may not see the subject record.

    The composition: notifications asks, **access** answers. This host's rule is
    the need-to-know one — a classified ticket only notifies agents whose
    clearance reaches it. Note there is no admin floor here, which is MAC's
    defining property.

    The filter runs for **every** ``notify`` that names an ``entity_type``, and
    receives ``entity_id`` alongside ``record``. ``record`` is ``None`` when the
    producer had only the type and the id — a generic producer cannot load an
    arbitrary subject — so resolve it here rather than assuming it was passed.
    Filtering only when the row happened to arrive is how a classified ticket's
    title reaches an inbox with no error.
    """
    recipients = set(recipients)
    ticket = record if isinstance(record, Ticket) else (
        session.get(Ticket, entity_id)
        if entity_type == "ticket" and entity_id is not None
        else None
    )
    if ticket is None or ticket.classification_code is None:
        return recipients
    record = ticket

    allowed = set()
    for agent_id in recipients:
        agent = session.get(Agent, agent_id)
        if agent is None:
            continue
        if asas_access.mac_allows(session, agent, entity_type, record):
            allowed.add(agent_id)
    return allowed


def configure() -> None:
    """Step 4 of the boot sequence."""
    notifications.configure_context_resolver(_context_resolver)
    notifications.configure_recipient_filter(_recipient_filter)

    notifications.register_kind(
        KIND_TICKET_ASSIGNED,
        category=notifications.Category.action,
        urgency=notifications.Urgency.normal,
        reason=notifications.Reason.participant,
    )
    notifications.register_kind(
        KIND_ESCALATION_REQUESTED,
        category=notifications.Category.action,
        urgency=notifications.Urgency.high,
        reason=notifications.Reason.requested,
    )
    notifications.register_kind(
        KIND_ESCALATION_DECIDED,
        category=notifications.Category.info,
        urgency=notifications.Urgency.normal,
        reason=notifications.Reason.participant,
    )
    notifications.register_kind(
        KIND_SLA_BREACHED,
        category=notifications.Category.warning,
        urgency=notifications.Urgency.high,
        reason=notifications.Reason.watching,
    )

    # Delivery channel. The logging adapter is the package's own, and is the
    # honest default for a reference host: a real one registers an email or chat
    # adapter here, and that is the only line that changes.
    notifications.register_adapter("log", notifications.LoggingAdapter())
