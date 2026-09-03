import subprocess

import pytest
import tomlkit

from asas_cli.cli import main

_LS_REMOTE_OUTPUT = (
    "abc\trefs/tags/asas-lookups/v0.11.0\n"
    "def\trefs/tags/asas-ratelimit/v0.11.0\n"
)


@pytest.fixture(autouse=True)
def _fake_remote(monkeypatch):
    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=_LS_REMOTE_OUTPUT, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)


def test_list_command_runs_clean(capsys):
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "lookups" in out
    assert "asas-ratelimit" not in out  # lists short keys, not dist names


def test_add_command_writes_pin_at_latest_by_default(tmp_path, capsys):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\ndependencies = []\n')

    rc = main(["add", "ratelimit", "--path", str(path)])

    assert rc == 0
    assert "added asas-ratelimit @ asas-ratelimit/v0.11.0" in capsys.readouterr().out
    doc = tomlkit.parse(path.read_text())
    assert any("asas-ratelimit/v0.11.0" in d for d in doc["project"]["dependencies"])


def test_add_command_respects_explicit_version(tmp_path, capsys):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\ndependencies = []\n')

    rc = main(["add", "ratelimit", "--version", "0.9.0", "--path", str(path)])

    assert rc == 0
    doc = tomlkit.parse(path.read_text())
    deps = "\n".join(doc["project"]["dependencies"])
    assert "asas-ratelimit/v0.9.0" in deps
    assert "asas-ratelimit/v0.11.0" not in deps  # the live tag was never consulted


def test_add_unknown_package_fails_cleanly(tmp_path, capsys):
    path = tmp_path / "pyproject.toml"
    path.write_text('[project]\nname = "demo"\ndependencies = []\n')

    rc = main(["add", "nope", "--path", str(path)])

    assert rc == 1
    assert "unknown Asas package" in capsys.readouterr().err


def test_new_command_scaffolds_project_with_per_package_tags(tmp_path, capsys):
    rc = main(["new", "demo", "--with", "lookups,ratelimit", "--dir", str(tmp_path)])

    assert rc == 0
    main_py = (tmp_path / "demo" / "main.py").read_text()
    assert "import asas_lookups" in main_py and "import asas_ratelimit" in main_py
    doc = tomlkit.parse((tmp_path / "demo" / "pyproject.toml").read_text())
    deps = "\n".join(doc["project"]["dependencies"])
    assert "asas-lookups/v0.11.0" in deps
    assert "asas-ratelimit/v0.11.0" in deps
    assert "scaffolded demo" in capsys.readouterr().out


def test_new_command_rejects_unknown_package(tmp_path, capsys):
    rc = main(["new", "demo", "--with", "lookups,nope", "--dir", str(tmp_path)])

    assert rc == 1
    assert "unknown Asas package" in capsys.readouterr().err
    assert not (tmp_path / "demo").exists()


def test_new_command_accepts_dist_names_like_add_does(tmp_path):
    # `asas add asas-lookups` works, so `asas new --with asas-lookups` must too
    # — both go through registry.resolve() now.
    rc = main(["new", "demo", "--with", "asas-lookups", "--dir", str(tmp_path)])

    assert rc == 0
    assert "import asas_lookups" in (tmp_path / "demo" / "main.py").read_text()


def test_new_command_dedupes_repeated_package_selections(tmp_path):
    # `lookups,asas-lookups` resolves to one package — the generated project
    # must not carry a duplicate dependency or double wiring.
    rc = main(["new", "demo", "--with", "lookups,asas-lookups,lookups", "--dir", str(tmp_path)])

    assert rc == 0
    doc = tomlkit.parse((tmp_path / "demo" / "pyproject.toml").read_text())
    lookups_deps = [d for d in doc["project"]["dependencies"] if "asas-lookups" in d]
    assert len(lookups_deps) == 1


def test_new_command_rejects_a_file_as_dir(tmp_path, capsys):
    target = tmp_path / "not-a-dir"
    target.write_text("plain file")

    rc = main(["new", "demo", "--with", "lookups", "--dir", str(target)])

    assert rc == 1
    assert "is not a directory" in capsys.readouterr().err


def test_new_command_rejects_a_path_as_project_name(tmp_path, capsys):
    # The name lands in the generated `[project] name`; a path would scaffold
    # a project pip refuses to install.
    rc = main(["new", str(tmp_path / "demo"), "--with", "lookups", "--dir", str(tmp_path)])

    assert rc == 1
    assert "not a valid project name" in capsys.readouterr().err


def test_new_command_refuses_existing_file_at_target(tmp_path, capsys):
    (tmp_path / "demo").write_text("a file, not a directory")

    rc = main(["new", "demo", "--with", "lookups", "--dir", str(tmp_path)])

    assert rc == 1
    assert "already exists" in capsys.readouterr().err


def test_add_bad_path_fails_before_any_tag_resolution(tmp_path, capsys, monkeypatch):
    import asas_cli.cli as cli_mod

    def _fail(*args, **kwargs):
        raise AssertionError("latest_tag ran before the local path check")

    monkeypatch.setattr(cli_mod, "latest_tag", _fail)

    rc = main(["add", "ratelimit", "--path", str(tmp_path / "nope.toml")])

    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_new_command_refuses_existing_nonempty_dir(tmp_path, capsys):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    (project_dir / "keep.txt").write_text("keep")

    rc = main(["new", "demo", "--with", "lookups", "--dir", str(tmp_path)])

    assert rc == 1
    assert "already exists" in capsys.readouterr().err
    assert (project_dir / "keep.txt").exists()
