"""asas release: three-way agreement enforced, tags cut once."""

import subprocess
from pathlib import Path

import pytest

from asas_cli.registry import resolve
from asas_cli.release import cut_release


def _fake_repo(tmp_path, version="0.5.0", changelog=None, dunder=None):
    root = tmp_path / "repo"
    spec = resolve("lookups")
    pkg = root / spec.subdir
    (pkg / "src" / spec.import_name).mkdir(parents=True)
    (pkg / "pyproject.toml").write_text(f'[project]\nname = "{spec.dist_name}"\nversion = "{version}"\n')
    (pkg / "src" / spec.import_name / "__init__.py").write_text(
        f'__version__ = "{dunder or version}"\n')
    (pkg / "CHANGELOG.md").write_text(f"# Changelog\n\n## {changelog or version} — today\n")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "-qm", "seed"], check=True)
    return root, spec


def test_release_tags_the_agreed_version(tmp_path):
    root, spec = _fake_repo(tmp_path)
    tag = cut_release(root, spec, push=False)
    assert tag == "asas-lookups/v0.5.0"
    out = subprocess.run(["git", "-C", str(root), "tag"], capture_output=True, text=True)
    assert "asas-lookups/v0.5.0" in out.stdout


def test_release_refuses_a_half_finished_bump(tmp_path):
    root, spec = _fake_repo(tmp_path, version="0.5.1", changelog="0.5.0")
    with pytest.raises(SystemExit, match="disagrees with itself"):
        cut_release(root, spec, push=False)


def test_release_refuses_an_existing_tag(tmp_path):
    root, spec = _fake_repo(tmp_path)
    cut_release(root, spec, push=False)
    with pytest.raises(SystemExit, match="already exists"):
        cut_release(root, spec, push=False)
