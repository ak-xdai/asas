"""asas-lookups.

Contract rows: **Routers** (``build_routers``), **Schema** (``migrate``),
**Seeding** (``seed``), **Host hooks** (``configure_org_resolver``). This is the
only package that exercises all four, which is why it reads as the fullest
example of the shape.

The one trap: ``seed(session)`` seeds *standards-based* vocabulary only —
salutation, gender, marital status, currency, country, nationality. Your
product's own words are yours to seed, with ``ensure_type`` / ``ensure_value``.
A host that calls ``seed()`` and expects its own ticket priorities to appear
gets an empty dropdown and no error.
"""

from __future__ import annotations

from typing import Optional

import asas_lookups
from sqlmodel import Session

from ..models import DEFAULT_ORG_ID

# The host's own vocabulary: type key -> (display name, [(code, English label)]).
#
# ``key`` is the type's stable identifier and ``name`` its human label. Note the
# vocabulary here is all *codes* — "high", "hardware". Labels are translations
# and live in the lookup tables, so renaming "Hardware" to "Devices" is a label
# edit and touches no ticket row. Never store a label in ``Ticket.priority_code``.
TICKET_VOCABULARY: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "ticket_priority": (
        "Ticket priority",
        [
            ("low", "Low"),
            ("normal", "Normal"),
            ("high", "High"),
            ("urgent", "Urgent"),
        ],
    ),
    "ticket_category": (
        "Ticket category",
        [
            ("access", "Access request"),
            ("hardware", "Hardware"),
            ("software", "Software"),
            ("other", "Other"),
        ],
    ),
}


def _org_resolver(session: Session) -> Optional[int]:
    """Tenancy is a host concept — the package never guesses it.

    Single-tenant, so this returns a constant. A multi-tenant host would read
    the org off its request-scoped context here, and that is the *only* change
    needed: nothing downstream in the package has to be told about tenancy.
    Returning ``None`` instead would also be valid and means "no org scoping at
    all"; a constant is used here so the column is visibly populated.
    """
    return DEFAULT_ORG_ID


def configure() -> None:
    """Step 3 of the boot sequence. Hooks before seeds, because seeding writes
    rows the resolver decides the org of."""
    asas_lookups.configure_org_resolver(_org_resolver)


def seed(session: Session) -> None:
    """Step 4. Idempotent, and safe to run on every boot."""
    # Standards-based vocabulary, shipped by the package.
    asas_lookups.seed(session)

    # The host's own. Note that this is the part `seed()` does not do.
    #
    # ``ensure_type`` takes **kwargs that are passed straight to the LookupType
    # model, so the accepted names are not visible in its signature: ``key`` is
    # the identifier (not ``code``), ``name`` the label. ``is_open`` left at its
    # default keeps these closed lists — an open type is one callers may extend
    # at runtime, the way a free-text skill list works.
    for type_key, (type_name, values) in TICKET_VOCABULARY.items():
        lookup_type = asas_lookups.ensure_type(session, key=type_key, name=type_name)
        for order, (code, label) in enumerate(values):
            asas_lookups.ensure_value(
                session,
                lookup_type.id,
                code,
                [("en", label)],
                sort_order=order,
            )
    session.commit()
