"""Which Asas packages exist and how to install each one.

Source of truth for *installation* only (dist name, import name, subdirectory).
Wiring/boot behaviour lives in :mod:`asas_cli.templates`, generated from each
package's own ``__init__.py`` docstring (the Asas host contract). Keep both
in sync when a package's public surface changes — this registry describes
the contract for scaffolding purposes; it does not enforce it.
"""

from __future__ import annotations

from dataclasses import dataclass

REPO_URL = "https://github.com/wlootah-a11y/asas.git"


@dataclass(frozen=True)
class PackageSpec:
    key: str  # short alias used on the CLI, e.g. "lookups"
    dist_name: str  # e.g. "asas-lookups"
    import_name: str  # e.g. "asas_lookups"
    subdir: str  # e.g. "packages/asas-lookups"
    summary: str
    variant: str  # short label shown by `asas list`, e.g. "table-owning + router"


_SPECS = [
    PackageSpec(
        "lookups",
        "asas-lookups",
        "asas_lookups",
        "packages/asas-lookups",
        "Bilingual reference-data lookups (types/values/aliases) + read+admin API.",
        "table-owning + router",
    ),
    PackageSpec(
        "validation",
        "asas-validation",
        "asas_validation",
        "packages/asas-validation",
        "Declarative cross-field/temporal validation rules + 422 envelopes.",
        "table-less + router",
    ),
    PackageSpec(
        "storage",
        "asas-storage",
        "asas_storage",
        "packages/asas-storage",
        "Pluggable object storage (local/S3/Azure Blob) behind one seam.",
        "table-less, router-less",
    ),
    PackageSpec(
        "ratelimit",
        "asas-ratelimit",
        "asas_ratelimit",
        "packages/asas-ratelimit",
        "In-process token-bucket rate limiting with FastAPI-native 429s.",
        "table-less, router-less",
    ),
    PackageSpec(
        "jobs",
        "asas-jobs",
        "asas_jobs",
        "packages/asas-jobs",
        "DB-backed background job queue + interval scheduler, no broker.",
        "table-owning, router-less",
    ),
    PackageSpec(
        "access",
        "asas-access",
        "asas_access",
        "packages/asas-access",
        "Configuration-driven field/action permissions + mandatory access control.",
        "table-owning, router-less",
    ),
    PackageSpec(
        "workflow",
        "asas-workflow",
        "asas_workflow",
        "packages/asas-workflow",
        "Graph-based process/approval workflow engine.",
        "table-owning, router-less",
    ),
    PackageSpec(
        "notifications",
        "asas-notifications",
        "asas_notifications",
        "packages/asas-notifications",
        "Notification engine with an in-app feed + per-channel delivery outbox.",
        "table-owning + router",
    ),
    PackageSpec(
        "search",
        "asas-search",
        "asas_search",
        "packages/asas-search",
        "Cross-entity search with an optional Postgres deep-content/semantic tier.",
        "table-owning (dialect-branched), router-less",
    ),
    PackageSpec(
        "mcp",
        "asas-mcp",
        "asas_mcp",
        "packages/asas-mcp",
        "Remote MCP server core (exposes the host to AI clients over MCP).",
        "protocol-only",
    ),
]

PACKAGES: dict[str, PackageSpec] = {spec.key: spec for spec in _SPECS}
_DIST_TO_KEY: dict[str, str] = {spec.dist_name: spec.key for spec in _SPECS}


def resolve(name: str) -> PackageSpec:
    """Look up a package by its short key ('lookups') or full dist name
    ('asas-lookups'). Raises KeyError (with the valid choices) otherwise."""
    if name in PACKAGES:
        return PACKAGES[name]
    if name in _DIST_TO_KEY:
        return PACKAGES[_DIST_TO_KEY[name]]
    raise KeyError(
        f"unknown Asas package {name!r} — known: {', '.join(sorted(PACKAGES))}"
    )


def dependency_string(spec: PackageSpec, version_tag: str) -> str:
    """The PEP 508 direct-URL dependency line pip/uv install from — no
    package index involved, straight from the git tag.

    `version_tag` is this package's own version suffix (e.g. ``"v0.11.0"``,
    as returned by ``git_tags.latest_tags()``/``FALLBACK_TAGS`` — never a
    full ref). Each package tags independently since RELEASING.md
    (2026-08-25): the real git tag is ``<dist_name>/<version_tag>``, e.g.
    ``asas-lookups/v0.11.0`` — there is no shared repo-wide tag anymore."""
    ref = f"{spec.dist_name}/{version_tag}"
    return f"{spec.dist_name} @ git+{REPO_URL}@{ref}#subdirectory={spec.subdir}"
