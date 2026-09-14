#!/usr/bin/env python3
"""Release discipline, producer side: a PR that changes a package's shipped
code must bump that package's version. Merged-to-main therefore always equals
release-ready, and `asas release <pkg>` is the only remaining step.

Shipped code = src/** and pyproject.toml. Tests, README, and CHANGELOG edits
alone do not force a bump. Run in CI against the merge base:

    python scripts/check_version_bumps.py origin/main
"""

import re
import subprocess
import sys
from pathlib import Path

BUMP_PATHS = ("src/", "pyproject.toml")


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def _version_at(ref: str, pkg: str) -> str | None:
    try:
        text = _git("show", f"{ref}:packages/{pkg}/pyproject.toml")
    except subprocess.CalledProcessError:
        return None  # package is new in this PR — nothing to compare against
    match = re.search(r'^version = "([^"]+)"', text, re.M)
    return match.group(1) if match else None


def main(base: str) -> int:
    merge_base = _git("merge-base", base, "HEAD").strip()
    changed = _git("diff", "--name-only", merge_base, "HEAD").splitlines()
    needs_bump = set()
    for path in changed:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "packages":
            rest = "/".join(parts[2:])
            if rest.startswith(BUMP_PATHS[0]) or rest == BUMP_PATHS[1]:
                needs_bump.add(parts[1])
    failures = []
    for pkg in sorted(needs_bump):
        before = _version_at(merge_base, pkg)
        after = _version_at("HEAD", pkg)
        if before is not None and before == after:
            failures.append(f"{pkg}: shipped code changed but version stayed {after}")
    if failures:
        print("Release discipline: every change to a package's src/ or "
              "pyproject.toml ships as a release, so it must bump the version "
              "(patch for fixes/additions, minor for breaking pre-1.0):\n")
        print("\n".join("  - " + f for f in failures))
        return 1
    print("release discipline: ok" + (f" ({', '.join(sorted(needs_bump))} bumped)" if needs_bump else " (no shipped-code changes)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "origin/main"))
