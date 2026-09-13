"""Asas audit — an unfalsifiable record of who did what.

Every business action becomes a permanent entry: who acted, what they did, which
record, when, and the details. Each entry carries the fingerprint of the one
before it, so the log is a chain and any alteration shows up as a divergence at
the row where it happened. Editing a row, deleting one, and inserting one after
the fact are all detectable, and all three matter.

Two properties are worth stating up front, because they are what separate this
from a log file:

* **The entry commits with the change it describes.** :func:`append` puts the row
  on the caller's own session and does not commit, so one transaction decides the
  fate of both. There is no state in which the change happened and the record did
  not, or the reverse.
* **Edits and deletes are refused by the database**, not merely absent from this
  package's code. A trigger rejects both for anyone, which is the layer that
  survives a compromised application or a direct connection.

The part that is genuinely hard is concurrency, and it is worth reading
:mod:`asas_audit.service` before changing anything there. Two simultaneous
appends must not chain off the same tail, and the obvious ``FOR UPDATE`` on the
tail row does not prevent it: it forks the chain, silently, and surfaces later as
a verification break that looks like tampering over data nobody touched. A
per-tenant advisory lock taken **before** the tail read is what works.

Host contract surface (table-owning + router variant):
:func:`migrate` (package Alembic chain, adopt-or-create), :func:`build_router`,
and the service functions below. No ``seed``: an audit log with seeded rows would
be a record of things that did not happen.

Depends on ``asas-tenancy`` so that this table and the host's own are protected by
one definition of the policy rather than two that can drift. That is the first
inter-package dependency in the family, and a deliberate one: this is the table
where cross-tenant visibility would be worst, and a second copy of the policy SQL
is exactly the drift the family's parity tests exist to prevent.
"""

from asas_audit.chain import (
    ChainBreak,
    VerifyReport,
    canonical_bytes,
    chain_payload,
    compute_hash,
    verify_rows,
)
from asas_audit.migrate import migrate
from asas_audit.models import AuditEvent
from asas_audit.router import build_router
from asas_audit.service import append, history, verify

__version__ = "0.1.0"

__all__ = [
    "append",
    "AuditEvent",
    "build_router",
    "canonical_bytes",
    "chain_payload",
    "ChainBreak",
    "compute_hash",
    "history",
    "migrate",
    "verify",
    "verify_rows",
    "VerifyReport",
    "__version__",
]
