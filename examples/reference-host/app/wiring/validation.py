"""asas-validation.

Contract row: **Routers** (``build_router``).

Table-less: the package owns no schema and needs no seeding, because the rules
are *code-declared data* rather than admin-tunable rows. They are developer
invariants — "a due date cannot precede the day the ticket was opened" is not
something a deployment should be able to switch off.

Adding a constraint is one ``Rule`` entry. It is never a scattered ``if``.

Two behaviours worth knowing before you write one:

- A rule only fires when the edit **touches** its fields. Submitting an
  unrelated change never trips an unrelated rule.
- A rule is **skipped when any value it reads is null**, so optional fields do
  not need null-guards written into every rule.

``build_router()`` serves the same rules to a frontend over ``GET
/validation/rules``, which is what keeps client-side pre-submit feedback from
drifting away from server-side enforcement — one source, fetched.
"""

from __future__ import annotations

import asas_validation

ENTITY = "ticket"

# Fields the engine may be asked about. Registered so a typo fails at boot.
TICKET_FIELDS = ("opened_on", "due_on", "created_at")

RULES = (
    asas_validation.Rule(
        entity=ENTITY,
        kind="order",
        fields=("opened_on", "due_on"),
        message="A ticket's due date cannot be before the day it was opened.",
        code="ticket.due_before_opened",
    ),
    asas_validation.Rule(
        entity=ENTITY,
        kind="not_future",
        fields=("opened_on",),
        message="A ticket cannot be opened in the future.",
        code="ticket.opened_in_future",
    ),
)


def configure() -> None:
    """Step 3 of the boot sequence.

    ``assert_rules_known`` is the loud half: it fails the boot if a rule names a
    field that was never registered, rather than leaving a rule that silently
    never fires.
    """
    asas_validation.register_fields(ENTITY, TICKET_FIELDS)
    asas_validation.declare_rules(RULES)
    asas_validation.assert_rules_known()
