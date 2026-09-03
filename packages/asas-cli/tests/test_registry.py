import pytest

from asas_cli.registry import PACKAGES, dependency_string, resolve

EXPECTED_KEYS = {
    "lookups",
    "validation",
    "storage",
    "ratelimit",
    "jobs",
    "access",
    "workflow",
    "notifications",
    "search",
    "mcp",
}


def test_all_ten_packages_registered():
    assert set(PACKAGES) == EXPECTED_KEYS


def test_resolve_by_short_key():
    assert resolve("lookups").dist_name == "asas-lookups"


def test_resolve_by_dist_name():
    assert resolve("asas-lookups").key == "lookups"


def test_resolve_unknown_raises_with_choices():
    with pytest.raises(KeyError, match="unknown Asas package 'nope'"):
        resolve("nope")


def test_dependency_string_shape():
    # Since RELEASING.md (2026-08-25): the git ref is <dist_name>/<version_tag>,
    # never a shared repo-wide tag.
    spec = resolve("ratelimit")
    line = dependency_string(spec, "v0.11.0")
    assert line == (
        "asas-ratelimit @ git+https://github.com/wlootah-a11y/asas.git"
        "@asas-ratelimit/v0.11.0#subdirectory=packages/asas-ratelimit"
    )


def test_every_spec_subdir_matches_its_key():
    for key, spec in PACKAGES.items():
        assert spec.subdir == f"packages/{spec.dist_name}"
        assert spec.import_name == spec.dist_name.replace("-", "_")
