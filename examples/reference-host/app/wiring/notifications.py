"""asas-notifications.

Contract rows: **Routers** (``build_router``), **Schema** (``migrate``),
**Seeding** (the topic vocabulary), **Host hooks**
(``configure_context_resolver``, ``configure_recipient_filter``).

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
from asas_notifications import NotificationTopic
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..models import DEFAULT_ORG_ID, Agent, Ticket

# The one notification reference that IS declared: topics are rows, seeded
# below, because routing policy and (later) preferences key on them — an emit
# into an unseeded topic fails loud. Everything else travels on the emit
# itself (DR 0003): the *action* is the app's own `entity.verb` reference,
# declared nowhere, and the nature/urgency/reason axes are stated at the call
# site — see the `notify()` calls in `jobs.py` and `workflow.py`.
TOPIC_TICKETS = "tickets"


def _context_resolver(session: Session) -> Optional[tuple[int, int]]:
    """``(user_id, org_id)`` — the package's order, and the order matters.

    An early version returned ``(org_id, actor)``, and nothing failed loudly:
    every feed served whichever agent's id equalled the org's, and every
    resolver-stamped row carried org 0 — which the org-scoped read paths then
    filtered out. A swapped tuple here is invisible until someone opens a feed.

    The actor is whoever ``fake_auth.get_current_user`` stashed on
    ``session.info`` for this request; outside a request (a job sweep, the
    boot) there is nobody, and 0 — "the system" — is reported. Single-tenant,
    so the org is the constant. Returning ``None`` is also valid and means
    "do not stamp".
    """
    return (session.info.get("actor_user_id", 0), DEFAULT_ORG_ID)


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

    # Delivery channel. The logging adapter is the package's own, and is the
    # honest default for a reference host: a real one registers an email or chat
    # adapter here, and that is the only line that changes.
    # **The NAME has to be the one routing resolves.** With no policy rows
    # seeded, 0.16's fallback routes normal/high emits to the channel called
    # "email"; an adapter registered under any other name is never found —
    # dispatch_pending writes the outbox row, resolves no adapter, and marks
    # it `skipped` ("no adapter registered for channel"), silently. The
    # logging adapter therefore registers AS the email channel here; a real
    # host swaps the adapter object and changes nothing else.
    # (Re-creates PR #41 by @ignacio-galindo against the 0.16 wiring.)
    notifications.register_adapter("email", notifications.LoggingAdapter())


def seed(session: Session) -> None:
    """Step 5. The topic vocabulary is the host's, and it is *data*.

    Idempotent, like every seed. The package migration seeds one platform
    topic (``general``, where ad hoc emits land); every topic the host emits
    into by name must be seeded here first, because an unknown topic raises at
    the emit site rather than routing somewhere surprising.

    The check below is only the fast path — two replicas booting at once both
    read "no row" before either writes, the same shape as the SLA sweep's
    claim in ``jobs.py``, and the same rule applies: idempotence under
    concurrency is the database's uniqueness rule (the platform-key index,
    package migration ``0005``), not the query. Losing that race surfaces as
    an ``IntegrityError``, caught under a savepoint so the rest of the seed
    transaction survives.
    """
    if not session.exec(
        select(NotificationTopic).where(NotificationTopic.key == TOPIC_TICKETS)
    ).first():
        savepoint = session.begin_nested()
        try:
            session.add(NotificationTopic(key=TOPIC_TICKETS, name="Tickets"))
            savepoint.commit()
        except IntegrityError:
            savepoint.rollback()  # a concurrent boot seeded it first
    session.commit()
