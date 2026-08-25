#!/usr/bin/env python3
"""Assert every exported symbol has a line in its package's doc page.

Contract: DR 0002. For each package under ``packages/`` that has a page at
``docs/packages/<name>.md``, every non-dunder name in the package's ``__all__``
must appear in that page. Packages without a page are skipped, so the check
gates a package from the moment its documentation lands and never before.

The check is deliberately shallow: it catches the drift that actually happens
(a symbol added, renamed, or removed and never written up). Whether a
description is any good is review's job.

Usage:
    python tools/check_docs.py            # check
    python tools/check_docs.py --list     # show coverage for every package
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PACKAGES = REPO / "packages"
DOCS = REPO / "docs" / "packages"


def exported_names(init: Path) -> list[str] | None:
    """Names in ``__all__``, or None if the module declares no ``__all__``."""
    tree = ast.parse(init.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__all__":
                return [
                    el.value
                    for el in node.value.elts
                    if isinstance(el, ast.Constant) and isinstance(el.value, str)
                ]
    return None


def public(names: list[str]) -> list[str]:
    return [n for n in names if not n.startswith("__")]


def missing_from(page: str, names: list[str]) -> list[str]:
    return [n for n in names if not re.search(rf"\b{re.escape(n)}\b", page)]


def survey() -> list[tuple[str, Path | None, list[str] | None]]:
    rows = []
    for pkg in sorted(PACKAGES.iterdir()):
        if not pkg.is_dir():
            continue
        init = next(pkg.glob("src/*/__init__.py"), None)
        if init is None:
            continue
        page = DOCS / f"{pkg.name}.md"
        rows.append((pkg.name, page if page.exists() else None, exported_names(init)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="report coverage, never fail")
    args = ap.parse_args()

    rows = survey()
    failures: list[str] = []
    checked = 0

    for name, page, names in rows:
        if args.list:
            total = len(public(names)) if names else 0
            if page is None:
                covered = "-"
                state = "no page"
            elif names is None:
                covered = "-"
                state = "no __all__"
            else:
                miss = missing_from(page.read_text(encoding="utf-8"), public(names))
                covered = f"{total - len(miss)}/{total}"
                state = "ok" if not miss else f"{len(miss)} undocumented"
            print(f"{name:24} {covered:>8}  {state}")
            continue

        if page is None:
            continue  # not yet documented; not yet gated

        checked += 1
        if names is None:
            failures.append(
                f"{name}: has docs/packages/{name}.md but declares no __all__, "
                f"so its public surface is undefined (DR 0002, D-4)."
            )
            continue

        miss = missing_from(page.read_text(encoding="utf-8"), public(names))
        if miss:
            failures.append(
                f"{name}: {len(miss)} exported symbol(s) missing from "
                f"docs/packages/{name}.md: {', '.join(sorted(miss))}"
            )

    if args.list:
        return 0

    if failures:
        print("Documentation check failed (DR 0002):\n", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print(
            "\nAdd the symbol to the package's API reference, or remove it "
            "from __all__ if it was never meant to be public.",
            file=sys.stderr,
        )
        return 1

    print(f"Documentation check passed: {checked} documented package(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
