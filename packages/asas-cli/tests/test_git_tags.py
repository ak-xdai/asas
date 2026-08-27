import subprocess

import pytest

from asas_cli.git_tags import FALLBACK_TAGS, latest_tag, latest_tags
from asas_cli.registry import PACKAGES

_LS_REMOTE_OUTPUT = (
    "abc123\trefs/tags/asas-lookups/v0.10.0\n"
    "def456\trefs/tags/asas-lookups/v0.11.0\n"
    "aaa111\trefs/tags/asas-lookups/v0.11.0^{}\n"  # peeled annotated-tag ref, same tag
    "bbb222\trefs/tags/asas-ratelimit/v0.11.0\n"
    "zzz999\trefs/tags/v0.15.0\n"  # retired flat tag — must never match
    "yyy888\trefs/tags/not-a-version\n"
)


def _fake_run(output):
    def _run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    return _run


def test_latest_tags_picks_highest_semver_per_package(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(_LS_REMOTE_OUTPUT))
    result = latest_tags(["asas-lookups", "asas-ratelimit"])
    assert result == {"asas-lookups": "v0.11.0", "asas-ratelimit": "v0.11.0"}


def test_latest_tags_ignores_retired_flat_tags(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(_LS_REMOTE_OUTPUT))
    # asas-storage has no namespaced tag in the fixture output — must fall
    # back, never accidentally match the flat `refs/tags/v0.15.0`.
    result = latest_tags(["asas-storage"])
    assert result == {"asas-storage": FALLBACK_TAGS["asas-storage"]}


def test_latest_tags_one_remote_call_for_many_packages(monkeypatch):
    calls = []

    def _run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=_LS_REMOTE_OUTPUT, stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    latest_tags(["asas-lookups", "asas-ratelimit", "asas-storage", "asas-jobs"])
    assert len(calls) == 1


def test_latest_tags_falls_back_when_git_unavailable(monkeypatch, capsys):
    def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    result = latest_tags(["asas-lookups", "asas-jobs"])
    assert result == {"asas-lookups": FALLBACK_TAGS["asas-lookups"], "asas-jobs": FALLBACK_TAGS["asas-jobs"]}
    assert "could not reach" in capsys.readouterr().err


def test_latest_tags_falls_back_on_nonzero_exit(monkeypatch):
    def _raise(*args, **kwargs):
        raise subprocess.CalledProcessError(128, args)

    monkeypatch.setattr(subprocess, "run", _raise)
    assert latest_tags(["asas-lookups"]) == {"asas-lookups": FALLBACK_TAGS["asas-lookups"]}


def test_latest_tags_unknown_dist_with_no_fallback_raises(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(""))
    with pytest.raises(KeyError, match="no live tag found"):
        latest_tags(["asas-nonexistent"])


def test_latest_tags_empty_input_returns_empty(monkeypatch):
    calls = []

    def _run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _run)
    assert latest_tags([]) == {}
    assert calls == []  # nothing to resolve — no remote round trip either


def test_latest_tag_single_package_convenience_form(monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run(_LS_REMOTE_OUTPUT))
    assert latest_tag("asas-ratelimit") == "v0.11.0"


def test_every_installable_package_has_a_fallback():
    # Derived from the registry, not a second hand-typed list — so a package
    # added to _SPECS without a fallback fails here, not at a consumer's
    # first offline install.
    installable = {spec.dist_name for spec in PACKAGES.values()}
    assert set(FALLBACK_TAGS) == installable


def test_fallback_tags_are_all_valid_semver():
    for dist, tag in FALLBACK_TAGS.items():
        major, minor, patch = tag.lstrip("v").split(".")
        assert major.isdigit() and minor.isdigit() and patch.isdigit(), dist
