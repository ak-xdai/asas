"""A package states its version in three places; all three must agree.

`pyproject.toml` is what pip resolves and what a lockfile records,
`__version__` is what a running process reports, and the newest `CHANGELOG.md`
heading is what a human reads before upgrading. A release that updates two of
the three ships a package that misreports itself — and the misreport is only
visible to whoever hits it.

This is the guard on the per-package tag scheme (see RELEASING.md). Under
lockstep it did not matter much, because the repo tag was the only number anyone
trusted. Now the tag *is* the package version, so these three have to be one
fact rather than three copies of it.
"""

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PACKAGES = sorted(p for p in (ROOT / "packages").iterdir() if p.is_dir())


def _pyproject_version(pkg: pathlib.Path) -> str:
    text = (pkg / "pyproject.toml").read_text()
    return re.search(r'^version = "([^"]+)"', text, re.M).group(1)


def _dunder_version(pkg: pathlib.Path) -> str:
    init = pkg / "src" / pkg.name.replace("-", "_") / "__init__.py"
    return re.search(r'^__version__ = "([^"]+)"', init.read_text(), re.M).group(1)


def _changelog_version(pkg: pathlib.Path) -> str:
    changelog = pkg / "CHANGELOG.md"
    if not changelog.exists():
        pytest.fail(f"{pkg.name} has no CHANGELOG.md (see RELEASING.md)")
    match = re.search(r"^## (\d+\.\d+\.\d+)", changelog.read_text(), re.M)
    if match is None:
        pytest.fail(f"{pkg.name}/CHANGELOG.md has no '## <version>' heading")
    return match.group(1)


def test_every_package_is_checked():
    """Eleven packages (the original ten plus asas-cli, added deliberately here
    so a twelfth can't slip past this file unnoticed)."""
    assert len(PACKAGES) == 11, [p.name for p in PACKAGES]


@pytest.mark.parametrize("pkg", PACKAGES, ids=lambda p: p.name)
def test_version_agrees_across_all_three(pkg):
    pyproject, dunder, changelog = (
        _pyproject_version(pkg), _dunder_version(pkg), _changelog_version(pkg),
    )
    assert pyproject == dunder == changelog, (
        f"{pkg.name} disagrees with itself: pyproject.toml={pyproject}, "
        f"__version__={dunder}, newest CHANGELOG heading={changelog}. "
        f"A release updates all three (RELEASING.md)."
    )


# ── asas-cli's copies of the package facts ──────────────────────────────────
#
# The CLI ships two hand-maintained snapshots of this repository: which
# packages exist (registry._SPECS) and each one's newest release tag
# (git_tags.FALLBACK_TAGS, the offline pin). Both are read here as source
# text — same rule as above, no imports — so a release or a new package that
# forgets the CLI fails on the PR that forgot it, not on some consumer's
# offline install months later.

CLI_SRC = ROOT / "packages" / "asas-cli" / "src" / "asas_cli"
NON_CLI_PACKAGES = [p for p in PACKAGES if p.name != "asas-cli"]


def _fallback_tags() -> dict[str, str]:
    text = (CLI_SRC / "git_tags.py").read_text()
    block = re.search(r"FALLBACK_TAGS[^{]*\{(.*?)\}", text, re.S).group(1)
    return dict(re.findall(r'"([a-z0-9-]+)":\s*"(v\d+\.\d+\.\d+)"', block))


def _registry_dist_names() -> set[str]:
    text = (CLI_SRC / "registry.py").read_text()
    return set(re.findall(r'"(asas-[a-z0-9-]+)",\n\s+"asas_', text))


def test_cli_registry_matches_the_packages_directory():
    expected = {p.name for p in NON_CLI_PACKAGES}
    assert _registry_dist_names() == expected, (
        "asas_cli.registry._SPECS disagrees with packages/ — a package was "
        "added or renamed without updating the CLI's roster."
    )


@pytest.mark.parametrize("pkg", NON_CLI_PACKAGES, ids=lambda p: p.name)
def test_cli_fallback_tag_is_current(pkg):
    fallback = _fallback_tags().get(pkg.name)
    assert fallback == f"v{_pyproject_version(pkg)}", (
        f"asas_cli.git_tags.FALLBACK_TAGS[{pkg.name!r}] is {fallback}, but the "
        f"package is at {_pyproject_version(pkg)} — bumping FALLBACK_TAGS is a "
        "release step (RELEASING.md), or offline installs pin a stale version."
    )
