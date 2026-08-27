"""asas-workflow.

Contract rows: **Schema** (``migrate``), **Seeding**
(``seed_workflow_definitions``).

This module owns the **escalation composition** — the reference host's clearest
demonstration that some features exist only *between* packages:

    asas-workflow      owns the definition and the instance lifecycle
    asas-access        supplies the CHANGE_APPROVER principal
    asas-notifications tells the approver, and tells the requester the verdict

Not one of the three can see the composition from inside itself. Workflow does
not know what a "support lead" is; access does not know an approval exists;
notifications does not know why it is sending. The host is where the feature
lives, and this file is that host's stitching — Teamy's equivalent is
``changecontrol_wiring.py``.

The trap: a definition is **data**, seeded into the database, and the seeded
copy is what runs. Editing ``ESCALATION`` below and restarting does not change
an already-seeded definition — ``ensure_definition`` is keyed on the definition
key and will not silently rewrite a version that live instances are bound to.
Versioning a live process is a deliberate act, not a redeploy.
"""

from __future__ import annotations

import asas_access
import asas_notifications as notifications
import asas_workflow as workflow
from sqlmodel import Session, select

from ..models import Agent, Ticket
from .access import ENTITY
from .notifications import KIND_ESCALATION_DECIDED, KIND_ESCALATION_REQUESTED

PROCESS_KEY = "ticket_escalation"

# The outcome strings a completed instance can carry.
#
# ``OUTCOME_APPROVED`` is ours — an end node's ``config["outcome"]`` is whatever
# the definition says. ``OUTCOME_REJECTED`` is the engine's: a negative verdict
# with no matching transition completes the instance with the literal
# ``"rejected"``. That constant is *not* exported from ``asas_workflow``, so a
# host has no choice but to restate it, as here.
OUTCOME_APPROVED = "approved"
OUTCOME_REJECTED = "rejected"

# A three-node process: start -> approval -> end. Small on purpose; the shape is
# what transfers, not the size.
ESCALATION = workflow.DefinitionSpec(
    key=PROCESS_KEY,
    name="Ticket escalation",
    entity_type="ticket",
    nodes=(
        workflow.NodeSpec(key="start", name="Requested", type=workflow.NodeType.start),
        workflow.NodeSpec(
            key="lead_approval",
            name="Support lead approval",
            type=workflow.NodeType.approval,
            # The principal is resolved by the host, below. The definition names
            # it; it never names people.
            config={"principals": [asas_access.CHANGE_APPROVER]},
        ),
        # An end node MUST carry config["outcome"]. The engine reads it
        # unguarded — omit it and a decision raises KeyError from inside the
        # engine, nowhere near this definition. The outcome string is what the
        # completion callback receives.
        workflow.NodeSpec(
            key="end",
            name="Decided",
            type=workflow.NodeType.end,
            config={"outcome": OUTCOME_APPROVED},
        ),
    ),
    transitions=(
        workflow.TransitionSpec(from_key="start", to_key="lead_approval"),
        workflow.TransitionSpec(from_key="lead_approval", to_key="end"),
    ),
)


def _change_approvers(session, entity_type: str, entity_id: int) -> set:
    """Who may approve an escalation of this ticket?

    Signature is fixed by the package — (session, entity_type, entity_id) -> set
    of user ids — and the meaning is the host's. Here: every admin, minus the
    ticket's own assignee, because approving your own escalation is not an
    approval. That exclusion is exactly the kind of rule a generic engine cannot
    hold, and exactly why the resolver is a host callback.
    """
    ticket = session.get(Ticket, entity_id)
    if ticket is None:
        # No subject, no approvers. Returning every admin here would route an
        # approval for a record nobody can look at.
        return set()

    approvers = {
        agent.id
        for agent in session.exec(select(Agent).where(Agent.role == "admin")).all()
        # **MAC has no admin floor.** An admin without the clearance cannot see
        # this ticket, so asking them to approve it would both leak its existence
        # and block the process on someone who cannot act.
        if asas_access.mac_allows(session, agent, ENTITY, ticket)
    }
    approvers.discard(ticket.assignee_id)
    return approvers


def _on_complete(session: Session, instance, outcome: str) -> None:
    """Completion callback: the notifications third of the composition.

    Workflow calls this when an instance reaches an end node. It does not know a
    notification exists; it knows only that the host asked to be told.

    **Do not commit in here.** The engine runs this inside its own transaction
    precisely so that a failure rolls the completion back rather than leaving an
    instance marked complete with none of its effects applied. Committing would
    discard that guarantee and is the kind of change that looks harmless.

    The verdict arrives as ``outcome`` — note that ``final_decision_of`` returns
    ``(actor_id, comment)``, not the verdict, which is easy to misread.
    """
    ticket = session.get(Ticket, instance.entity_id)
    if ticket is None:
        return

    approved = outcome == OUTCOME_APPROVED
    if approved:
        ticket.status = "escalated"
        session.add(ticket)

    if instance.initiated_by is not None:
        notifications.notify(
            session,
            [instance.initiated_by],
            KIND_ESCALATION_DECIDED,
            title=(
                f"Escalation of ticket #{ticket.id} was "
                f"{'approved' if approved else 'declined'}"
            ),
            entity_type="ticket",
            entity_id=ticket.id,
            record=ticket,
        )


def configure() -> None:
    """Step 4 of the boot sequence."""
    workflow.register_assignee_resolver(asas_access.CHANGE_APPROVER, _change_approvers)
    workflow.register_definition(ESCALATION)
    workflow.register_completion_callback(PROCESS_KEY, _on_complete)


def seed(session: Session) -> None:
    """Step 5. Writes the definition rows registered above."""
    workflow.seed_workflow_definitions(session)
    session.commit()


def request_escalation(session: Session, ticket: Ticket, requester: Agent):
    """Open an escalation, and tell the approvers. The composition, in one call.

    Kept here rather than in the router because it is the *feature*, and the
    router should read as HTTP plumbing over it.
    """
    instance = workflow.open_instance(
        session,
        process_key=PROCESS_KEY,
        entity_type="ticket",
        entity_id=ticket.id,
        subject_snapshot={"title": ticket.title, "priority": ticket.priority_code},
        initiated_by=requester.id,
    )

    approvers = _change_approvers(session, "ticket", ticket.id)
    if approvers:
        notifications.notify(
            session,
            approvers,
            KIND_ESCALATION_REQUESTED,
            title=f"Escalation requested for ticket #{ticket.id}",
            actor_user_id=requester.id,
            entity_type="ticket",
            entity_id=ticket.id,
            record=ticket,
        )
    session.commit()
    return instance
