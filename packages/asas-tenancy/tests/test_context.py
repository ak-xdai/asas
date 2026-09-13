"""The tenant context, and specifically its refusal to be ambiguous."""

from __future__ import annotations

import pytest

import asas_tenancy
from asas_tenancy import TenantContextError


def test_bind_then_read():
    asas_tenancy.bind_tenant(7)
    assert asas_tenancy.current_tenant_id() == 7


def test_unbound_read_raises_rather_than_returning_none():
    """The whole design decision, pinned.

    A ``None`` return reads identically at every call site and behaves
    catastrophically differently: a query built with no tenant does not fail, it
    returns every tenant's rows.
    """
    with pytest.raises(TenantContextError):
        asas_tenancy.current_tenant_id()


def test_maybe_is_the_explicit_opt_out():
    assert asas_tenancy.maybe_tenant_id() is None
    asas_tenancy.bind_tenant(7)
    assert asas_tenancy.maybe_tenant_id() == 7


def test_binding_none_is_refused():
    """``bind_tenant(None)`` looks like a way to clear and is not one: it would
    leave every later read raising as though nothing had ever been bound, which
    is a confusing way to spell ``clear_tenant``."""
    with pytest.raises(TenantContextError):
        asas_tenancy.bind_tenant(None)


def test_clear_makes_reads_raise_again():
    asas_tenancy.bind_tenant(7)
    asas_tenancy.clear_tenant()
    with pytest.raises(TenantContextError):
        asas_tenancy.current_tenant_id()


def test_scope_restores_rather_than_clearing():
    """A nested scope must not wipe its parent's tenant on the way out, which is
    what a ``clear_tenant`` in the unwind would do."""
    asas_tenancy.bind_tenant("outer")
    with asas_tenancy.tenant_scope("inner"):
        assert asas_tenancy.current_tenant_id() == "inner"
    assert asas_tenancy.current_tenant_id() == "outer"


def test_scope_restores_on_exception():
    asas_tenancy.bind_tenant("outer")
    with pytest.raises(ValueError):
        with asas_tenancy.tenant_scope("inner"):
            raise ValueError("handler blew up")
    assert asas_tenancy.current_tenant_id() == "outer"


def test_reset_token_unwinds_one_level():
    token = asas_tenancy.bind_tenant("first")
    asas_tenancy.reset(token)
    assert asas_tenancy.maybe_tenant_id() is None


def test_tenant_id_type_is_the_hosts_business():
    """int, str, UUID: the context stores what it was given, unchanged. Only the
    GUC layer stringifies, at the boundary where everything is text anyway."""
    import uuid

    value = uuid.uuid4()
    asas_tenancy.bind_tenant(value)
    assert asas_tenancy.current_tenant_id() is value
