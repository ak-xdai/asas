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


# --------------------------------------------------------------------------
# The optional tier: ranked deep-content search, Postgres only.
# --------------------------------------------------------------------------
#
# This module is the **dialect dispatch point**. The deep provider is registered
# only when the engine is Postgres; on SQLite the base provider above answers
# alone and the `/search` contract is byte-for-byte the same. That is the shape
# to copy — branch once, here, rather than scattering `if postgres` through the
# call sites.


def _extract_ticket(ticket: Ticket):
    """Turn a ticket into indexable documents.

    ``internal_note`` is absent, and must stay absent. The index is a write-time
    copy: once restricted text is in ``search_document`` there is no query-time
    redaction that can take it back out of a substring match.
    """
    from asas_search.fts import IndexDoc

    yield IndexDoc(
        entity_type=ENTITY,
        entity_id=ticket.id,
        source="ticket_body",
        source_id=ticket.id,
        content=ticket.body or "",
        context=ticket.title,
    )


def _resolve(session, user, ids: list) -> dict:
    """Matched ids -> (title, subtitle, url_path), for the hits that survive."""
    rows = session.exec(select(Ticket).where(Ticket.id.in_(ids))).all()
    return {
        t.id: (t.title, f"{t.status} · {t.priority_code}", f"/tickets/{t.id}")
        for t in rows
    }


def _org_of(session, user):
    """Single-tenant: no org scoping on the index."""
    return None


def _row_filter(session, user, entity_type: str, entity_id: int) -> bool:
    """Visibility, still at query time — even in the deep tier.

    The index does not know about clearances, and deliberately so: baking
    visibility into it would mean re-indexing every record whenever anyone's
    clearance changed.
    """
    ticket = session.get(Ticket, entity_id)
    return ticket is not None and asas_access.mac_allows(session, user, ENTITY, ticket)


def configure(engine=None) -> None:
    """Step 4 of the boot sequence.

    ``engine`` is optional so a caller with no bind still gets the portable
    tier; passing it is what enables the Postgres-only half.
    """
    search.register_provider(ENTITY, _ticket_provider)

    if engine is None or not search.fts.is_postgres(engine):
        return

    search.fts.register_extractor("ticket_body", _extract_ticket)
    search.register_provider(
        ENTITY,
        search.fts.make_provider(
            ENTITY, resolver=_resolve, org_of=_org_of, row_filter=_row_filter
        ),
    )
