"""asas outdated: pin parsing, drift tiers, advisory gating (RELEASING.md,
"The consumer contract")."""

import json

from asas_cli.updates import PinStatus, _tier, check_pins, outdated, parse_pins

PYPROJECT = """
[project]
dependencies = [
  "fastapi",
  "asas-validation @ git+https://github.com/wlootah-a11y/asas.git@asas-validation/v0.11.0#subdirectory=packages/asas-validation",
  "asas-lookups @ git+https://github.com/wlootah-a11y/asas.git@asas-lookups/v0.13.2#subdirectory=packages/asas-lookups",
]
"""


def test_parse_pins_finds_only_asas_tag_pins():
    assert parse_pins(PYPROJECT) == {
        "asas-validation": "v0.11.0",
        "asas-lookups": "v0.13.2",
    }


def test_tiers():
    assert _tier("v0.12.0", "v0.12.0") == "current"
    assert _tier("v0.12.0", "v0.12.3") == "patch-behind"
    assert _tier("v0.11.2", "v0.12.0") == "minor-behind"
    assert _tier("v0.13.0", "v0.12.9") == "current"  # ahead is not behind


def _project(tmp_path, text=PYPROJECT):
    path = tmp_path / "pyproject.toml"
    path.write_text(text)
    return path


def _advisories(tmp_path, entries):
    path = tmp_path / "ADVISORIES.json"
    path.write_text(json.dumps({"advisories": entries}))
    return path.as_uri()


def test_outdated_exit_codes(tmp_path, monkeypatch):
    import asas_cli.updates as updates

    monkeypatch.setattr(
        updates, "latest_tags",
        lambda names, url: {"asas-validation": "v0.12.0", "asas-lookups": "v0.13.2"},
    )
    project = _project(tmp_path)

    # minor-behind: informational normally, failing in CI
    quiet = _advisories(tmp_path, [])
    assert outdated(project, ci=False, advisories_url=quiet) == 0
    assert outdated(project, ci=True, advisories_url=quiet) == 1

    # an advisory on the pinned version is mandatory in any mode
    loud = _advisories(tmp_path, [{
        "package": "asas-lookups", "fixed_in": "0.13.3",
        "note": "seed dedupe writes cross-tenant rows",
    }])
    assert outdated(project, ci=False, advisories_url=loud) == 2


def test_advisory_does_not_fire_once_fixed_version_is_pinned(tmp_path, monkeypatch):
    import asas_cli.updates as updates

    monkeypatch.setattr(
        updates, "latest_tags",
        lambda names, url: {"asas-validation": "v0.12.0", "asas-lookups": "v0.13.2"},
    )
    project = _project(tmp_path)
    url = _advisories(tmp_path, [{"package": "asas-lookups", "fixed_in": "0.13.0"}])
    statuses = check_pins(project, advisories_url=url)
    lookups = next(s for s in statuses if s.dist_name == "asas-lookups")
    assert lookups.advisory is None  # pinned 0.13.2 >= fixed_in 0.13.0


def test_unreachable_advisories_never_fail_the_build(tmp_path, monkeypatch, capsys):
    import asas_cli.updates as updates

    monkeypatch.setattr(updates, "latest_tags",
                        lambda names, url: {"asas-validation": "v0.11.0",
                                            "asas-lookups": "v0.13.2"})
    project = _project(tmp_path)
    code = outdated(project, ci=False,
                    advisories_url=(tmp_path / "missing.json").as_uri())
    assert code == 0
    assert "could not fetch advisories" in capsys.readouterr().err
