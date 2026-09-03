import pytest
import tomlkit

from asas_cli.pyproject_edit import add_dependency
from asas_cli.registry import resolve

RATELIMIT = resolve("ratelimit")
LOOKUPS = resolve("lookups")


def _write(tmp_path, text):
    path = tmp_path / "pyproject.toml"
    path.write_text(text)
    return path


def test_add_to_project_with_no_dependencies_array(tmp_path):
    path = _write(tmp_path, '[project]\nname = "demo"\nversion = "0.1.0"\n')
    outcome = add_dependency(path, RATELIMIT, "v0.11.0")
    assert outcome == "added"
    doc = tomlkit.parse(path.read_text())
    assert any("asas-ratelimit" in d for d in doc["project"]["dependencies"])


def test_add_appends_without_disturbing_existing_deps_or_comments(tmp_path):
    original = (
        "[project]\n"
        'name = "demo"\n'
        "# keep this comment\n"
        'dependencies = [\n    "fastapi>=0.110,<1",\n]\n'
    )
    path = _write(tmp_path, original)
    add_dependency(path, RATELIMIT, "v0.11.0")
    result = path.read_text()
    assert "# keep this comment" in result
    assert "fastapi>=0.110,<1" in result
    assert "asas-ratelimit" in result


def test_add_twice_is_idempotent_not_duplicated(tmp_path):
    path = _write(tmp_path, '[project]\nname = "demo"\ndependencies = []\n')
    add_dependency(path, RATELIMIT, "v0.11.0")
    before = path.stat().st_mtime_ns

    outcome = add_dependency(path, RATELIMIT, "v0.11.0")

    assert outcome == "unchanged"
    assert path.stat().st_mtime_ns == before  # byte-identical → not rewritten
    doc = tomlkit.parse(path.read_text())
    matches = [d for d in doc["project"]["dependencies"] if "asas-ratelimit" in d]
    assert len(matches) == 1


def test_add_recognizes_pep503_equivalent_spellings(tmp_path):
    # pip treats asas_ratelimit / Asas-Ratelimit / asas-ratelimit as one
    # distribution — a second line for any spelling would make the next
    # `pip install -e .` fail with a double requirement.
    original = (
        "[project]\n"
        'name = "demo"\n'
        'dependencies = [\n'
        '    "asas_ratelimit @ git+https://example.invalid@asas-ratelimit/v0.10.0#subdirectory=packages/asas-ratelimit",\n'
        "]\n"
    )
    path = _write(tmp_path, original)

    outcome = add_dependency(path, RATELIMIT, "v0.11.0")

    assert outcome == "updated"
    deps = list(tomlkit.parse(path.read_text())["project"]["dependencies"])
    assert len(deps) == 1
    assert "asas-ratelimit/v0.11.0" in deps[0]


def test_add_with_newer_tag_updates_existing_pin(tmp_path):
    path = _write(tmp_path, '[project]\nname = "demo"\ndependencies = []\n')
    add_dependency(path, RATELIMIT, "v0.10.5")
    add_dependency(path, RATELIMIT, "v0.11.0")
    doc = tomlkit.parse(path.read_text())
    deps = list(doc["project"]["dependencies"])
    assert len(deps) == 1
    assert "v0.11.0" in deps[0]
    assert "v0.10.5" not in deps[0]


def test_add_two_different_packages_both_present(tmp_path):
    path = _write(tmp_path, '[project]\nname = "demo"\ndependencies = []\n')
    add_dependency(path, RATELIMIT, "v0.11.0")
    add_dependency(path, LOOKUPS, "v0.11.0")
    doc = tomlkit.parse(path.read_text())
    deps = list(doc["project"]["dependencies"])
    assert any("asas-ratelimit" in d for d in deps)
    assert any("asas-lookups" in d for d in deps)
    assert len(deps) == 2


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        add_dependency(tmp_path / "nope.toml", RATELIMIT, "v0.11.0")


def test_no_project_table_raises(tmp_path):
    path = _write(tmp_path, '[tool.poetry]\nname = "demo"\n')
    with pytest.raises(KeyError, match="PEP 621"):
        add_dependency(path, RATELIMIT, "v0.11.0")
