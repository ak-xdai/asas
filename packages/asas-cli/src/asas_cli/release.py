"""The producer side of the release discipline: `asas release <package>`.

Merged-to-main is release-READY (CI refuses a package change without a
version bump); this command makes it RELEASED: verify the three version
declarations agree, verify the tag is new, cut the annotated tag, push it.
One command, so cutting a release is never the step that gets skipped."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from .registry import PackageSpec, resolve


def _pyproject_version(pkg_dir: Path) -> str:
    match = re.search(r'^version = "([^"]+)"', (pkg_dir / "pyproject.toml").read_text(), re.M)
    if not match:
        raise SystemExit(f"no version in {pkg_dir}/pyproject.toml")
    return match.group(1)


def _dunder_version(pkg_dir: Path, import_name: str) -> str:
    init = pkg_dir / "src" / import_name / "__init__.py"
    match = re.search(r'^__version__ = "([^"]+)"', init.read_text(), re.M)
    if not match:
        raise SystemExit(f"no __version__ in {init}")
    return match.group(1)


def _changelog_version(pkg_dir: Path) -> str:
    match = re.search(r"^## (\d+\.\d+\.\d+)", (pkg_dir / "CHANGELOG.md").read_text(), re.M)
    if not match:
        raise SystemExit(f"no '## <version>' heading in {pkg_dir}/CHANGELOG.md")
    return match.group(1)


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo_root), *args],
                          capture_output=True, text=True)


def cut_release(repo_root: Path, spec: PackageSpec, *, push: bool = True) -> str:
    """Verify agreement, tag ``<dist>/v<version>`` on HEAD, optionally push.
    Returns the tag name."""
    pkg_dir = repo_root / spec.subdir
    versions = {
        "pyproject.toml": _pyproject_version(pkg_dir),
        "__init__.__version__": _dunder_version(pkg_dir, spec.import_name),
        "CHANGELOG.md": _changelog_version(pkg_dir),
    }
    if len(set(versions.values())) != 1:
        raise SystemExit(f"{spec.dist_name} disagrees with itself: {versions} — "
                         "finish the bump before releasing")
    version = versions["pyproject.toml"]
    tag = f"{spec.dist_name}/v{version}"
    if _git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}").returncode == 0:
        raise SystemExit(f"tag {tag} already exists — bump the version first")
    remote = _git(repo_root, "ls-remote", "--tags", "origin", tag)
    if remote.returncode == 0 and remote.stdout.strip():
        raise SystemExit(f"tag {tag} already exists on origin — bump the version first")
    made = _git(repo_root, "tag", "-a", tag, "-m",
                f"{spec.dist_name} {version} (see {spec.subdir}/CHANGELOG.md)")
    if made.returncode != 0:
        raise SystemExit(f"git tag failed: {made.stderr.strip()}")
    if push:
        pushed = _git(repo_root, "push", "origin", tag)
        if pushed.returncode != 0:
            raise SystemExit(f"tag created locally but push failed: {pushed.stderr.strip()}")
    print(f"released {tag}" + ("" if push else " (not pushed: --no-push)"))
    return tag


def release(name: str, *, push: bool = True, repo_root: Path | None = None) -> str:
    root = repo_root or Path(
        subprocess.run(["git", "rev-parse", "--show-toplevel"],
                       capture_output=True, text=True).stdout.strip()
    )
    return cut_release(root, resolve(name), push=push)
