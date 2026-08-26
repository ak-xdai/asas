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
    due_on: Optional[str] = None
    assignee_id: Optional[int] = None


class TicketUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    priority_code: Optional[str] = None
    status: Optional[str] = None
    internal_note: Optional[str] = None
    classification_code: Optional[str] = None
    due_on: Optional[str] = None


def _read_model(session: Session, user: Optional[Agent], ticket: Ticket) -> dict:
    """Project a ticket for the caller.

    ``redact_view`` nulls the fields this viewer may not see. It is a no-op for
    fields with no policy rows, so this single call is the whole of the read-side
    enforcement — there is no per-field branching to keep in sync.
    """
    data = {
        "id": ticket.id,
        "title": ticket.title,
        "body": ticket.body,
        "priority_code": ticket.priority_code,
        "category_code": ticket.category_code,
        "status": ticket.status,
        "assignee_id": ticket.assignee_id,
        "internal_note": ticket.internal_note,
        "classification_code": ticket.classification_code,
        "opened_on": str(ticket.opened_on),
        "due_on": str(ticket.due_on) if ticket.due_on else None,
    }
    return asas_access.redact_view(session, user, ENTITY, data, ticket)


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
) -> dict:
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

    # 3. Semantic validation, against the *resulting* record. Raises FastAPI's
    #    native 422 so one client-side mapper handles Pydantic and rule
    #    violations alike.
    asas_validation.raise_if_invalid(ENTITY, None, changes)

    ticket = Ticket(**changes)
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return _read_model(session, user, ticket)


@router.get("/{ticket_id}")
def read_ticket(
    ticket_id: int,
    session: Session = Depends(get_session),
    user: Optional[Agent] = Depends(get_current_user),
) -> dict:
    return _read_model(session, user, _get_or_404(session, user, ticket_id))


@router.patch("/{ticket_id}")
def update_ticket(
    ticket_id: int,
    payload: TicketUpdate,
    session: Session = Depends(get_session),
    user: Optional[Agent] = Depends(get_current_user),
) -> dict:
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
) -> list[dict]:
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
