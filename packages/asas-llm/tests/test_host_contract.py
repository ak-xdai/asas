"""Host-contract conformance (Teamy TEAMY-798).

Shape invariants a host integrator relies on when they read the package surface
rather than the source. The behavioural suites live alongside.

The bug that motivated them: a package re-exported names from a submodule
called ``seed``, so ``package.seed`` resolved to the module rather than a
callable, and following the documented contract raised
``TypeError: 'module' object is not callable``.

This package is the table-less, router-less variant: it exposes ``configure``
and nothing from the schema or router rows.
"""

import subprocess
import sys
import types
from pathlib import Path

import asas_llm

CONTRACT_CALLABLES = (
    "build_routers",
    "build_router",
    "build_mcp_app",
    "seed",
    "migrate",
    "configure",
)

INTENTIONAL_MODULE_EXPORTS = set()

SRC = str(Path(__file__).resolve().parents[1] / "src")


def test_declares_all():
    assert hasattr(asas_llm, "__all__"), "asas_llm must declare __all__"
    assert asas_llm.__all__, "asas_llm.__all__ must not be empty"


def test_every_exported_name_resolves():
    missing = [n for n in asas_llm.__all__ if not hasattr(asas_llm, n)]
    assert not missing, f"asas_llm.__all__ names that do not resolve: {missing}"


def test_contract_names_are_callable():
    broken = [
        n for n in CONTRACT_CALLABLES
        if hasattr(asas_llm, n) and not callable(getattr(asas_llm, n))
    ]
    assert not broken, (
        f"asas_llm exposes contract names that are not callable: {broken}. "
        f"A submodule is probably shadowing the function."
    )


def test_module_exports_are_intentional():
    exported = {
        n for n in asas_llm.__all__
        if isinstance(getattr(asas_llm, n), types.ModuleType)
    }
    unexpected = exported - INTENTIONAL_MODULE_EXPORTS
    assert not unexpected, f"asas_llm exports undeclared modules: {sorted(unexpected)}."


def test_version_is_exported():
    assert isinstance(asas_llm.__version__, str)
    assert asas_llm.__version__


def test_owns_no_schema_or_router_slots():
    """Stated as a test: no tables, no routers, so adoption cannot collide with
    a host's schema or its auth model."""
    assert not hasattr(asas_llm, "migrate")
    assert not hasattr(asas_llm, "seed")
    assert not hasattr(asas_llm, "build_routers")
    assert not hasattr(asas_llm, "build_router")
    assert callable(asas_llm.configure)
    assert callable(asas_llm.runner)


def test_runner_requires_configure():
    import pytest

    with pytest.raises(asas_llm.NotConfigured):
        asas_llm.runner()


def test_import_loads_no_optional_sdk():
    """``import asas_llm`` must not import Langfuse or LangChain: a host using
    another registry, or only the structured-output helpers, does not install
    them. Checked in a fresh interpreter so this suite's own imports cannot
    mask a regression."""
    code = (
        "import sys, asas_llm; "
        "bad = [m for m in ('langfuse', 'langchain_core', 'langchain', 'langchain_openai', 'starlette', 'openai') "
        "if m in sys.modules]; "
        "print(','.join(bad))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True,
        env={"PYTHONPATH": SRC, "PATH": ""},
    )
    assert out.stdout.strip() == "", f"import asas_llm loaded optional SDKs: {out.stdout.strip()}"


def test_optional_modules_say_which_extra(monkeypatch):
    """The Langfuse and runner modules import their SDK lazily and name the
    extra to install when it is missing."""
    import builtins

    real_import = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "langfuse" or name.startswith("langfuse.") or name.startswith("langchain_core"):
            raise ModuleNotFoundError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    from asas_llm import langfuse as lf
    from asas_llm import runners as rn

    try:
        lf.make_client(public_key="pk", secret_key="sk")
    except ModuleNotFoundError as exc:
        assert "asas-llm[langfuse]" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected the guard to fire")

    try:
        rn._require_langchain()
    except ModuleNotFoundError as exc:
        assert "asas-llm[langchain]" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected the guard to fire")
