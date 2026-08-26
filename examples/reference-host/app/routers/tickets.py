"""Ticket routes — where the seams meet HTTP.

Demonstrates: the **call order** a write path follows. Every package appears
here as one line, and the order is the point:

    ratelimit  -> is this caller allowed to ask again yet?
    access     -> may this caller perform this verb at all?
    validation -> is the resulting record coherent?
    <the write>
    search     -> nothing; indexing is the engine's business, not the route's

This file deliberately contains no permission logic of its own. Every ``if`` you
might expect — "only leads see internal notes", "only admins classify" — is a
seed row in ``wiring/access.py``. If you find yourself adding a role check to a
handler, that is the signal you wanted a row.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import asas_access
import asas_ratelimit
import asas_validation
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from ..fake_auth import get_current_user
from ..db import get_session
from ..models import Agent, Ticket
from ..wiring import workflow as workflow_wiring
from ..wiring.access import ENTITY
from ..wiring.ratelimit import TICKET_CREATE

router = APIRouter(prefix="/tickets", tags=["tickets"])


class TicketCreate(BaseModel):
    title: str
    body: str = ""
    priority_code: str = "normal"
    category_code: Optional[str] = None
    # `date`, not `str`. Pydantic parses the ISO string for us, and the model
    # column is a Date — handing SQLModel a string raises from inside the SQLite
    # dialect, a long way from the schema that accepted it.
    due_on: Optional[date] = None
    assignee_id: Optional[int] = None


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    priority_code: Optional[str] = None
    status: Optional[str] = None
    internal_note: Optional[str] = None
    classification_code: Optional[str] = None
    due_on: Optional[date] = None


class TicketRead(BaseModel):
    """The read projection.

    **It has to be an object, not a dict.** ``redact_view`` nulls fields with
    ``hasattr``/``setattr``, so handing it a plain dict silently redacts nothing
    — no error, no warning, and the restricted field goes straight to the
    client. A dict projection is the natural thing to write in a small FastAPI
    app, which is exactly what makes this worth a model and this paragraph.
    """

    id: int
    title: str
    body: str
    priority_code: str
    category_code: Optional[str]
    status: str
    assignee_id: Optional[int]
    internal_note: Optional[str]
    classification_code: Optional[str]
    opened_on: date
    due_on: Optional[date]


def _read_model(session: Session, user: Optional[Agent], ticket: Ticket) -> TicketRead:
    """Project a ticket for the caller.

    ``redact_view`` nulls the fields this viewer may not see. It is a no-op for
    fields with no policy rows, so this single call is the whole of the read-side
    enforcement — there is no per-field branching to keep in sync.
    """
    view = TicketRead.model_validate(ticket, from_attributes=True)
    return asas_access.redact_view(session, user, ENTITY, view, ticket)


def _get_or_404(session: Session, user: Optional[Agent], ticket_id: int) -> Ticket:
    """The single resolution point, and therefore the single MAC gate.

    Need-to-know is enforced here rather than in each handler, and it returns
    **404, not 403**: telling an unauthorized caller that a restricted ticket
    exists is itself the disclosure.
    """
    ticket = session.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found")
    if not asas_access.mac_allows(session, user, ENTITY, ticket):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "ticket not found")
    return ticket


@router.post("", status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreate,
    request: Request,
    session: Session = Depends(get_session),
    user: Optional[Agent] = Depends(get_current_user),
) -> TicketRead:
    # 1. Rate limit, keyed on the caller. Cheapest check first, and the one that
    #    should reject before any database work happens.
    asas_ratelimit.check(
        TICKET_CREATE.name, str(user.id if user else request.client.host)
    )

    # 2. The action verb. Not a role check — an unconfigured verb is admin-only,
    #    and the grants live in access_policy, not here.
    if user is not None and not asas_access.action_allowed(session, user, "ticket.create"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")

    changes = payload.model_dump(exclude_none=True)

    # 3. Semantic validation, against the **effective** record — not the payload.
    #
    #    This distinction is the whole trick on a create path. A rule is skipped
    #    when any value it reads is null, and `opened_on` is not in the payload:
    #    it comes from the model's default. Validating `changes` alone therefore
    #    silently skips every rule that reads it, and "due date before the
    #    opening date" would be accepted.
    #
    #    So construct first, validate against what the row will actually hold.
    ticket = Ticket(**changes)
    asas_validation.raise_if_invalid(
        ENTITY,
        None,
        {**changes, "opened_on": ticket.opened_on, "due_on": ticket.due_on},
    )

    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return _read_model(session, user, ticket)


@router.get("/{ticket_id}")
def read_ticket(
    ticket_id: int,
    session: Session = Depends(get_session),
    user: Optional[Agent] = Depends(get_current_user),
) -> TicketRead:
    return _read_model(session, user, _get_or_404(session, user, ticket_id))


@router.patch("/{ticket_id}")
def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    session: Session = Depends(get_session),
    user: Optional[Agent] = Depends(get_current_user),
) -> TicketRead:
    ticket = _get_or_404(session, user, ticket_id)
    changes = payload.model_dump(exclude_none=True)

    # Field-level edit rights. Note this checks only fields whose value actually
    # *changes* — resubmitting the current value is a no-op, not a violation, so
    # a client echoing back a whole record does not trip on a field it may not
    # edit but did not touch.
    forbidden = asas_access.forbidden_edits(session, user, ENTITY, ticket, changes)
    if forbidden:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, f"not permitted to edit: {sorted(forbidden)}"
        )

    asas_validation.raise_if_invalid(ENTITY, ticket, changes)

    for field, value in changes.items():
        setattr(ticket, field, value)
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return _read_model(session, user, ticket)


@router.post("/{ticket_id}/escalate")
def escalate_ticket(
    ticket_id: int,
    session: Session = Depends(get_session),
    user: Optional[Agent] = Depends(get_current_user),
) -> dict:
    """The composition, over HTTP.

    Three packages fire behind this one call — workflow opens the instance,
    access resolves who may approve it, notifications tells them — and the route
    knows about none of that beyond calling the host function that stitches
    them.
    """
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    if not asas_access.action_allowed(session, user, "ticket.escalate"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not permitted")

    ticket = _get_or_404(session, user, ticket_id)
    instance = workflow_wiring.request_escalation(session, ticket, user)
    return {"instance_id": instance.id, "status": str(instance.status)}


@router.get("")
def list_tickets(
    session: Session = Depends(get_session),
    user: Optional[Agent] = Depends(get_current_user),
) -> list[TicketRead]:
    """Note the MAC filter is applied to the *list*, not just the detail route.

    A count leaks as much as a row. Filtering only the detail endpoint is the
    classic half-fix: the list still tells you the restricted ticket exists.
    """
    rows = session.exec(select(Ticket).order_by(Ticket.id)).all()
    return [
        _read_model(session, user, t)
        for t in rows
        if asas_access.mac_allows(session, user, ENTITY, t)
    ]
