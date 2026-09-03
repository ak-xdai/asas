"""Per-package boot snippets used by `asas new`.

Every non-comment line generated here is real, runnable code against the
package's actual public surface (verified against each package's
``__init__.py`` and router/service signatures, not guessed) — a fresh
`asas new` project must boot cleanly with zero edits, even though most of
what it does is a stub. Lines that need host-specific policy/data the CLI
cannot know (permission grants, workflow specs, tool implementations, …)
are left as ``# TODO`` comments naming the real function to call, never as
fabricated calls.

Three buckets per package:

- ``imports`` — module-level imports.
- ``setup`` — module-level statements, run once at import time, after
  ``app``/``get_session``/``engine`` exist. Router includes and one-shot
  ``configure_*``/``register_*``/``declare_*`` calls belong here — none of
  them need a live session.
- ``boot`` — statements run inside the startup hook, where an open
  ``session`` is available. ``migrate(engine)`` and any ``seed*(session)``
  call belong here.

``settings_fields`` are appended to the generated ``AppSettings`` when that
package is selected.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BootSnippet:
    imports: tuple[str, ...] = ()
    setup: tuple[str, ...] = ()
    boot: tuple[str, ...] = ()
    settings_fields: tuple[tuple[str, str, str], ...] = ()  # (name, type, default literal)


SNIPPETS: dict[str, BootSnippet] = {
    "lookups": BootSnippet(
        imports=("import asas_lookups",),
        setup=(
            "_lookups_routers = asas_lookups.build_routers(get_session)",
            "app.include_router(_lookups_routers.read)",
            "app.include_router(_lookups_routers.admin)",
            "# asas_lookups.configure_org_resolver(my_org_resolver)  # TODO: only if multi-tenant",
        ),
        boot=(
            "asas_lookups.migrate(engine)",
            "asas_lookups.seed(session)",
        ),
    ),
    "validation": BootSnippet(
        imports=("import asas_validation",),
        setup=(
            "asas_validation.declare_rules([\n"
            "    # TODO: your Rule catalog — see asas_validation.Rule\n"
            "])",
            'asas_validation.register_fields("your_entity", {"field_a", "field_b"})  '
            "# TODO: real entity + field names",
            "asas_validation.assert_rules_known()  # fails loud at boot on a typo'd rule name",
            'app.include_router(asas_validation.build_router())  # already prefixed "/validation"',
        ),
    ),
    "storage": BootSnippet(
        imports=("import asas_storage", "from pathlib import Path"),
        setup=(
            'asas_storage.configure(lambda: asas_storage.LocalStorage(Path("./var/storage")))',
            "# TODO: swap for asas_storage.S3Storage(...) or asas_storage.AzureBlobStorage(...) in prod",
        ),
    ),
    "ratelimit": BootSnippet(
        imports=("import asas_ratelimit",),
        setup=(
            "asas_ratelimit.configure(enabled=settings.rate_limit_enabled)",
            'asas_ratelimit.declare(asas_ratelimit.Rule(name="example.rule", limit=30, '
            "window_seconds=3600))  # TODO: your real rules",
            "# for _name, (_count, _window) in "
            "asas_ratelimit.parse_overrides(settings.rate_limit_overrides).items():",
            "#     asas_ratelimit.declare(asas_ratelimit.Rule(name=_name, limit=_count, "
            "window_seconds=_window))",
        ),
        settings_fields=(
            ("rate_limit_enabled", "bool", "True"),
            ("rate_limit_overrides", "str", '""'),
        ),
    ),
    "jobs": BootSnippet(
        imports=("import asas_jobs",),
        setup=(
            "def example_job_handler(session, payload):\n"
            "    ...  # TODO: your real handler body",
            "",
            "asas_jobs.configure_runner(lambda: Session(engine), poll_seconds=5.0, "
            "lease_seconds=60)",
            "# asas_jobs.configure_context_binder(my_context_binder)  "
            "# TODO: only if jobs need tenant context",
            'asas_jobs.register_handler("example.kind", example_job_handler)  '
            "# TODO: your real handlers",
            "# Run the worker loop somewhere — a background task or a separate process:",
            "# asas_jobs.run_loop()",
        ),
        boot=("asas_jobs.migrate(engine)",),
    ),
    "access": BootSnippet(
        imports=("import asas_access",),
        setup=(
            "# Everything below is host policy data/hooks — see the package README for each:",
            "# asas_access.register_resolver(...), register_global_source(...), "
            "register_record_source(...)",
            "# asas_access.register_actions([...]); asas_access.register_fields(...)",
            "# asas_access.reserve_principals([...]); asas_access.register_private_viewers(...)",
            "# asas_access.register_classified_entity(...); asas_access.register_subject_source(...)",
        ),
        boot=(
            "asas_access.migrate(engine)",
            "# asas_access.ensure_system_groups(session, [...])  # TODO",
            "# asas_access.ensure_clearance_levels(session, [...])  # TODO",
            "# asas_access.seed_field_permissions(session, [...])  # TODO",
            "# asas_access.seed_action_permissions(session, [...])  # TODO",
        ),
    ),
    "workflow": BootSnippet(
        imports=("import asas_workflow",),
        setup=(
            "# asas_workflow.register_definition(asas_workflow.DefinitionSpec(...))  "
            "# TODO: your process specs",
            "# asas_workflow.register_system_handler(...), register_completion_callback(...)",
            "# asas_workflow.register_subject_renderer(...), register_assignee_resolver(...)",
            "# asas_workflow.register_floor_resolver(...)  "
            "# fail-closed floor — required before go-live",
        ),
        boot=(
            "asas_workflow.migrate(engine)",
            "asas_workflow.seed_workflow_definitions(session)  "
            "# seeds whatever was register_definition()'d above",
        ),
    ),
    "notifications": BootSnippet(
        imports=("import asas_notifications",),
        setup=(
            "asas_notifications.configure_context_resolver(None)  "
            "# TODO: (session) -> (user_id, org_id) | None",
            "# TODO: asas_notifications.configure_recipient_filter(fn) — "
            "(session, user_ids, entity_type, entity_id, record) -> visible user_ids. "
            "Left unconfigured (not None-configured) on purpose: with no filter, "
            "notify() skips visibility filtering, so wire this before notifying "
            "on any private record.",
            "# TODO: seed your topic vocabulary (~5-8 rows; an emit's topic= must "
            'exist — migrate() seeds only "general"):\n'
            "# with Session(engine) as s:\n"
            '#     s.add(asas_notifications.NotificationTopic(key="jobs", name="Jobs"))\n'
            "#     s.commit()\n"
            "# Emits then pass the causing action plus the four axes, no registration:\n"
            '# asas_notifications.notify(session, recipients, action="job.publish",\n'
            '#     topic="jobs", nature="info", urgency="normal", reason="watching",\n'
            '#     title=...)',
            'app.include_router(asas_notifications.build_router(get_session))  '
            '# already prefixed "/me/notifications"',
            "# Dispatch the outbox on your own cadence (after-commit hook / boot sweep / periodic job):",
            "# asas_notifications.dispatch_pending(engine)",
        ),
        boot=("asas_notifications.migrate(engine)",),
    ),
    "search": BootSnippet(
        imports=("import asas_search",),
        setup=(
            '# asas_search.register_provider("your_entity", your_provider)  '
            "# TODO: a Provider per searchable entity type",
        ),
        boot=(
            "asas_search.migrate(engine)  "
            "# Postgres-only DDL; SQLite records the version and creates nothing",
        ),
    ),
    "mcp": BootSnippet(
        imports=("import asas_mcp", "from starlette.routing import Route"),
        setup=(
            "def my_tool_lister():\n"
            "    return []  # TODO: real list[asas_mcp.MCPToolDef]",
            "",
            "def my_tool_runner(name, arguments):\n"
            "    raise NotImplementedError(name)  # TODO: dispatch real tools",
            "",
            "asas_mcp_app, asas_mcp_lifespan = asas_mcp.build_mcp_app(\n"
            '    name="{project_name}",\n'
            '    instructions="TODO: what this MCP server exposes.",\n'
            "    list_tools=my_tool_lister,\n"
            "    run_tool=my_tool_runner,\n"
            ")",
            "# Exact route, never app.mount() — MCP clients don't survive the "
            "trailing-slash redirect a Mount can trigger.",
            "app.router.routes.append(Route(\"/mcp\", endpoint=asas_mcp_app, "
            'methods=["POST", "GET", "DELETE"]))',
            "# TODO: enter asas_mcp_lifespan() inside your app's own lifespan "
            "(merge with any existing one)",
        ),
    ),
}


def get(key: str) -> BootSnippet:
    return SNIPPETS[key]
