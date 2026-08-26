"""asas-search.

Contract rows: **Schema** (``migrate``).

Also the second half of the **classified-record composition**: MAC decides who
may see a ticket, and search must never be the hole in that. The rule:

    **Never index a restricted or redactable field.**

The index is a write-time *copy*. Redaction cannot be applied to it afterwards,
because by then the restricted text is already sitting in a searchable column
where a substring match can surface it. So ``internal_note`` is absent from the
provider below, permanently, and no configuration can put it back.

Visibility stays **query-time**: the provider filters by what the *caller* may
see, on every search. Baking visibility into the index would mean re-indexing
the world every time a clearance changed.

Tiering: this host registers the portable ``ilike`` provider on every engine.
The deep, ranked, Postgres-only tier is additive — the ``/search`` contract is
identical on SQLite, only the ranking is poorer. That is what graceful
degradation means here.
"""

from __future__ import annotations

import asas_access
import asas_search as search
from sqlmodel import select

from ..models import Ticket

ENTITY = "ticket"


def _ticket_provider(session, user, q: str, lang: str, limit: int) -> list:
    """Portable ticket search.

    Note what is searched: title and body. Not ``internal_note`` — see the
    module docstring — and the MAC filter runs on every hit before it is
    returned, rather than being pre-computed into the index.
    """
    pattern = f"%{q}%"
    rows = session.exec(
        select(Ticket)
        .where((Ticket.title.ilike(pattern)) | (Ticket.body.ilike(pattern)))
        .limit(limit * 4)  # over-fetch: the MAC filter below removes some
    ).all()

    hits = []
    for ticket in rows:
        # Query-time need-to-know. A classified ticket the caller cannot reach
        # is not a redacted hit — it is not a hit at all.
        if not asas_access.mac_allows(session, user, ENTITY, ticket):
            continue
        hits.append(
            search.SearchHit(
                entity_type=ENTITY,
                id=ticket.id,
                title=ticket.title,
                subtitle=f"{ticket.status} · {ticket.priority_code}",
                # The host supplies the link; the engine never invents routes.
                url_path=f"/tickets/{ticket.id}",
                # Lower tiers rank higher. `title_tier` gives a title match its
                # standard tier, so a ticket whose title starts with the query
                # outranks one that merely mentions it in the body.
                rank_tier=search.title_tier(ticket.title, q),
            )
        )
        if len(hits) >= limit:
            break
    return hits


def configure() -> None:
    """Step 4 of the boot sequence."""
    search.register_provider(ENTITY, _ticket_provider)
