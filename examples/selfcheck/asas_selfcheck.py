"""Asas host self-check — run this against **your own** application.

Contract rows demonstrated: *Schema* (``migrate``) and *Host hooks*
(``configure_*``) from the table in the repository README. This module asserts
nothing about the packages; the per-package ``tests/test_host_contract.py``
suites do that. It asserts things about **a host's integration** — the class of
mistake that is invisible until production, because every package is designed to
degrade quietly when a host forgets it.

Why this exists at all: an agent handed a failing assertion fixes it; an agent
handed a paragraph guesses. Every finding below carries a ``fix`` line naming
the exact call that resolves it.

Usage::

    python asas_selfcheck.py --app myapp.main:app --database-url sqlite:///./app.db
    python asas_selfcheck.py --app myapp.main:app --engine myapp.db:engine

``--app`` matters more than it looks. Hosts almost always install their hooks
inside the FastAPI **lifespan** (that is what Teamy does, and what the reference
host does), so merely importing the module observes an unconfigured process and
every hook reads as missing. This tool therefore *runs the lifespan* and
inspects state inside it, then tears it down.

Using it from pytest needs no plugin — the core is a pure function::

    report = run_checks(engine)
    assert not report.failures, report.format()

Exit status is 0 when there are no failures, 1 otherwise. Warnings never fail
the run: a warning means "this is a legitimate configuration, but it is more
often an oversight", and only the host knows which.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import enum
import importlib
import sys
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine

__all__ = [
    "Level",
    "Finding",
    "Report",
    "run_checks",
    "main",
]


class Level(enum.Enum):
    """Ordered by how much attention the finding deserves."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclasses.dataclass(frozen=True)
class Finding:
    """One observation about the host.

    ``fix`` is the point of the whole tool: a finding a reader cannot act on is
    a paragraph with extra steps.
    """

    level: Level
    package: str
    check: str
    message: str
    fix: Optional[str] = None

    def format(self) -> str:
        mark = {Level.OK: "ok  ", Level.WARN: "warn", Level.FAIL: "FAIL"}[self.level]
        head = f"[{mark}] {self.package}: {self.message}"
        return head if not self.fix else f"{head}\n         fix: {self.fix}"


