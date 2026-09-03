"""Resolve which per-package Asas tag `asas add`/`asas new` pin generated
dependency strings to, when the caller doesn't pass an explicit version.

Since `RELEASING.md` (2026-08-25) each package tags **independently** —
`asas-<pkg>/vX.Y.Z` — there is no repo-wide tag to resolve against anymore.
The old flat `vX.Y.Z` scheme is retired (kept only for decoding old pins);
every function here is scoped to one dist name at a time, batched into a
single remote round trip when resolving several."""

from __future__ import annotations

import re
import subprocess
import sys

from .registry import REPO_URL

# refs/tags/<dist_name>/vX.Y.Z
_TAG_RE = re.compile(r"refs/tags/([a-z0-9-]+)/(v\d+\.\d+\.\d+)$")

# Bumped whenever a package cuts a release — a step in RELEASING.md's
# checklist, and tests/test_version_consistency.py at the repo root fails if
# an entry disagrees with that package's pyproject.toml. Used only as a last
# resort — offline, no git on PATH, or the remote is unreachable — so
# `asas add`/`asas new` still work without a network, just possibly pinned to
# a tag that's no longer that package's newest. An explicit --version always
# wins over both this and live discovery.
FALLBACK_TAGS: dict[str, str] = {
    "asas-access": "v0.15.0",
    "asas-jobs": "v0.11.0",
    "asas-lookups": "v0.13.2",
    "asas-mcp": "v0.11.1",
    "asas-notifications": "v0.16.0",
    "asas-ratelimit": "v0.11.0",
    "asas-search": "v0.11.1",
    "asas-storage": "v0.15.0",
    "asas-validation": "v0.11.0",
    "asas-workflow": "v0.11.2",
}


def _semver_key(version_tag: str) -> tuple[int, int, int]:
    major, minor, patch = version_tag.lstrip("v").split(".")
    return (int(major), int(minor), int(patch))


def _ls_remote_tag_refs(repo_url: str, timeout: float) -> list[str] | None:
    """Raw (de-peeled) `refs/tags/...` ref strings from the remote, or None
    if the remote couldn't be reached at all."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", repo_url],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
    except Exception as exc:  # noqa: BLE001 — any failure here is a soft fallback
        print(f"asas: could not reach {repo_url} ({exc}).", file=sys.stderr)
        return None

    refs = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        ref = parts[1]
        if ref.endswith("^{}"):  # peeled annotated-tag ref — same tag name
            ref = ref[:-3]
        refs.append(ref)
    return refs


def latest_tags(
    dist_names, repo_url: str = REPO_URL, *, timeout: float = 5.0
) -> dict[str, str]:
    """The highest `vX.Y.Z` version tag for each of `dist_names`, e.g.
    ``{"asas-lookups": "v0.11.0"}`` — one remote round trip regardless of how
    many names are asked for. Falls back per-package to FALLBACK_TAGS (with a
    stderr warning) for any name live discovery didn't resolve; raises
    KeyError only if a name has neither a live tag nor a known fallback."""
    wanted = set(dist_names)
    if not wanted:  # nothing to resolve — don't pay the remote round trip
        return {}
    resolved: dict[str, str] = {}

    refs = _ls_remote_tag_refs(repo_url, timeout)
    if refs is not None:
        by_dist: dict[str, set[str]] = {}
        for ref in refs:
            match = _TAG_RE.search(ref)
            if match and match.group(1) in wanted:
                by_dist.setdefault(match.group(1), set()).add(match.group(2))
        for dist, versions in by_dist.items():
            resolved[dist] = max(versions, key=_semver_key)

    for dist in wanted - set(resolved):
        fallback = FALLBACK_TAGS.get(dist)
        if fallback is None:
            raise KeyError(
                f"no live tag found for {dist!r} and no fallback known — "
                "pass --version explicitly"
            )
        print(
            f"asas: could not resolve a live tag for {dist}; falling back to "
            f"{fallback}. Pass --version to pin explicitly.",
            file=sys.stderr,
        )
        resolved[dist] = fallback

    return resolved


def latest_tag(dist_name: str, repo_url: str = REPO_URL, *, timeout: float = 5.0) -> str:
    """Single-package convenience form of latest_tags()."""
    return latest_tags([dist_name], repo_url, timeout=timeout)[dist_name]
