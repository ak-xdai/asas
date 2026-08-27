"""asas-ratelimit.

Contract row: **Host hooks** (``configure``).

Declared rules are process-local token buckets, so this is protection against a
runaway client, not a distributed quota. The reference host puts one on ticket
creation because that is the endpoint that writes rows and (in a real helpdesk)
sends mail.
"""

from __future__ import annotations

import asas_ratelimit

# name -> capacity per window. Kept tiny so the suite can actually exhaust it.
TICKET_CREATE = asas_ratelimit.Rule(
    name="ticket.create", limit=20, window_seconds=60
)


def configure() -> None:
    """Step 3 of the boot sequence."""
    asas_ratelimit.configure(enabled=True)
    asas_ratelimit.declare(TICKET_CREATE)
