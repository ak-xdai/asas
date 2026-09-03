"""The six ``migrate.py`` files must stay byte-identical modulo per-package data.

Every table-owning package carries its own copy of the adopt-or-create migration
runner. That duplication is a **deliberate standing choice** (Teamy TEAMY-798,
option (b)): extracting it into an ``asas-core`` package is the obvious
alternative, but the family is installed by git URL with no package index, so a
sibling dependency would force every consumer to add an explicit
``asas-core @ git+…`` pin, including any that mirror this repo internally. That
cost was judged higher than the duplication.

The bargain only holds if the copies cannot drift, which is what this test is
for. It already earned its place: ``asas-lookups`` (the pilot, written first)
still told readers to call ``migrate()`` *before* the host's own migrations,
while the other five said *alongside*. DR 0017 §4 was corrected during the
pilot; that one docstring never was, so the oldest copy documented the opposite
of what every host actually does.

If this test fails, the fix is to apply your change to **all six** files — not
to loosen the normalisation. The only thing that may join the normalised set is
genuinely per-package *data* — the identity of the package and the shape of the
schema it owns. Logic and prose must stay identical, which is the whole point.
"""

import difflib
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
MIGRATES = sorted(ROOT.glob("packages/asas-*/src/asas_*/migrate.py"))

# Everything below is legitimately per-package: the identity of the package and
# the shape of the table it adopts. Anything else differing is drift.
_COLUMNS_BLOCK = re.compile(r"_SENTINEL_COLUMNS = frozenset\(\{.*?\n\}\)", re.S)
_TABLES_BLOCK = re.compile(r"_BASELINE_TABLES = frozenset\(\{.*?\n\}\)", re.S)
# Which of its own columns a package renames is the shape of the schema it
# adopts, exactly like the sentinel columns above, so it belongs here rather
# than counting as drift. The LOGIC that reads it stays identical.
_RENAMED_BLOCK = re.compile(
    r"_RENAMED_PAIRS: tuple\[tuple\[str, str\], \.\.\.\] = \(.*?\)\n", re.S
)


def _package_of(path: pathlib.Path) -> str:
    return path.parts[-4]


def normalise(path: pathlib.Path) -> str:
    """Strip the per-package data, leaving only the shared logic and prose."""
    text = path.read_text()
    sentinel = re.search(r'_SENTINEL_TABLE = "([^"]+)"', text).group(1)
    dist = _package_of(path)                    # asas-lookups
    module = dist.replace("-", "_")             # asas_lookups

    text = _COLUMNS_BLOCK.sub("_SENTINEL_COLUMNS = frozenset({<COLUMNS>})", text)
    text = _TABLES_BLOCK.sub("_BASELINE_TABLES = frozenset({<TABLES>})", text)
    text = _RENAMED_BLOCK.sub("_RENAMED_PAIRS = (<RENAMES>)\n", text)
    # Order matters: the module form is a substring of nothing, but the dist form
    # appears inside the version-table name, so replace the longer names first.
    text = text.replace(f"alembic_version_{module}", "alembic_version_<PKG>")
    text = text.replace(dist, "<DIST>").replace(module, "<MODULE>")
    text = text.replace(f'"{sentinel}"', '"<SENTINEL>"')
    text = text.replace(f"``{sentinel}``", "``<SENTINEL>``")
    return text


def test_all_six_packages_are_present():
    """A new table-owning package silently skipping this test would defeat it."""
    assert len(MIGRATES) == 6, f"expected 6 migrate.py files, found {len(MIGRATES)}: {MIGRATES}"


@pytest.mark.parametrize("path", MIGRATES[1:], ids=_package_of)
def test_matches_the_reference_copy(path):
    """Each copy must normalise to exactly the same text as the first one."""
    reference = MIGRATES[0]
    expected, actual = normalise(reference), normalise(path)
    if expected != actual:
        diff = "\n".join(difflib.unified_diff(
            expected.splitlines(), actual.splitlines(),
            fromfile=f"{_package_of(reference)}/migrate.py",
            tofile=f"{_package_of(path)}/migrate.py",
            lineterm="",
        ))
        pytest.fail(
            f"{_package_of(path)}/migrate.py has drifted from "
            f"{_package_of(reference)}/migrate.py.\n"
            f"Apply the change to all six copies (see this module's docstring).\n\n{diff}"
        )
