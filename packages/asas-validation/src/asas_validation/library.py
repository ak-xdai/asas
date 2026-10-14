"""The two doors over the checks file, both derived by reading it (DR 0004).

``validate`` is the namespace: ``validate.after(...)`` exists because a
function named ``after`` exists in ``checks.py`` (or in a module the host
handed to :func:`include_checks`). ``validate("after", ...)`` is the machine
door for callers holding the name as data — an agent that just read
:func:`catalog`, a client-side evaluator. Both doors reach the same function.

Nothing here is authored twice: a check's signature says what it takes and its
docstring's first line is the sentence humans see — :func:`catalog` just reads
them out.
"""

import inspect
from types import ModuleType

from . import checks as _shipped


def _check_functions(module: ModuleType):
    for name, fn in inspect.getmembers(module, inspect.isfunction):
        if not name.startswith("_") and fn.__module__ == module.__name__:
            yield name, fn


class _Validate:
    """The namespace. Attribute door for humans, call door for machines."""

    def __call__(self, name, *args, **kwargs):
        fn = getattr(self, name, None)
        if fn is None or name.startswith("_"):
            known = ", ".join(sorted(n for n in vars(self) if not n.startswith("_")))
            raise LookupError(f"unknown check {name!r}. Known checks: {known}")
        return fn(*args, **kwargs)


validate = _Validate()
_MODULES: dict[str, ModuleType] = {}


def include_checks(module: ModuleType) -> None:
    """Add every public function of ``module`` to the ``validate`` namespace
    and the catalog. The host calls this once at startup with its own checks
    file; adding a check afterwards means adding a function to that file.
    A name that collides with an existing check fails loud — silently
    replacing a shipped check would change behavior everywhere at once.

    All-or-nothing: names are validated FIRST, then applied, so a collision
    mid-module can never leave phantom checks (installed on ``validate`` but
    absent from the catalog). Re-executing the same module (a reload) replaces
    its functions and its catalog entry — never duplicates them."""
    functions = list(_check_functions(module))
    for name, fn in functions:
        existing = getattr(validate, name, None)
        if (existing is not None and existing is not fn
                and getattr(existing, "__module__", None) != fn.__module__):
            raise ValueError(
                f"check {name!r} already exists (from {existing.__module__}); "
                f"rename the function in {module.__name__}"
            )
    for name, fn in functions:
        setattr(validate, name, fn)
    _MODULES[module.__name__] = module


def catalog() -> list[dict]:
    """Every check, machine-readably: name, the values it takes, its settings
    with defaults, and its human sentence. Derived from the functions, so it
    can never drift from the code. Serves IDE-less callers: agents, docs
    generators, and (a later phase) the rules endpoint."""
    out = []
    for module in _MODULES.values():
        for name, fn in _check_functions(module):
            sig = inspect.signature(fn)
            takes, settings = [], {}
            for pname, p in sig.parameters.items():
                if pname in ("field", "message_key"):
                    continue
                if p.kind is inspect.Parameter.VAR_POSITIONAL:
                    takes.append(f"{pname}...")
                elif p.default is inspect.Parameter.empty:
                    takes.append(pname)
                else:
                    settings[pname] = p.default
            out.append({
                "name": name,
                "takes": takes,
                "settings": settings,
                "sentence": next(iter((fn.__doc__ or "").strip().splitlines()), ""),
            })
    return sorted(out, key=lambda c: c["name"])


include_checks(_shipped)

