"""Pinning the tenant onto the database session."""

from __future__ import annotations

import pytest
from sqlalchemy import text

import asas_tenancy
from asas_tenancy.guc import validate_guc_name


def test_pin_is_readable_back(engine):
    if engine.dialect.name != "postgresql":
        pytest.skip("custom session settings are a Postgres feature")
    with engine.begin() as conn:
        asas_tenancy.set_tenant_guc(conn, "tenant-9")
        assert asas_tenancy.read_tenant_guc(conn) == "tenant-9"


def test_local_pin_does_not_survive_the_transaction(engine):
    """``local=True`` is the default and the safe one: a pooled connection must
    not carry one request's tenant into the next request's work."""
    if engine.dialect.name != "postgresql":
        pytest.skip("custom session settings are a Postgres feature")
    with engine.begin() as conn:
        asas_tenancy.set_tenant_guc(conn, "tenant-9")
    with engine.begin() as conn:
        assert asas_tenancy.read_tenant_guc(conn) is None


def test_value_is_bound_not_interpolated(engine):
    """A quote in the value must not be able to end the literal. The value comes
    from a verified credential rather than a caller, so this is defence in depth,
    but the obvious ``SET LOCAL`` spelling cannot take a bind at all and that is
    how a host ends up formatting one in."""
    if engine.dialect.name != "postgresql":
        pytest.skip("custom session settings are a Postgres feature")
    hostile = "x' , false); DROP TABLE widget; --"
    with engine.begin() as conn:
        asas_tenancy.set_tenant_guc(conn, hostile)
        assert asas_tenancy.read_tenant_guc(conn) == hostile


def test_empty_and_unset_read_the_same(engine):
    """A cleared pin and an absent one are one state as far as callers go, which
    is what the policy's ``NULLIF`` relies on."""
    if engine.dialect.name != "postgresql":
        pytest.skip("custom session settings are a Postgres feature")
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        assert asas_tenancy.read_tenant_guc(conn) is None


@pytest.mark.parametrize(
    "bad",
    ["tenant_id", "a.b.c", "", "app.", ".id", "app.1id", "app.tenant-id",
     "app.tenant_id; DROP TABLE widget"],
)
def test_bad_guc_names_are_refused(bad):
    """The GUC NAME is interpolated (``set_config`` takes no bind for it), so it
    is validated rather than trusted, even though it is normally a constant."""
    with pytest.raises(ValueError):
        validate_guc_name(bad)


def test_good_guc_name_accepted():
    validate_guc_name("app.tenant_id")
    validate_guc_name("myorg.current_org")


def test_async_variant_is_a_coroutine_function():
    """The async pin exists because the sync one cannot serve an async host at
    all, and it deliberately imports no async session type, so the package stays
    installable with no async driver and no greenlet."""
    import inspect

    assert inspect.iscoroutinefunction(asas_tenancy.async_set_tenant_guc)


def test_the_pin_is_a_no_op_off_postgres(engine):
    """A dual-engine host runs the same code on both, so the pin has to go quiet
    on a database that has no session variables rather than raise.

    Found by adopting this package from a sibling: ``set_config`` is a Postgres
    function, so a SQLite suite failed on the PIN instead of on the absent
    isolation, which sends the reader looking in the wrong place entirely. The
    pin going quiet is honest, because there is no policy to feed either. What
    must never go quiet is the claim that isolation exists, which is what the
    conformance kit refuses to do.
    """
    if engine.dialect.name == "postgresql":
        pytest.skip("this is the non-Postgres path")
    with engine.begin() as conn:
        asas_tenancy.set_tenant_guc(conn, "tenant-9")   # must not raise
        assert asas_tenancy.read_tenant_guc(conn) is None


def test_capability_is_detected_through_every_shape(engine):
    """A Connection carries ``dialect``; a Session answers ``get_bind()``. The
    module's whole point is not to care which, so it walks both."""
    from sqlmodel import Session

    expected = engine.dialect.name == "postgresql"
    with engine.connect() as conn:
        assert asas_tenancy.supports_guc(conn) is expected
    with Session(engine) as s:
        assert asas_tenancy.supports_guc(s) is expected


def test_an_unrecognised_shape_is_assumed_capable():
    """Fail loudly on the statement rather than silently skip the pin: a skipped
    pin looks exactly like working isolation and is the opposite of it."""
    class Mystery:
        pass

    assert asas_tenancy.supports_guc(Mystery()) is True
