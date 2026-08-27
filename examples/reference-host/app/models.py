"""The helpdesk domain: ``Agent`` and ``Ticket``.

Demonstrates: nothing on its own. This file exists so the other files have
something to act on, and it is deliberately the smallest domain that makes
every seam land naturally (design record 0030 §5c).

Two fields are load-bearing and would look arbitrary otherwise:

``Ticket.internal_note``
    The field-permission subject. Agents write candid notes here; the customer
    who raised the ticket must never see them. That restriction is a *seed row*
    in ``wiring/access.py``, never a check in a route handler.

``Ticket.classification_code``
    The MAC (need-to-know) stamp. Host-owned column, as the access package
    requires — the package owns the catalogs and the subject clearances, the
    host owns the column on its own table.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel

# Single-tenant: every row carries the same org. Kept explicit rather than
# omitted, because the column is what a multi-tenant host would start filtering
# on, and a reader porting this needs to see where it goes.
DEFAULT_ORG_ID = 1


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Agent(SQLModel, table=True):
    """A helpdesk agent. Also the ``user`` object the access package resolves
    principals against — see ``wiring/access.py``."""

    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(default=DEFAULT_ORG_ID, index=True)
    name: str
    email: str = Field(index=True, unique=True)
    # Account tier, in the access package's vocabulary: admin | member | viewer.
    role: str = Field(default="member")
    # Powers the `supervisor` relationship principal.
    supervisor_id: Optional[int] = Field(default=None, foreign_key="agent.id")


class Ticket(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    org_id: int = Field(default=DEFAULT_ORG_ID, index=True)

    title: str
    body: str = ""

    # Lookup-backed vocabulary. Codes are stable; labels are translations and
    # live in the lookup tables — never store a label here.
    priority_code: str = Field(default="normal")
    category_code: Optional[str] = Field(default=None)

    # open | escalated | resolved. A plain column: the workflow package governs
    # the *approval* of an escalation, not the ticket's own lifecycle.
    status: str = Field(default="open")

    assignee_id: Optional[int] = Field(default=None, foreign_key="agent.id", index=True)
    reporter_email: Optional[str] = Field(default=None)

    internal_note: Optional[str] = Field(default=None)
    classification_code: Optional[str] = Field(default=None, index=True)

    opened_on: date = Field(default_factory=lambda: _now().date())
    due_on: Optional[date] = Field(default=None)
    created_at: datetime = Field(default_factory=_now)


class TicketAttachment(SQLModel, table=True):
    """Storage's subject. The row holds the *key*, never the bytes — the file
    itself lives behind the storage seam (see ``wiring/storage.py``)."""

    id: Optional[int] = Field(default=None, primary_key=True)
    ticket_id: int = Field(foreign_key="ticket.id", index=True)
    filename: str
    storage_key: str
    created_at: datetime = Field(default_factory=_now)


class SlaNotice(SQLModel, table=True):
    """One row per ticket that has had its SLA breach announced.

    Exists for its **unique constraint**, not its data. The SLA sweep is an
    at-least-once job, so two runs can overlap when a lease is reclaimed; a
    read-then-write check ("has this already been notified?") is a race, because
    both runs can read *no* and then both write. Inserting this row first makes
    the database the arbiter: the loser gets an IntegrityError and skips.

    That is the shape worth copying. Idempotence is designed, and the cheapest
    correct design is usually a uniqueness constraint rather than a query.
    """

    ticket_id: int = Field(primary_key=True, foreign_key="ticket.id")
    notified_at: datetime = Field(default_factory=_now)
