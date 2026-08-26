"""asas-jobs.

Contract rows: **Schema** (``migrate``), **Seeding** (``ensure_schedule``),
**Host hooks** (``configure_context_binder``, ``configure_runner``).

Also one third of the **async-notification composition**: jobs supplies the
*when*, notifications the *what*. Neither package imports the other — the host
is the only place they meet, which is exactly why no per-package README can
teach this.

**Delivery is at-least-once, so handlers must be idempotent.** That is not a
caveat, it is the design: crash recovery re-runs a job whose lease expired, and
a handler that cannot tolerate a second run will corrupt data the first time a
process dies mid-work.

The rule this replaces: any new async or periodic work is a handler here plus an
``enqueue`` or ``ensure_schedule`` call. Never a ``BackgroundTasks``, never an
``after_commit`` hook, never a sweep at boot — those all lose the work when the
process dies, which is the failure this package exists to remove.
"""

from __future__ import annotations

from datetime import date

import asas_jobs as jobs
import asas_notifications as notifications
from sqlmodel import Session, select

from ..models import DEFAULT_ORG_ID, Ticket
from .notifications import KIND_SLA_BREACHED

KIND_SLA_SWEEP = "tickets.sla_sweep"

# Deliberately short so the suite can observe a tick without waiting.
SLA_SWEEP_SECONDS = 300


def _context_binder(org_id, **_kwargs) -> None:
    """Bind the job's tenant context before its handler runs.

    The queue tables are tenancy-**global** — a job is claimed before any context
    exists, so the runner cannot be inside one. It therefore binds the context
    for each job from the row's own ``org_id``, and that is what this hook does.
    Single-tenant here, so there is nothing to bind and the body is empty; the
    hook is still installed, because the *shape* is the thing worth showing.
    """
    return None


def _sla_sweep(session: Session, payload: dict | None = None, **_kwargs) -> None:
    """Notify on tickets past their due date.

    Idempotent by construction: it only notifies tickets that are still open and
    past due, and the notification kind coalesces, so a re-run after a crash
    does not produce a second inbox row. Idempotence here is a property of the
    query, not a flag — which is the durable way to get it.
    """
    overdue = session.exec(
        select(Ticket).where(
            Ticket.status == "open",
            Ticket.due_on.is_not(None),
            Ticket.due_on < date.today(),
        )
    ).all()

    for ticket in overdue:
        if ticket.assignee_id is None:
            continue
        notifications.notify(
            session,
            [ticket.assignee_id],
            KIND_SLA_BREACHED,
            title=f"Ticket #{ticket.id} is past its due date",
            entity_type="ticket",
            entity_id=ticket.id,
            # The record goes in so the recipient filter can apply need-to-know
            # before the row is written. Omitting it is the silent way to leak a
            # classified ticket's title into an inbox.
            record=ticket,
        )
    session.commit()


def configure(session_factory) -> None:
    """Step 4 of the boot sequence.

    ``poll_seconds=0`` leaves the runner inert so a test drives it with
    ``run_once()``. A web process would pass a real interval here, and moving to
    a dedicated worker later is this one number — not an architecture change.
    """
    jobs.configure_context_binder(_context_binder)
    jobs.configure_runner(session_factory, poll_seconds=0, lease_seconds=60)
    jobs.register_handler(KIND_SLA_SWEEP, _sla_sweep)


def seed(session: Session) -> None:
    """Step 5. Idempotent: re-running does not create a second schedule."""
    jobs.ensure_schedule(
        session,
        KIND_SLA_SWEEP,
        every_seconds=SLA_SWEEP_SECONDS,
        org_id=DEFAULT_ORG_ID,
    )
    session.commit()
