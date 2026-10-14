"""Many appends at once must leave one chain, not several.

**This is the test the package exists for.** The concurrency bug it guards
against is not a crash and not a slowdown: two simultaneous appends chain off the
same tail, the history forks, nothing fails at the time, and it surfaces weeks
later as a verification break that looks exactly like tampering over data nobody
touched. By then there is no way to tell which branch is the real history.

Postgres only. SQLite serialises writers at the database, so it has the same
property by a different mechanism and there is nothing here for it to prove.
"""

from __future__ import annotations

import threading

import pytest
from sqlmodel import Session

import asas_audit
import asas_tenancy
from asas_audit import service
from conftest import ORG_A, ORG_B

WRITERS = 8


def _append_concurrently(engine, org_id, writers=WRITERS, barrier=None):
    """Fire ``writers`` appends as simultaneously as threads allow.

    The barrier is what makes this a real test rather than a hopeful one: without
    it the threads start staggered by their own setup and may never overlap at
    the tail read, which is the exact instant the race lives in.
    """
    errors: list[BaseException] = []
    gate = barrier or threading.Barrier(writers)

    def write(n: int) -> None:
        try:
            with Session(engine) as s:
                asas_tenancy.set_tenant_guc(s, org_id)
                gate.wait(timeout=10)
                asas_audit.append(
                    s, org_id=org_id, actor=f"writer-{n}", action="thing.done",
                    resource_type="thing", resource_id=str(n),
                )
                s.commit()
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return errors


def test_concurrent_appends_leave_the_chain_intact(requires_postgres):
    """Eight writers, one tail, one chain.

    Without the per-tenant advisory lock taken BEFORE the tail read, several of
    these read the same newest row and hash off it, so the chain forks and
    ``verify`` reports breaks. The lock is what makes this pass.
    """
    engine = requires_postgres
    errors = _append_concurrently(engine, ORG_A)
    assert not errors, f"appends raised: {errors}"

    with Session(engine) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        report = asas_audit.verify(s, ORG_A)

    assert report.events_checked == WRITERS
    assert report.is_intact, (
        f"the chain forked under concurrency: {report.first_break}"
    )


def test_without_the_lock_the_chain_really_does_fork(requires_postgres, monkeypatch):
    """The lock is load-bearing, demonstrated rather than asserted in a comment.

    Disabling it and running the same eight writers produces a chain that does
    not verify. This is a race, so a run that happens not to overlap proves
    nothing and skips rather than failing: the assertion that matters is the test
    above, and this one exists so a future reader can see why the lock is there.
    """
    engine = requires_postgres
    monkeypatch.setattr(service, "_lock_chain", lambda session, org_id: None)

    errors = _append_concurrently(engine, ORG_A)
    with Session(engine) as s:
        asas_tenancy.set_tenant_guc(s, ORG_A)
        report = asas_audit.verify(s, ORG_A)

    if report.is_intact and not errors:
        pytest.skip(
            "the writers did not actually overlap this run, so the race did not "
            "fire; the guarantee is asserted by the locked test above"
        )
    # Either the chain forked, or a unique-constraint style error surfaced. Both
    # are the damage the lock prevents.
    assert errors or not report.is_intact


def test_one_tenants_appends_do_not_serialise_another(requires_postgres):
    """The lock is per tenant, so a busy tenant cannot make a quiet one wait.

    Asserted structurally: two tenants' writers share one barrier, so every
    thread is inside its append at the same moment. A single global lock would
    deadlock on the barrier and time out; a per-tenant one lets both chains
    proceed.
    """
    engine = requires_postgres
    gate = threading.Barrier(WRITERS)
    errors: list[BaseException] = []

    def run(org):
        errors.extend(_append_concurrently(engine, org, writers=WRITERS // 2,
                                           barrier=gate))

    threads = [threading.Thread(target=run, args=(o,)) for o in (ORG_A, ORG_B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"cross-tenant appends interfered: {errors}"
    with Session(engine) as s:
        for org in (ORG_A, ORG_B):
            asas_tenancy.set_tenant_guc(s, org)
            report = asas_audit.verify(s, org)
            assert report.events_checked == WRITERS // 2
            assert report.is_intact
            s.commit()


def test_two_appends_in_one_transaction_chain_to_each_other(session):
    """Not concurrency, but the same tail-read question inside one unit of work.

    The second append has to see the first, which is why ``append`` flushes
    before reading the tail. Without it both chain to the row that preceded them
    and the transaction commits a fork all by itself.
    """
    first = asas_audit.append(
        session, org_id=ORG_A, actor="alice", action="one",
        resource_type="thing", resource_id="1",
    )
    second = asas_audit.append(
        session, org_id=ORG_A, actor="alice", action="two",
        resource_type="thing", resource_id="1",
    )
    session.commit()

    assert bytes(second.hash_prev) == bytes(first.hash_current)
    assert asas_audit.verify(session, ORG_A).is_intact
