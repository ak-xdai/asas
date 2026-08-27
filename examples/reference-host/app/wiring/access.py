"""asas-access.

Contract rows: **Schema** (``migrate``), **Seeding**
(``seed_field_permissions``, ``seed_action_permissions``,
``ensure_system_groups``, ``ensure_clearance_levels``).

The idea worth taking away: **restricting a field is a row, not a branch.**
``Ticket.internal_note`` is hidden from the customer by a seed row below, not by
an ``if`` in the ticket router. Adding a restricted field later is a row and a
backfill; it is never a new guard scattered through handlers.

Two things about the semantics that surprise people:

- **Safe by default.** A field with *no* rows keeps the caller's baseline rule.
  Rows switch that field to an explicit allowlist. So seeding one row for one
  field does not lock down the other fields — it locks down that one.
- **admin is an implicit floor.** You cannot lock an admin out by configuration,
  which is why no default below bothers granting to admin.

Registration vs seeding is the other distinction: ``register_*`` declares what
*exists* (validated at boot, so a typo fails loudly), while ``seed_*`` writes
the *policy rows*. Registration happens every boot; seeding is idempotent but
its rows are then owned by the deployment, which is why changing a shipped
default is a backfill migration rather than a seed edit.
"""

from __future__ import annotations

import asas_access
from sqlmodel import Session

from ..models import DEFAULT_ORG_ID, Ticket

ENTITY = "ticket"

# Every field the access engine may be asked about. A verb or field that is not
# registered fails loudly at boot rather than silently allowing everything.
TICKET_FIELDS = (
    "title",
    "body",
    "priority_code",
    "category_code",
    "status",
    "assignee_id",
    "internal_note",
    "classification_code",
    "due_on",
)

# Org-wide action gates. Cataloged verbs, not role checks.
ACTION_VERBS = (
    "ticket.create",
    "ticket.escalate",
    "ticket.classify",
)

# (entity, field, action, principal) — one row per *allowed* grant.
#
# Only `internal_note` appears here, and that is the point: every other field
# keeps its baseline rule because it has no rows at all.
FIELD_DEFAULTS: tuple[tuple[str, str, str, str], ...] = (
    (ENTITY, "internal_note", "view", asas_access.ROLE_MEMBER),
    (ENTITY, "internal_note", "edit", asas_access.ROLE_MEMBER),
    # The assigned agent, whoever they are — a *relationship*, resolved per
    # (user, record) by the resolver below. This is the case a role alone
    # cannot express.
    (ENTITY, "internal_note", "view", "ticket_assignee"),
    (ENTITY, "internal_note", "edit", "ticket_assignee"),
)

# (verb, principal).
ACTION_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("ticket.create", asas_access.ROLE_MEMBER),
    ("ticket.create", asas_access.ROLE_VIEWER),
    ("ticket.escalate", asas_access.ROLE_MEMBER),
    # ticket.classify is deliberately absent: an unconfigured verb is
    # admin-only, which is the safe default and worth seeing at least once.
)

SYSTEM_GROUPS = {"support_leads": "Support leads"}

# Need-to-know ladder: code -> (label, rank). Higher rank sees lower.
#
# MAC is a *separate* axis from the field and action permissions above, and the
# difference that catches people: **there is no admin floor in MAC**. An admin
# without the clearance does not see the record. That is the whole point of a
# need-to-know layer, and it is why classification lives here rather than being
# folded into the role model.
CLEARANCE_LEVELS: dict[str, tuple[str, int]] = {
    "public": ("Public", 0),
    "internal": ("Internal", 10),
    "restricted": ("Restricted", 20),
}


def _is_assignee(session: Session, user, record) -> bool:
    """Relationship principal: does this user hold the ticket?

    The signature is fixed by the package — (session, user, record) -> bool —
    and the host supplies the meaning. This is the seam that makes "the agent
    who owns the ticket, and nobody else" expressible as configuration.
    """
    if user is None or not isinstance(record, Ticket):
        return False
    return record.assignee_id is not None and record.assignee_id == user.id


def configure() -> None:
    """Step 3 of the boot sequence: declare what exists.

    Registration is separate from seeding on purpose — it runs every boot and
    validates the seed data, so a renamed field breaks the boot rather than
    quietly leaving a dead permission row behind.
    """
    asas_access.register_fields(ENTITY, TICKET_FIELDS)
    asas_access.register_actions(ACTION_VERBS)
    asas_access.register_resolver(ENTITY, "ticket_assignee", _is_assignee)
    # The MAC half. The host owns the ``classification_code`` column on its own
    # table; the package owns the catalogs, the subject clearances, and the
    # ``mac_allows`` decision.
    asas_access.register_classified_entity(ENTITY)


def seed(session: Session) -> None:
    """Step 4: write the policy rows.

    Idempotent, but note the deeper rule: once a deployment owns these rows,
    changing a *shipped default* means a keyed backfill migration. Re-seeding a
    changed default would resurrect grants an operator deliberately deleted.
    """
    asas_access.seed_field_permissions(session, FIELD_DEFAULTS)
    asas_access.seed_action_permissions(session, ACTION_DEFAULTS)
    asas_access.ensure_system_groups(session, DEFAULT_ORG_ID, SYSTEM_GROUPS)
    asas_access.ensure_clearance_levels(session, DEFAULT_ORG_ID, CLEARANCE_LEVELS)
    session.commit()
