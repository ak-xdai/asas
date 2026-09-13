"""One tenant cannot read another's history.

Worth its own file because this is the table where cross-tenant visibility would
be worst: an audit log is a record of who did what, so leaking it leaks the shape
of another tenant's whole operation, not one record of it.

Every test goes through ``requires_enforcement``, since on a superuser connection
they would all pass against a completely unprotected table.
"""

from __future__ import annotations

import asas_tenancy
from conftest import ORG_A, ORG_B, append_and_commit


def test_the_table_is_protected(requires_enforcement):
    """The policy is emitted by ``asas_tenancy`` inside this package's own chain,
    so the package's table and the host's are protected by one definition."""
    asas_tenancy.assert_tables_protected(requires_enforcement, ["audit_event"])


def test_tenants_see_disjoint_histories(requires_enforcement):
    engine = requires_enforcement
    a = append_and_commit(engine, ORG_A, action="a.thing")
    b = append_and_commit(engine, ORG_B, action="b.thing")
    assert a != b

    seen_a = asas_tenancy.visible_keys(engine, "audit_event", ORG_A, key="id")
    seen_b = asas_tenancy.visible_keys(engine, "audit_event", ORG_B, key="id")
    assert seen_a == {a}
    assert seen_b == {b}


def test_an_unpinned_connection_sees_no_history(requires_enforcement):
    """Fail-closed. A background job or a misconfigured request binds no tenant,
    and must get nothing rather than everything."""
    engine = requires_enforcement
    append_and_commit(engine, ORG_A)
    asas_tenancy.assert_unpinned_sees_nothing(engine, "audit_event")


def test_isolation_holds_between_two_tenants(requires_enforcement):
    engine = requires_enforcement
    append_and_commit(engine, ORG_A)
    append_and_commit(engine, ORG_B)
    asas_tenancy.assert_isolated(engine, "audit_event", ORG_A, ORG_B, key="id")
