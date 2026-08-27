"""Idempotent edits to a project's `[project] dependencies` array — add or
update one Asas package's pin without disturbing anything else in the file
(comments, formatting, unrelated dependencies)."""

from __future__ import annotations

import re
from pathlib import Path

import tomlkit

from .registry import PackageSpec, dependency_string


def _canonical_name(name: str) -> str:
    """PEP 503 normalization: pip treats `asas_lookups`, `Asas-Lookups`, and
    `asas-lookups` as the same distribution, so recognition must too."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _dep_dist_name(dep_line: str) -> str:
    """The canonical package name a PEP 508 dependency string names, ignoring
    any version specifier, extras, or URL — enough to recognize "is this
    dependency line already pinning the same package"."""
    name = str(dep_line).strip()
    for sep in ("@", "==", ">=", "<=", "~=", "!=", ">", "<", ";", "["):
        name = name.split(sep, 1)[0]
    return _canonical_name(name.strip())


def add_dependency(pyproject_path: Path, spec: PackageSpec, tag: str) -> str:
    """Add or update `spec`'s pin (at `tag`) in `pyproject_path`.

    Returns ``"added"``, ``"updated"``, or ``"unchanged"`` (already pinned at
    exactly this tag — the file is not rewritten, so watchers keyed on its
    mtime don't retrigger). Raises ``FileNotFoundError`` if the file doesn't
    exist, and ``KeyError`` if it has no PEP 621 ``[project]`` table (e.g. a
    poetry-only ``pyproject.toml``)."""
    if not pyproject_path.exists():
        raise FileNotFoundError(pyproject_path)

    doc = tomlkit.parse(pyproject_path.read_text())
    if "project" not in doc:
        raise KeyError(
            f"{pyproject_path} has no [project] table — this needs a PEP 621 "
            "pyproject.toml (not a poetry-only [tool.poetry] one)."
        )

    project = doc["project"]
    deps = project.get("dependencies")
    if deps is None:
        deps = tomlkit.array()
        deps.multiline(True)
        project["dependencies"] = deps

    new_line = dependency_string(spec, tag)
    for i, existing in enumerate(deps):
        if _dep_dist_name(existing) == _canonical_name(spec.dist_name):
            if str(existing) == new_line:
                return "unchanged"
            deps[i] = new_line
            pyproject_path.write_text(tomlkit.dumps(doc))
            return "updated"

    deps.append(new_line)
    pyproject_path.write_text(tomlkit.dumps(doc))
    return "added"