@dataclasses.dataclass
class Report:
    findings: list[Finding] = dataclasses.field(default_factory=list)

    def add(self, *args: Any, **kwargs: Any) -> None:
        self.findings.append(Finding(*args, **kwargs))

    @property
    def failures(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.FAIL]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level is Level.WARN]

    def format(self, *, verbose: bool = False) -> str:
        shown = self.findings if verbose else [
            f for f in self.findings if f.level is not Level.OK
        ]
        if not shown:
            return "All checks passed."
        lines = [f.format() for f in shown]
        lines.append(
            f"\n{len(self.failures)} failure(s), {len(self.warnings)} warning(s), "
            f"{len(self.findings)} check(s) run."
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------
# One entry per package that a host can get *wrong*. Packages with neither a
# schema nor a hook (asas-validation, asas-mcp) are absent on purpose: there is
# no host-side state to check, and an entry that can never fail is noise.
#
# Hook state is read from the module-level global the setter writes, rather than
# from a public accessor, because no package exposes one. That coupling is
# deliberate and cheap to maintain — it lives in this repository, alongside the
# packages, and a rename breaks this file's own tests immediately.


@dataclasses.dataclass(frozen=True)
class Hook:
    """A ``configure_*`` seam, and whether a host may legitimately skip it."""

    setter: str
    module: str
    state_attr: str
    required: bool
    purpose: str
    default_behaviour: str


@dataclasses.dataclass(frozen=True)
class Spec:
    module: str
    migrates: bool = False
    hooks: Sequence[Hook] = ()


REGISTRY: tuple[Spec, ...] = (
    Spec(
        module="asas_lookups",
        migrates=True,
        hooks=(
            Hook(
                setter="asas_lookups.configure_org_resolver",
                module="asas_lookups.service",
                state_attr="_org_resolver",
                required=False,
                purpose="scope reference data per tenant",
                default_behaviour="single-tenant: every lookup is global",
            ),
        ),
    ),
    Spec(module="asas_access", migrates=True),
    Spec(module="asas_workflow", migrates=True),
    Spec(
        module="asas_notifications",
        migrates=True,
        hooks=(
            Hook(
                setter="asas_notifications.configure_context_resolver",
                module="asas_notifications.service",
                state_attr="_context_resolver",
                required=False,
                purpose="stamp notifications with the acting org and user",
                default_behaviour="single-tenant: rows carry no org or actor",
            ),
            Hook(
                setter="asas_notifications.configure_recipient_filter",
                module="asas_notifications.service",
                state_attr="_recipient_filter",
                required=False,
                purpose="drop recipients who may not see the subject record",
                default_behaviour="every named recipient is notified",
            ),
        ),
    ),
    Spec(
        module="asas_jobs",
        migrates=True,
        hooks=(
            Hook(
                setter="asas_jobs.configure_context_binder",
                module="asas_jobs.registry",
                state_attr="_context_binder",
                required=False,
                purpose="bind each job's tenant context before its handler runs",
                default_behaviour="handlers run with no tenant context bound",
            ),
        ),
    ),
    Spec(module="asas_search", migrates=True),
    Spec(
        module="asas_storage",
        hooks=(
            Hook(
                setter="asas_storage.configure",
                module="asas_storage",
                state_attr="_factory",
                required=True,
                purpose="select the storage backend",
                default_behaviour="storage() raises RuntimeError on first use",
            ),
        ),
    ),
)


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------


def _import(name: str) -> Optional[Any]:
    """Import, or return None if the host does not use this package at all.

    A host is expected to adopt a subset; an absent package is not a finding.

    **Only genuine absence returns None.** Catching every ``ImportError`` would
    make a package that is installed but fails to initialize — a missing
    internal dependency, a broken transitive import — indistinguishable from one
    the host never adopted. It would then be skipped silently and the tool would
    report success over it, which is precisely the class of quiet failure this
    whole file exists to catch.

    So the guard is narrow: a ``ModuleNotFoundError`` naming *this* module means
    absent; anything else propagates.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        # `exc.name` is the module that could not be found. If it is not the one
        # we asked for, the package exists and its own imports are broken.
        if exc.name == name or (exc.name and name.startswith(f"{exc.name}.")):
            return None
        raise


def _chain_head(module: Any) -> Optional[str]:
    """The head revision of a package's own Alembic chain.

    Read from the package's ``migrations/`` directory rather than hardcoded, so
    this tool cannot drift behind a package that adds a revision.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    migrations = Path(module.__file__).parent / "migrations"
    if not migrations.is_dir():
        return None
    cfg = Config()
    cfg.set_main_option("script_location", str(migrations))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _current_revision(engine: Engine, version_table: str) -> Optional[str]:
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(f"SELECT version_num FROM {version_table}")  # noqa: S608 - identifier from our own registry
        ).first()
    return row[0] if row else None


