"""Tests for the host self-check.

These are tests *of the checker*, which means each one builds a deliberately
broken host and asserts the checker notices. The interesting property is not
that a correct host passes — it is that each distinct way of being wrong
produces a distinct, actionable finding, because a checker that collapses three
causes into one message sends the reader to the wrong fix.

Run with the repo's selfcheck venv, from ``examples/selfcheck``::

    pytest tests/ -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asas_selfcheck as sc  # noqa: E402

import asas_lookups  # noqa: E402
import asas_storage  # noqa: E402

# Fixtures (engine / migrated / clean_process_state) live in conftest.py.


def findings_for(report, package, check):
    return [f for f in report.findings if f.package == package and f.check == check]


# --------------------------------------------------------------------------
# Schema checks
# --------------------------------------------------------------------------


def test_unmigrated_host_fails_per_package(engine):
    """The base case: packages imported, migrate() never called."""
    report = sc.Report()
    sc.check_migrations(engine, report)

    assert report.failures
    jobs = findings_for(report, "asas_jobs", "migrations")
    assert len(jobs) == 1
    assert jobs[0].level is sc.Level.FAIL
    assert "never been migrated" in jobs[0].message
    assert "asas_jobs.migrate(engine)" in jobs[0].fix


def test_migrated_host_passes(migrated):
    report = sc.Report()
    sc.check_migrations(migrated, report)

    assert not report.failures, report.format()
    jobs = findings_for(report, "asas_jobs", "migrations")
    assert jobs[0].level is sc.Level.OK
    assert "at head" in jobs[0].message


def test_schema_behind_head_is_distinguished_from_never_migrated(migrated):
    """A host that upgraded the package but did not re-run migrate().

    This must not read as "never migrated": the fix is the same call, but the
    diagnosis a reader forms from the message drives whether they go looking for
    a missing boot step or a missing redeploy.

    Uses asas-notifications because it is the package with more than one
    revision, so "rewound to the baseline" is a state a real host can reach —
    rather than writing a revision string no chain ever had.
    """
    with migrated.begin() as conn:
        conn.execute(
            sa.text("UPDATE alembic_version_asas_notifications SET version_num = '0001'")
        )

    report = sc.Report()
    sc.check_migrations(migrated, report)

    found = findings_for(report, "asas_notifications", "migrations")
    assert found[0].level is sc.Level.FAIL
    assert "'0001'" in found[0].message
    assert "chain head" in found[0].message
    assert "never been migrated" not in found[0].message


def test_brownfield_tables_without_version_table_names_adoption(migrated):
    """DR 0030's headline defect, caught *before* the loud failure.

    Dropping only the version table leaves exactly the brownfield shape: the
    package's tables exist, but nothing records that its chain ever ran. The
    checker must say so and warn that the next migrate() attempts an adopt,
    rather than reporting a plain "never migrated".
    """
    with migrated.begin() as conn:
        conn.execute(sa.text("DROP TABLE alembic_version_asas_jobs"))

    report = sc.Report()
    sc.check_migrations(migrated, report)

    jobs = findings_for(report, "asas_jobs", "migrations")
    assert jobs[0].level is sc.Level.FAIL
    assert "already exist" in jobs[0].message
    assert "adopt" in jobs[0].message
    assert "background_job" in jobs[0].message


def test_absent_package_is_not_a_finding(engine, monkeypatch):
    """A host adopts a subset; the packages it never installed are not its problem."""
    real_import = sc._import

    def fake_import(name):
        return None if name.startswith("asas_jobs") else real_import(name)

    monkeypatch.setattr(sc, "_import", fake_import)

    report = sc.Report()
    sc.check_migrations(engine, report)

    assert not findings_for(report, "asas_jobs", "migrations")


# --------------------------------------------------------------------------
# Hook checks
# --------------------------------------------------------------------------


def test_missing_required_hook_fails():
    asas_storage._factory = None

    report = sc.Report()
    sc.check_hooks(report)

    storage = findings_for(report, "asas_storage", "hooks")
    assert storage[0].level is sc.Level.FAIL
    assert "asas_storage.configure" in storage[0].message


def test_configured_required_hook_passes():
    asas_storage.configure(lambda: object())

    report = sc.Report()
    sc.check_hooks(report)

    storage = findings_for(report, "asas_storage", "hooks")
    assert storage[0].level is sc.Level.OK


def test_missing_optional_hook_warns_and_states_the_default():
    """The single-tenant host is correct, not broken.

    A checker that failed here would train its reader to ignore it, so the
    finding has to carry the default behaviour and let the host decide.
    """
    asas_lookups.service._org_resolver = None

    report = sc.Report()
    sc.check_hooks(report)

    lookups = findings_for(report, "asas_lookups", "hooks")
    assert lookups[0].level is sc.Level.WARN
    assert "single-tenant" in lookups[0].message
    # Scoped to this package: an unrelated required hook elsewhere in the
    # process must not decide whether *this* assertion holds.
    assert not [f for f in lookups if f.level is sc.Level.FAIL]


def test_registry_drift_is_reported_as_a_checker_fault(monkeypatch):
    """If a package renames the global we read, say *that* — do not report the
    host as misconfigured for a mistake in this file."""
    monkeypatch.delattr(asas_storage, "_factory")

    report = sc.Report()
    sc.check_hooks(report)

    storage = findings_for(report, "asas_storage", "hooks")
    assert storage[0].level is sc.Level.FAIL
    assert "out of date" in storage[0].message
    assert "REGISTRY" in storage[0].fix


def test_registry_matches_the_installed_packages():
    """The coupling in REGISTRY, pinned.

    Every hook the registry names must exist on the installed package. This is
    what makes reading private module state acceptable: a rename fails here, in
    this repository, on the PR that renames it.
    """
    broken = []
    for spec in sc.REGISTRY:
        if sc._import(spec.module) is None:
            continue
        for hook in spec.hooks:
            owner = sc._import(hook.module)
            if owner is None or not hasattr(owner, hook.state_attr):
                broken.append(f"{hook.module}.{hook.state_attr}")
            setter_module, _, setter_name = hook.setter.rpartition(".")
            mod = sc._import(setter_module)
            if mod is not None and not callable(getattr(mod, setter_name, None)):
                broken.append(hook.setter)
    assert not broken, f"REGISTRY names that no longer resolve: {broken}"


# --------------------------------------------------------------------------
# Report / exit-code behaviour
# --------------------------------------------------------------------------


def test_warnings_alone_do_not_fail_the_run(migrated):
    """Exit status is the contract for CI use: warnings are advice, not breakage."""
    asas_storage.configure(lambda: object())

    report = sc.run_checks(migrated)

    assert report.warnings
    assert not report.failures


def test_missing_engine_warns_rather_than_looking_clean():
    """"No database" and "database is fine" must not produce the same output."""
    report = sc.run_checks(None)

    migrations = findings_for(report, "-", "migrations")
    assert migrations[0].level is sc.Level.WARN
    assert "skipped" in migrations[0].message


def test_format_hides_passing_checks_unless_verbose(migrated):
    asas_storage.configure(lambda: object())
    report = sc.run_checks(migrated)

    assert "at head" not in report.format()
    assert "at head" in report.format(verbose=True)
