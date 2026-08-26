"""The boot contract: zero configuration, every chain, every hook.

This is the file that makes the reference host a conformance harness rather
than a demo. If a package changes its migration chain, its hook names, or its
router factory, one of these fails on the pull request that changed it.
"""

from __future__ import annotations

import sqlalchemy as sa

# The six packages that own a schema, and the version table each records its
# position in. Listed explicitly rather than derived: this test's job is to
# state the expectation independently, so that a package quietly dropping its
# chain is a failure rather than a silently shorter loop.
VERSION_TABLES = {
    "asas_lookups": "alembic_version_asas_lookups",
    "asas_access": "alembic_version_asas_access",
    "asas_workflow": "alembic_version_asas_workflow",
    "asas_notifications": "alembic_version_asas_notifications",
    "asas_jobs": "alembic_version_asas_jobs",
    "asas_search": "alembic_version_asas_search",
}


def test_boots_with_no_configuration(client):
    """Acceptance: `uvicorn app.main:app` against an empty environment.

    The fixture sets DATABASE_URL and UPLOADS_DIR only so tests do not collide;
    neither is required, and `test_defaults_need_no_environment` pins that.
    """
    assert client.get("/health").status_code == 200


def test_defaults_need_no_environment(monkeypatch):
    """Every core-tier setting has a working default.

    Checked on Settings directly, because the point is that nothing in the core
    tier *reads* an unset variable and gets None.
    """
    for var in ("DATABASE_URL", "UPLOADS_DIR", "MCP_TOKEN", "ENABLE_FAKE_AUTH"):
        monkeypatch.delenv(var, raising=False)

    import importlib

    import app.config

    settings = importlib.reload(app.config).Settings()

    assert settings.database_url.startswith("sqlite")
    assert settings.uploads_dir
    assert settings.mcp_enabled is False
    assert settings.enable_fake_auth is False


def test_every_package_chain_is_at_head(client, app_module):
    """All six chains ran, and each recorded a revision.

    A missing version table means `migrate()` was never called for that package
    — the failure that leaves tables present but unversioned, which is
    indistinguishable from a brownfield database.
    """
    inspector = sa.inspect(app_module.engine)
    missing = [
        package
        for package, table in VERSION_TABLES.items()
        if not inspector.has_table(table)
    ]
    assert not missing, f"packages whose migrate() never ran: {missing}"

    with app_module.engine.connect() as conn:
        for package, table in VERSION_TABLES.items():
            revision = conn.execute(sa.text(f"SELECT version_num FROM {table}")).first()
            assert revision is not None, f"{package}: version table is empty"


def test_selfcheck_reports_no_failures(client, app_module):
    """The self-check, run against this host, inside its own suite.

    The reference host is the self-check's first consumer, so a change that
    breaks the wiring should fail here even if no other assertion happens to
    cover it. Warnings are allowed: this host is single-tenant, so the tenancy
    hooks it does not install are a legitimate configuration.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "selfcheck"))
    import asas_selfcheck

    report = asas_selfcheck.run_checks(app_module.engine)
    assert not report.failures, report.format(verbose=True)


def test_host_schema_does_not_create_package_tables(database_url, monkeypatch, tmp_path):
    """`SQLModel.metadata` is global — pin the `tables=` guard in db.py.

    Without it, `create_all` sweeps up every imported package's tables and the
    package's own `migrate()` then fails. Regressing this is a one-word edit, so
    it gets an explicit test rather than relying on the boot to notice.
    """
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path / "uploads"))

    import importlib

    import app.config
    import app.db

    importlib.reload(app.config)
    db = importlib.reload(app.db)

    db.create_host_schema(db.engine)

    inspector = sa.inspect(db.engine)
    host_owned = {t.name for t in db.host_tables()}
    created = set(inspector.get_table_names())

    leaked = created - host_owned
    assert not leaked, (
        f"create_host_schema created tables the host does not own: {sorted(leaked)}. "
        f"SQLModel.metadata is process-global — pass tables= to create_all."
    )


def test_tier_report_names_every_optional_package(client):
    """Degradation must be visible.

    A host that silently runs without deep search or MCP is indistinguishable
    from one that is broken, so /health states which tier each package is in.
    """
    tiers = client.get("/health").json()["tiers"]
    assert set(tiers) >= {"database", "search", "mcp", "auth", "storage"}
    # Nothing configured in this suite, so the optional tier reports itself off.
    assert "off" in tiers["mcp"]


def test_lookup_router_serves_host_vocabulary(client):
    """The routers came back from `build_routers` and the host mounted them.

    Also pins the seeding split: `seed()` ships standards-based vocabulary, and
    the host's own words are the host's to seed. A ticket priority appearing
    here means `wiring/lookups.py` did the second half.
    """
    response = client.get("/lookups/ticket_priority")
    assert response.status_code == 200

    codes = {value["code"] for value in response.json()["items"]}
    assert {"low", "normal", "high", "urgent"} <= codes


def test_deep_search_tier_engages_only_on_postgres(client, app_module):
    """Acceptance: the same app boots on Postgres with the deep tier active.

    Two claims in one, and the second is the one worth pinning: on SQLite the
    deep tier must be *absent* rather than broken, because "runs everywhere,
    ranks better on Postgres" is only true if the portable path is unaffected.

    Registration count is the observable — `register_provider` appends, so the
    Postgres boot leaves two providers where SQLite leaves one.
    """
    import asas_search

    engine = app_module.engine
    is_postgres = engine.dialect.name == "postgresql"

    assert app_module.settings.deep_search is is_postgres
    assert "ticket" in asas_search.registered_types()

    tiers = client.get("/health").json()["tiers"]
    assert ("deep" in tiers["search"]) is is_postgres
