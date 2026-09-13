"""The assertions that matter: does one tenant actually see only its own rows.

Every test here goes through the ``requires_enforcement`` fixture, because on a
superuser or BYPASSRLS connection they would all pass on a completely
unprotected table. That is not a hypothetical: it is how a broken protection
reaches a deployment with a green suite behind it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

import asas_tenancy
from conftest import TENANT_A, TENANT_B


def test_a_tenant_sees_only_its_own_rows(seeded, requires_enforcement):
    assert asas_tenancy.visible_keys(seeded, "widget", TENANT_A) == {1}
    assert asas_tenancy.visible_keys(seeded, "widget", TENANT_B) == {2}


def test_assert_isolated_passes_on_a_protected_table(seeded, requires_enforcement):
    asas_tenancy.assert_isolated(seeded, "widget", TENANT_A, TENANT_B)


def test_unpinned_connection_sees_nothing(seeded, requires_enforcement):
    """Fail-closed, which is the half a policy can get wrong on its own: scoping
    correctly between two bound tenants while still leaking to a connection that
    bound none. That is what every background job starts as."""
    asas_tenancy.assert_unpinned_sees_nothing(seeded, "widget")


def test_an_empty_string_pin_is_not_a_wildcard(seeded, requires_enforcement):
    """``NULLIF(..., '')`` in the policy is what makes this a filter rather than
    an error. Without it the cast of an empty string raises, which fails closed
    by accident but presents as a broken query rather than as no rows."""
    with seeded.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        assert conn.execute(text("SELECT count(*) FROM widget")).scalar() == 0


def test_writing_another_tenants_row_is_refused(seeded, requires_enforcement):
    """The ``WITH CHECK`` half. A policy with only ``USING`` hides other tenants'
    rows on read and happily lets a caller create one."""
    with pytest.raises(ProgrammingError):
        with seeded.begin() as conn:
            asas_tenancy.set_tenant_guc(conn, TENANT_A)
            conn.execute(
                text("INSERT INTO widget (id, tenant_id, label) VALUES (3, :b, 'sneak')"),
                {"b": TENANT_B},
            )


def test_updating_across_tenants_matches_nothing(seeded, requires_enforcement):
    """Not an error, deliberately: an UPDATE the policy filters reports zero rows
    affected. That silence is exactly the failure ``add_tenant_scoped_column``
    exists to avoid, so it is pinned here as behaviour rather than assumed."""
    with seeded.begin() as conn:
        asas_tenancy.set_tenant_guc(conn, TENANT_A)
        result = conn.execute(text("UPDATE widget SET label = 'rewritten'"))
        assert result.rowcount == 1  # only its own row, not both


def test_a_vacuous_isolation_check_is_an_error(widgets, requires_enforcement):
    """No rows means the assertion cannot fail, so it must not be allowed to
    pass. An empty database is where the D216-class migration bug hides."""
    with pytest.raises(AssertionError, match="Vacuous"):
        asas_tenancy.assert_isolated(widgets, "widget", TENANT_A, TENANT_B)


def test_isolation_helpers_refuse_an_unenforced_connection(seeded):
    """The guard itself. On a bypassing role these helpers must raise rather than
    report success, which is the difference between this kit and a comment."""
    if asas_tenancy.unenforced_reason(seeded) is None:
        pytest.skip("this connection does enforce; the refusal path is the point here")
    with pytest.raises(RuntimeError, match="not enforced"):
        asas_tenancy.assert_isolated(seeded, "widget", TENANT_A, TENANT_B)
