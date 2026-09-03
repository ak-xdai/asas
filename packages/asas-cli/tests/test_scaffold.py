import subprocess

import pytest
import tomlkit

from asas_cli.registry import PACKAGES, resolve
from asas_cli.scaffold import scaffold


def _versions_for(*keys):
    """Explicit per-package version overrides — every selected package
    covered, so scaffold() never needs a live/mocked git call."""
    return {resolve(k).dist_name: "v0.11.0" for k in keys}


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    # Belt-and-suspenders: every test below supplies `versions=` covering
    # every selected package, so latest_tags() should never actually run —
    # this fixture just guarantees that if it somehow did, it wouldn't hit
    # the real network from inside a test.
    def _fail(*args, **kwargs):
        raise AssertionError("scaffold() hit the network — a test is missing a versions= override")

    monkeypatch.setattr(subprocess, "run", _fail)


def test_scaffold_creates_expected_files(tmp_path):
    project_dir = tmp_path / "demo"
    created = scaffold(
        project_dir, "demo", ["lookups", "ratelimit"], versions=_versions_for("lookups", "ratelimit")
    )

    names = {p.name for p in created}
    assert names == {
        "main.py", "settings.py", "pyproject.toml", "README.md", ".env.example", "test_smoke.py",
    }
    for path in created:
        assert path.exists()
    assert (project_dir / "tests" / "test_smoke.py").exists()


def test_pyproject_declares_the_dev_extra_the_readme_installs(tmp_path):
    # Regression: every next-step in the generated README and the CLI's own
    # hint says `pip install -e '.[dev]'` — the extra has to actually exist,
    # with pytest (to run tests/) and httpx (fastapi.testclient needs it).
    project_dir = tmp_path / "demo"
    scaffold(project_dir, "demo", ["ratelimit"], versions=_versions_for("ratelimit"))
    doc = tomlkit.parse((project_dir / "pyproject.toml").read_text())
    dev = "\n".join(doc["project"]["optional-dependencies"]["dev"])
    assert "pytest" in dev
    assert "httpx" in dev
    compile((project_dir / "tests" / "test_smoke.py").read_text(), "test_smoke.py", "exec")


def test_refuses_to_scaffold_over_an_existing_file(tmp_path):
    # Regression: a plain file at the target used to escape as
    # NotADirectoryError from iterdir(), which the CLI doesn't catch.
    target = tmp_path / "demo"
    target.write_text("I am a file, not a directory")

    with pytest.raises(FileExistsError):
        scaffold(target, "demo", ["lookups"], versions=_versions_for("lookups"))

    assert target.read_text() == "I am a file, not a directory"


def test_refuses_to_scaffold_into_nonempty_dir(tmp_path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    (project_dir / "existing.txt").write_text("don't touch me")

    with pytest.raises(FileExistsError):
        scaffold(project_dir, "demo", ["lookups"], versions=_versions_for("lookups"))

    assert (project_dir / "existing.txt").read_text() == "don't touch me"


@pytest.mark.parametrize("key", sorted(PACKAGES))
def test_generated_main_py_is_syntactically_valid_for_every_package(tmp_path, key):
    project_dir = tmp_path / key
    scaffold(project_dir, "demo", [key], versions=_versions_for(key))
    compile((project_dir / "main.py").read_text(), "main.py", "exec")
    compile((project_dir / "settings.py").read_text(), "settings.py", "exec")


def test_generated_main_py_is_valid_for_all_packages_combined(tmp_path):
    project_dir = tmp_path / "everything"
    scaffold(project_dir, "demo", sorted(PACKAGES), versions=_versions_for(*sorted(PACKAGES)))
    compile((project_dir / "main.py").read_text(), "main.py", "exec")


def test_pyproject_toml_pins_selected_packages_each_at_its_own_tag(tmp_path):
    project_dir = tmp_path / "demo"
    scaffold(
        project_dir, "demo", ["lookups", "ratelimit"],
        versions={"asas-lookups": "v0.11.0", "asas-ratelimit": "v0.10.5"},
    )
    doc = tomlkit.parse((project_dir / "pyproject.toml").read_text())
    deps = "\n".join(doc["project"]["dependencies"])
    assert "asas-lookups/v0.11.0" in deps
    assert "asas-ratelimit/v0.10.5" in deps
    # never a shared repo-wide tag across the two
    assert "asas-lookups/v0.10.5" not in deps
    assert "asas-ratelimit/v0.11.0" not in deps


def test_pyproject_toml_declares_py_modules_so_pip_install_e_works(tmp_path):
    # Regression: a flat main.py + settings.py layout with no [tool.setuptools]
    # py-modules makes setuptools' auto-discovery refuse to guess which of the
    # two top-level modules is "the" package — `pip install -e .` fails
    # outright with "Multiple top-level modules discovered in a flat-layout".
    project_dir = tmp_path / "demo"
    scaffold(project_dir, "demo", ["ratelimit"], versions=_versions_for("ratelimit"))
    text = (project_dir / "pyproject.toml").read_text()
    doc = tomlkit.parse(text)
    assert doc["tool"]["setuptools"]["py-modules"] == ["main", "settings"]


def test_settings_py_includes_ratelimit_fields_only_when_selected(tmp_path):
    with_rl = tmp_path / "with_rl"
    without_rl = tmp_path / "without_rl"
    scaffold(with_rl, "demo", ["ratelimit"], versions=_versions_for("ratelimit"))
    scaffold(without_rl, "demo", ["lookups"], versions=_versions_for("lookups"))

    assert "rate_limit_enabled" in (with_rl / "settings.py").read_text()
    assert "rate_limit_enabled" not in (without_rl / "settings.py").read_text()


def test_mcp_project_name_is_substituted_not_left_literal(tmp_path):
    project_dir = tmp_path / "myservice"
    scaffold(project_dir, "myservice", ["mcp"], versions=_versions_for("mcp"))
    main_py = (project_dir / "main.py").read_text()
    assert 'name="myservice"' in main_py
    assert "{project_name}" not in main_py


def test_readme_lists_wired_packages_with_their_own_tags(tmp_path):
    project_dir = tmp_path / "demo"
    scaffold(project_dir, "demo", ["lookups"], versions=_versions_for("lookups"))
    readme = (project_dir / "README.md").read_text()
    assert "asas-lookups" in readme
    assert "v0.11.0" in readme


def test_scaffold_resolves_live_when_no_override_given(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout="abc\trefs/tags/asas-ratelimit/v0.12.0\n", stderr=""
        ),
    )
    project_dir = tmp_path / "demo"
    scaffold(project_dir, "demo", ["ratelimit"])
    doc = tomlkit.parse((project_dir / "pyproject.toml").read_text())
    assert any("asas-ratelimit/v0.12.0" in d for d in doc["project"]["dependencies"])
