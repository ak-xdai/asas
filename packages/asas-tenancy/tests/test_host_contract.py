"""Host-contract conformance (Teamy TEAMY-798).

Shape invariants a host integrator relies on when they read the package surface
rather than the source. The behavioural suites live alongside.

The bug that motivated them: a package re-exported names from a submodule called
``seed``, so ``package.seed`` resolved to the module rather than a callable, and
following the documented contract raised ``TypeError: 'module' object is not
callable``.

This package exposes NONE of the schema or router slots on purpose: it owns no
tables, so there is no ``migrate`` and no ``seed``, and there is nothing to
adopt or collide with in a host's existing schema.
"""

import types

import asas_tenancy

CONTRACT_CALLABLES = (
    "build_routers",
    "build_router",
    "build_mcp_app",
    "seed",
    "migrate",
    "configure",
)

INTENTIONAL_MODULE_EXPORTS = set()


def test_declares_all():
    assert hasattr(asas_tenancy, "__all__"), "asas_tenancy must declare __all__"
    assert asas_tenancy.__all__, "asas_tenancy.__all__ must not be empty"


def test_every_exported_name_resolves():
    missing = [n for n in asas_tenancy.__all__ if not hasattr(asas_tenancy, n)]
    assert not missing, f"asas_tenancy.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    broken = [
        n for n in CONTRACT_CALLABLES
        if hasattr(asas_tenancy, n) and not callable(getattr(asas_tenancy, n))
    ]
    assert not broken, (
        f"asas_tenancy exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function."
    )


def test_module_exports_are_intentional():
    exported = {
        n for n in asas_tenancy.__all__
        if isinstance(getattr(asas_tenancy, n), types.ModuleType)
    }
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, (
        f"asas_tenancy exports undeclared modules: {sorted(unexpected)}."
    )


def test_version_is_exported():
    assert isinstance(asas_tenancy.__version__, str)
    assert asas_tenancy.__version__


def test_owns_no_schema_slots():
    """Stated as a test, because "no tables" is the property that makes this the
    cheapest package in the family to adopt: nothing to migrate, nothing to
    stamp, nothing that can clash with a host's own table names."""
    assert not hasattr(asas_tenancy, "migrate")
    assert not hasattr(asas_tenancy, "seed")


def test_importable_without_alembic(monkeypatch):
    """Alembic is an optional extra: it is needed by the migration helpers and by
    nothing else, so a host that only wants the context and the GUC pin must not
    have to install it. The helpers raise with that instruction when called."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "alembic" or name.startswith("alembic."):
            raise ModuleNotFoundError("No module named 'alembic'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    from asas_tenancy import policy

    try:
        policy.enable_rls("thing")
    except ModuleNotFoundError as exc:
        assert "optional extra" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected the guard to fire")
