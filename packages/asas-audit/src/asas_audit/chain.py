"""Hash-chain primitives. Pure: no database, no session, unit-testable.

Each entry carries the fingerprint of the one before it, so the log is a chain
and any alteration to a link shows up as a divergence from that point on. Three
kinds of tampering are detectable and all three matter: an entry that was
**edited**, an entry that was **deleted**, and an entry **inserted** after the
fact to look original.

**The writer and the verifier share one canonical encoding, which is why they
live in one file.** Two encodings is how a chain reports breaks that are not
there, and a tamper-evidence tool that cries wolf gets switched off, which is
strictly worse than not having one. So this module owns the exact dict that gets
hashed (:func:`chain_payload`), the exact bytes it becomes
(:func:`canonical_bytes`), and both sides of the arithmetic. Nothing else in the
package, and nothing in a host, may compose either.

    hash_current = sha256(hash_prev || canonical_json(chain_payload))

On verify the payload is rebuilt from the stored columns, so a row that was
changed in place no longer hashes to what it says it does.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional, Sequence


def normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """One round trip through the canonical encoding, keys stringified first.

    ``json.dumps(sort_keys=True)`` cannot even ORDER mixed str/int keys (it
    raises comparing them), and the JSON column will stringify keys on storage
    anyway — so append() normalises up front and stores exactly what it
    hashed, making the write-side and read-side bytes identical by
    construction."""
    def _str_keys(value):
        if isinstance(value, dict):
            return {str(k): _str_keys(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_str_keys(v) for v in value]
        return value

    return json.loads(canonical_bytes(_str_keys(payload)))


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    """The one encoding, used by both the writer and the verifier.

    ``sort_keys`` because a dict's insertion order is not part of its meaning and
    two processes must not encode the same facts differently. Tight separators so
    the bytes do not depend on a formatting default. ``default=str`` so a value
    the host put in the payload cannot make the hash undefined; it is a fallback,
    not a licence to store exotic objects.
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode()


def canonical_timestamp(value: datetime) -> str:
    """The one spelling of a moment, and it has to be one spelling.

    **Found by running the suite on both engines.** The hash covers
    ``occurred_at``, so the writer's rendering of it and the verifier's rendering
    of the value read back must be identical, and they were not: Postgres returns
    a ``timestamptz`` as an aware datetime, SQLite has no timezone type at all and
    returns a naive one. The same row therefore hashed one way on write and
    another on verify, and the whole chain reported as tampered with on SQLite
    while passing on Postgres.

    A naive value is read as UTC rather than as local time, which is the only
    choice that is stable: local time depends on the process, so the same row
    would hash differently on two machines, and it is what SQLite gives back for
    a value that was UTC when it went in.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def chain_payload(
    *,
    event_id: Any,
    org_id: Any,
    actor: str,
    action: str,
    resource_type: str,
    resource_id: Any,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> dict[str, Any]:
    """Exactly what gets hashed. The single source of truth for the encoding.

    Every field is stringified rather than left to the JSON encoder's own idea of
    an int or a UUID, so a host that keys tenants by integer and one that keys
    them by UUID produce the same shape, and a column type change does not
    invalidate an existing chain.

    ``occurred_at`` goes through :func:`canonical_timestamp` rather than straight
    to ``isoformat``, for the reason that function explains.

    Adding a field here **breaks every existing chain**, because the rebuilt
    payload of an old row would no longer match its stored hash. If a field ever
    has to join it, that is a versioned migration of the chain and not an edit to
    this function.
    """
    return {
        "id": str(event_id),
        "org_id": str(org_id),
        "actor": actor,
        "action": action,
        "resource_type": resource_type,
        "resource_id": str(resource_id),
        "payload": payload,
        "occurred_at": canonical_timestamp(occurred_at),
    }


def compute_hash(hash_prev: Optional[bytes], payload: dict[str, Any]) -> bytes:
    """The link. ``hash_prev`` is ``None`` for the first entry in a chain."""
    return hashlib.sha256((hash_prev or b"") + canonical_bytes(payload)).digest()


@dataclass(frozen=True)
class ChainBreak:
    """One divergence, with both hashes so a reader can see it rather than take
    it on trust."""

    event_id: Any
    seq: int
    action: str
    expected_hash_hex: str
    stored_hash_hex: str
    #: Why this row diverged, in words. A break at the FIRST bad row is the
    #: interesting one; the rows after it usually diverge as a consequence.
    detail: str = ""


@dataclass(frozen=True)
class VerifyReport:
    """The verdict for one tenant's chain."""

    org_id: Any
    events_checked: int
    breaks: tuple[ChainBreak, ...]

    @property
    def is_intact(self) -> bool:
        return not self.breaks

    @property
    def first_break(self) -> Optional[ChainBreak]:
        """The row where the history stops adding up, which is the one to look at.

        Everything after a break usually diverges too, as a consequence rather
        than as separate evidence, so a report of forty breaks is normally one
        alteration and thirty-nine echoes.
        """
        return self.breaks[0] if self.breaks else None


def verify_rows(org_id: Any, rows) -> VerifyReport:
    """Re-derive the chain over ``rows``, which must be ordered by ``seq`` ascending.

    ``rows`` is any ITERABLE (a list, or a streamed result) of anything with the stored columns as attributes (the package's own
    ORM row, a plain namedtuple, a test double). Keeping it structural is what
    makes this function testable without a database, which in turn is what makes
    the tamper cases cheap to assert.

    Two checks per row, and both are needed:

    * the stored ``hash_prev`` must equal the previous row's ``hash_current``,
      which is what catches a **deleted** row (the survivors' links no longer
      meet) and an **inserted** one;
    * the stored ``hash_current`` must equal the hash recomputed from the row's
      own columns, which is what catches an **edited** row.
    """
    breaks: list[ChainBreak] = []
    prev_hash: Optional[bytes] = None
    checked = 0
    for row in rows:
        expected = compute_hash(
            prev_hash,
            chain_payload(
                event_id=row.id,
                org_id=org_id,
                actor=row.actor,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                payload=row.payload,
                occurred_at=row.occurred_at,
            ),
        )
        stored = bytes(row.hash_current)
        stored_prev = bytes(row.hash_prev) if row.hash_prev is not None else None

        if stored_prev != prev_hash:
            breaks.append(
                _break(row, expected, stored,
                       "does not link to the previous entry: a row was deleted, "
                       "inserted, or reordered")
            )
        elif expected != stored:
            breaks.append(
                _break(row, expected, stored,
                       "its own columns no longer hash to its stored fingerprint: "
                       "the row was edited")
            )
        # Chain off what is STORED rather than what was expected, so one bad row
        # does not necessarily invalidate every row after it: a single edited
        # entry then reports as one break rather than as a cascade.
        prev_hash = stored
        checked += 1
    return VerifyReport(org_id=org_id, events_checked=checked, breaks=tuple(breaks))


def _break(row: Any, expected: bytes, stored: bytes, detail: str) -> ChainBreak:
    return ChainBreak(
        event_id=row.id,
        seq=int(row.seq),
        action=row.action,
        expected_hash_hex=expected.hex(),
        stored_hash_hex=stored.hex(),
        detail=detail,
    )
