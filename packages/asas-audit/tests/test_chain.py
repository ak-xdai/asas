"""The chain arithmetic and the three kinds of tampering. No database.

These are pure because the encoding is pure, which is what makes the tamper cases
cheap enough to assert exhaustively. The database-level guarantees are in
``test_append_only.py`` and ``test_concurrency.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Optional

from asas_audit import chain


@dataclass
class Row:
    """A stored row's shape, structurally. ``verify_rows`` reads attributes, so a
    test double needs no ORM and no session."""

    id: str
    seq: int
    actor: str
    action: str
    resource_type: str
    resource_id: str
    payload: dict
    occurred_at: datetime
    hash_prev: Optional[bytes]
    hash_current: bytes


ORG = "org-a"
WHEN = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


def build(n: int) -> list[Row]:
    """A well-formed chain of ``n`` entries, hashed exactly as the writer would."""
    rows: list[Row] = []
    prev: Optional[bytes] = None
    for i in range(1, n + 1):
        row = Row(
            id=f"id-{i}", seq=i, actor="alice", action=f"thing.{i}",
            resource_type="thing", resource_id=str(i), payload={"n": i},
            occurred_at=WHEN, hash_prev=prev, hash_current=b"",
        )
        row.hash_current = chain.compute_hash(
            prev,
            chain.chain_payload(
                event_id=row.id, org_id=ORG, actor=row.actor, action=row.action,
                resource_type=row.resource_type, resource_id=row.resource_id,
                payload=row.payload, occurred_at=row.occurred_at,
            ),
        )
        prev = row.hash_current
        rows.append(row)
    return rows


def test_an_intact_chain_verifies():
    report = chain.verify_rows(ORG, build(5))
    assert report.is_intact
    assert report.events_checked == 5
    assert report.first_break is None


def test_an_empty_chain_is_intact():
    """A tenant that has done nothing yet is not a tenant whose history is
    broken."""
    assert chain.verify_rows(ORG, []).is_intact


def test_an_edited_row_is_detected_at_that_row():
    """Tamper one: a value was changed in place. Its own columns no longer hash to
    its stored fingerprint."""
    rows = build(5)
    rows[2].payload = {"n": "rewritten"}
    report = chain.verify_rows(ORG, rows)
    assert not report.is_intact
    assert report.first_break.seq == 3
    assert "edited" in report.first_break.detail


def test_an_edited_row_reports_once_not_as_a_cascade():
    """Chaining off what is STORED rather than off what was expected is what
    keeps one alteration from reporting as one break plus a long tail of
    consequences. Forty breaks would bury the row that matters."""
    rows = build(10)
    rows[4].actor = "mallory"
    report = chain.verify_rows(ORG, rows)
    assert len(report.breaks) == 1
    assert report.first_break.seq == 5


def test_a_deleted_row_is_detected():
    """Tamper two: an entry was removed, so the survivors' links no longer meet.
    Nothing about the remaining rows is wrong in itself, which is why the
    ``hash_prev`` check has to exist separately from the recompute."""
    rows = build(5)
    del rows[2]
    report = chain.verify_rows(ORG, rows)
    assert not report.is_intact
    assert report.first_break.seq == 4
    assert "deleted" in report.first_break.detail


def test_an_inserted_row_is_detected():
    """Tamper three: an entry was added after the fact to look original. It has
    no valid link to what precedes it, and it cannot be given one without the
    previous row's fingerprint."""
    rows = build(4)
    forged = Row(
        id="id-forged", seq=3, actor="mallory", action="approval.granted",
        resource_type="thing", resource_id="9", payload={}, occurred_at=WHEN,
        hash_prev=rows[1].hash_current, hash_current=b"\x00" * 32,
    )
    rows.insert(2, forged)
    report = chain.verify_rows(ORG, rows)
    assert not report.is_intact
    assert report.first_break.seq == 3


def test_reordering_is_detected():
    rows = build(5)
    rows[1], rows[2] = rows[2], rows[1]
    assert not chain.verify_rows(ORG, rows).is_intact


def test_a_break_carries_both_hashes():
    """So a reader can see the divergence rather than take the verdict on
    trust."""
    rows = build(3)
    rows[1].action = "changed"
    b = chain.verify_rows(ORG, rows).first_break
    assert b.expected_hash_hex != b.stored_hash_hex
    assert len(b.expected_hash_hex) == 64


def test_the_encoding_is_key_order_independent():
    """Two processes must not encode the same facts differently, or the chain
    breaks for reasons that have nothing to do with tampering."""
    a = chain.canonical_bytes({"x": 1, "y": 2})
    b = chain.canonical_bytes({"y": 2, "x": 1})
    assert a == b


def test_identity_is_stringified_so_int_and_uuid_hosts_agree():
    """The payload stringifies every id, which is what lets a UUID-keyed host and
    an integer-keyed one share this package instead of forking it, and what makes
    a later column type change harmless to an existing chain."""
    import uuid as _uuid

    ident = _uuid.uuid4()
    as_uuid = chain.chain_payload(
        event_id=ident, org_id=1, actor="a", action="b", resource_type="t",
        resource_id=7, payload={}, occurred_at=WHEN,
    )
    as_text = chain.chain_payload(
        event_id=str(ident), org_id="1", actor="a", action="b", resource_type="t",
        resource_id="7", payload={}, occurred_at=WHEN,
    )
    assert as_uuid == as_text


def test_the_chain_payload_shape_is_pinned():
    """Adding a field to the payload invalidates every existing chain, because an
    old row's rebuilt payload would no longer match its stored hash. That is a
    versioned migration of the chain, never an edit to the function, so the field
    set is asserted here to make the decision explicit."""
    keys = set(chain.chain_payload(
        event_id="i", org_id="o", actor="a", action="b", resource_type="t",
        resource_id="r", payload={}, occurred_at=WHEN,
    ))
    assert keys == {
        "id", "org_id", "actor", "action", "resource_type", "resource_id",
        "payload", "occurred_at",
    }