def check_migrations(engine: Engine, report: Report) -> None:
    """Contract row: **Schema** — ``migrate(engine)``.

    Three distinguishable states, and conflating them is what makes this
    failure mode expensive:

    - version table at head — the package is fully migrated;
    - version table behind head — the host upgraded the package but did not
      re-run ``migrate()``, so new columns are missing;
    - no version table — ``migrate()`` has never run against this database. If
      the sentinel tables are nonetheless present, the host is a *brownfield*
      adopter and the next ``migrate()`` will attempt a shape-verified adopt;
      that is reported separately because it can still fail loudly.
    """
    inspector = sa.inspect(engine)

    for spec in REGISTRY:
        if not spec.migrates:
            continue
        module = _import(spec.module)
        if module is None:
            continue

        migrate_mod = _import(f"{spec.module}.migrate")
        if migrate_mod is None:
            report.add(
                Level.FAIL,
                spec.module,
                "migrations",
                f"{spec.module} is installed but has no migrate module",
                fix="Reinstall the package; this build is incomplete.",
            )
            continue

        version_table = getattr(migrate_mod, "VERSION_TABLE")
        head = _chain_head(module)

        if not inspector.has_table(version_table):
            baseline_tables = getattr(migrate_mod, "_BASELINE_TABLES", frozenset())
            present = sorted(t for t in baseline_tables if inspector.has_table(t))
            if present:
                report.add(
                    Level.FAIL,
                    spec.module,
                    "migrations",
                    f"{version_table!r} is missing, but {present} already exist. "
                    f"migrate() has never run here; the next call will try a "
                    f"shape-verified adopt of those tables and will refuse if they "
                    f"are not this package's.",
                    fix=f"Call {spec.module}.migrate(engine) at boot, after your own "
                        f"Alembic chain, and read the error if it refuses.",
                )
            else:
                report.add(
                    Level.FAIL,
                    spec.module,
                    "migrations",
                    f"{spec.module} is imported but its schema has never been "
                    f"migrated ({version_table!r} absent).",
                    fix=f"Call {spec.module}.migrate(engine) at boot, after your own "
                        f"Alembic chain.",
                )
            continue

        current = _current_revision(engine, version_table)
        if head is not None and current != head:
            report.add(
                Level.FAIL,
                spec.module,
                "migrations",
                f"schema is at revision {current!r} but the installed package's "
                f"chain head is {head!r}.",
                fix=f"Call {spec.module}.migrate(engine) at boot — it is idempotent "
                    f"and safe to call on every start.",
            )
        else:
            report.add(
                Level.OK,
                spec.module,
                "migrations",
                f"schema at head ({current}).",
            )


def check_hooks(report: Report) -> None:
    """Contract row: **Host hooks** — ``configure_*``.

    The hooks default to single-tenant/no-op by design, which is exactly why a
    forgotten one is silent. A *required* hook is one whose default is a crash
    or a data-loss-shaped surprise rather than a smaller feature set; everything
    else is a warning that names what the default actually does, so a
    single-tenant host can read it and move on.
    """
    for spec in REGISTRY:
        if _import(spec.module) is None:
            continue
        for hook in spec.hooks:
            owner = _import(hook.module)
            if owner is None:
                # The root package imported (checked above) but the module that
                # owns this hook did not. Skipping would report success over a
                # package we could not actually inspect — the same silent pass
                # this tool exists to prevent.
                report.add(
                    Level.FAIL,
                    spec.module,
                    "hooks",
                    f"{hook.module} could not be imported, so {hook.setter} could "
                    f"not be checked. The package is installed but not loadable.",
                    fix=f"Fix the import error in {hook.module}, then re-run.",
                )
                continue
            if not hasattr(owner, hook.state_attr):
                report.add(
                    Level.FAIL,
                    spec.module,
                    "hooks",
                    f"cannot inspect {hook.setter}: {hook.module}.{hook.state_attr} "
                    f"no longer exists. This self-check is out of date with the "
                    f"installed package.",
                    fix="Update asas_selfcheck's REGISTRY to match the package.",
                )
                continue

            configured = getattr(owner, hook.state_attr) is not None
            if configured:
                report.add(
                    Level.OK, spec.module, "hooks", f"{hook.setter} is configured."
                )
            elif hook.required:
                report.add(
                    Level.FAIL,
                    spec.module,
                    "hooks",
                    f"{hook.setter} was never called — {hook.default_behaviour}.",
                    fix=f"Call {hook.setter}(...) during startup, before anything "
                        f"uses the package.",
                )
            else:
                report.add(
                    Level.WARN,
                    spec.module,
                    "hooks",
                    f"{hook.setter} was never called, so the default applies: "
                    f"{hook.default_behaviour}. Intended if you are single-tenant; "
                    f"otherwise you wanted it to {hook.purpose}.",
                    fix=f"Call {hook.setter}(...) during startup if you need to "
                        f"{hook.purpose}.",
                )


