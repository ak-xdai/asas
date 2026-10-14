"""Host-contract conformance (Teamy TEAMY-798).

Shape invariants a host integrator relies on when they read the package surface
rather than the source. The behavioural suites live alongside.

The bug that motivated them: a package re-exported names from a submodule called
``seed``, so ``package.seed`` resolved to the module rather than a callable, and
following the documented contract raised ``TypeError: 'module' object is not
callable``.
"""

import types

import asas_audit

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
    assert hasattr(asas_audit, "__all__"), "asas_audit must declare __all__"
    assert asas_audit.__all__, "asas_audit.__all__ must not be empty"


def test_every_exported_name_resolves():
    missing = [n for n in asas_audit.__all__ if not hasattr(asas_audit, n)]
    assert not missing, f"asas_audit.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    broken = [
        n for n in CONTRACT_CALLABLES
        if hasattr(asas_audit, n) and not callable(getattr(asas_audit, n))
    ]
    assert not broken, (
        f"asas_audit exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function."
    )


def test_module_exports_are_intentional():
    exported = {
        n for n in asas_audit.__all__
        if isinstance(getattr(asas_audit, n), types.ModuleType)
    }
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, f"asas_audit exports undeclared modules: {sorted(unexpected)}."


def test_version_is_exported():
    assert isinstance(asas_audit.__version__, str)
    assert asas_audit.__version__


def test_there_is_no_seed():
    """An audit log with seeded rows would be a record of things that did not
    happen, so this slot is empty on purpose rather than by omission."""
    assert not hasattr(asas_audit, "seed")


def test_the_chain_primitives_are_reachable_without_a_database():
    """A host verifying an export, or writing its own report, needs the encoding
    and the arithmetic. They are pure, and exported for that reason."""
    for name in ("canonical_bytes", "chain_payload", "compute_hash", "verify_rows"):
        assert callable(getattr(asas_audit, name))