def check_boot_order(report: Report) -> None:
    """Contract row: **Host hooks**, ordering half (DR 0017's storage trap).

    ``asas_storage.configure()`` must run before anything calls ``storage()``,
    which in practice means before the host's ``init_db``/seed step — seeds that
    write files are the usual first caller. The package already fails loudly on
    the misordered path, so this check exists to report the *good* news
    positively: an agent that sees the ordering asserted stops wondering whether
    it matters.
    """
    storage = _import("asas_storage")
    if storage is None:
        return
    if getattr(storage, "_factory", None) is None:
        # Already reported as a required-hook failure; do not double-count.
        return
    report.add(
        Level.OK,
        "asas_storage",
        "boot-order",
        "configure() ran before any storage() call in this process.",
    )


def run_checks(engine: Optional[Engine] = None) -> Report:
    """The whole self-check as a pure function — the pytest entry point.

    ``engine`` may be omitted to check only process state (hooks, boot order);
    migration checks are skipped with a warning, because "no database" and
    "database is fine" must not look alike.
    """
    report = Report()
    if engine is not None:
        check_migrations(engine, report)
    else:
        report.add(
            Level.WARN,
            "-",
            "migrations",
            "no engine supplied; schema checks were skipped.",
            fix="Pass --database-url or --engine to check migration state.",
        )
    check_hooks(report)
    check_boot_order(report)
    return report


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _load_object(spec: str) -> Any:
    """Resolve ``module:attr``.

    The working directory goes on ``sys.path`` first, because ``--app`` is
    deliberately spelled the way ``uvicorn`` spells it and uvicorn puts cwd on
    the path. Without this, the obvious invocation from a host's own source
    directory — ``--app app.main:app`` — fails with a bare ImportError, which
    reads as "the tool is broken" rather than "add PYTHONPATH".
    """
    if ":" not in spec:
        raise SystemExit(f"expected 'module:attr', got {spec!r}")
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    module_name, _, attr = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise SystemExit(f"cannot import {module_name!r}: {exc}") from exc
    try:
        return getattr(module, attr)
    except AttributeError as exc:
        raise SystemExit(f"{module_name!r} has no attribute {attr!r}") from exc


@contextlib.contextmanager
def _app_lifespan(app: Any) -> Iterator[None]:
    """Run the host's startup so hook state is observable, then shut it down.

    Without this the tool would inspect a process where the host's lifespan
    never ran and report every hook as missing — the single most likely way for
    a self-check to be confidently wrong.
    """
    router = getattr(app, "router", None)
    lifespan_context: Optional[Callable[[Any], Any]] = getattr(
        router, "lifespan_context", None
    )
    if lifespan_context is None:
        yield
        return

    loop = asyncio.new_event_loop()
    ctx = lifespan_context(app)
    try:
        # Enter first, outside the try that guarantees exit: calling __aexit__
        # on a context that never entered is its own error, and it would mask
        # the startup failure that is the thing worth reporting.
        loop.run_until_complete(ctx.__aenter__())
        try:
            yield
        finally:
            # Shutdown failures propagate. Suppressing them lets main() return
            # 0 for a host whose teardown raised — a self-check that reports
            # success over a failure is worse than no self-check.
            loop.run_until_complete(ctx.__aexit__(None, None, None))
    finally:
        loop.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check that a host application wires the Asas packages correctly.",
    )
    parser.add_argument(
        "--app",
        help="FastAPI app as 'module:attr'. Its lifespan is run so that hooks "
             "installed at startup are visible.",
    )
    parser.add_argument("--database-url", help="SQLAlchemy URL to check schema against.")
    parser.add_argument(
        "--engine", help="Existing Engine as 'module:attr', instead of --database-url."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Also print passing checks."
    )
    args = parser.parse_args(argv)

    if args.database_url and args.engine:
        raise SystemExit("pass --database-url or --engine, not both")

    app = _load_object(args.app) if args.app else None

    with _app_lifespan(app) if app is not None else contextlib.nullcontext():
        engine: Optional[Engine] = None
        if args.engine:
            engine = _load_object(args.engine)
        elif args.database_url:
            engine = sa.create_engine(args.database_url)
        report = run_checks(engine)
        # Formatted inside the lifespan purely so a host that tears down its
        # engine on shutdown cannot make the report unprintable.
        text = report.format(verbose=args.verbose)

    print(text)
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
