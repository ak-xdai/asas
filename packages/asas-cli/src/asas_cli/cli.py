"""`asas` command-line entry point: `asas add`, `asas new`, `asas list`."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .git_tags import latest_tag
from .pyproject_edit import add_dependency
from .registry import PACKAGES, resolve
from .scaffold import scaffold


def _cmd_list(_args: argparse.Namespace) -> int:
    width = max(len(k) for k in PACKAGES)
    for key, spec in sorted(PACKAGES.items()):
        print(f"{key.ljust(width)}  {spec.variant.ljust(28)}  {spec.summary}")
    return 0


def _cmd_add(args: argparse.Namespace) -> int:
    try:
        spec = resolve(args.package)
    except KeyError as exc:
        print(f"asas add: {exc}", file=sys.stderr)
        return 1

    # Fail on a bad --path before latest_tag's remote round trip, not after.
    path = Path(args.path)
    if not path.is_file():
        print(f"asas add: {path} does not exist", file=sys.stderr)
        return 1

    version = f"v{args.version.lstrip('v')}" if args.version else latest_tag(spec.dist_name)

    try:
        outcome = add_dependency(path, spec, version)
    except (FileNotFoundError, KeyError) as exc:
        print(f"asas add: {exc}", file=sys.stderr)
        return 1

    print(f"asas add: {outcome} {spec.dist_name} @ {spec.dist_name}/{version} in {path}")
    if outcome == "added":
        print("Run `pip install -e .` (or your usual install command) to pull it in.")
    return 0


def _cmd_new(args: argparse.Namespace) -> int:
    # The name lands verbatim in the generated `[project] name`, so it must be
    # a valid PEP 508 project name — this also rejects paths passed as names.
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?", args.name):
        print(
            f"asas new: {args.name!r} is not a valid project name "
            "(letters, digits, ., _, - only; use --dir to pick the parent directory)",
            file=sys.stderr,
        )
        return 1

    raw = [k.strip() for k in args.with_.split(",") if k.strip()]
    if not raw:
        print("asas new: --with needs at least one package (see `asas list`)", file=sys.stderr)
        return 1

    try:
        keys = [resolve(k).key for k in raw]
    except KeyError as exc:
        print(f"asas new: {exc}", file=sys.stderr)
        return 1

    project_dir = Path(args.dir) / args.name if args.dir else Path(args.name)

    try:
        created = scaffold(project_dir, args.name, keys)
    except FileExistsError as exc:
        print(f"asas new: {exc}", file=sys.stderr)
        return 1

    print(f"asas new: scaffolded {args.name} in {project_dir}/ wired for: {', '.join(keys)}")
    for path in created:
        print(f"  {path}")
    print(f"\nNext: cd {project_dir} && pip install -e '.[dev]' && uvicorn main:app --reload")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="asas", description="Asas developer CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list known Asas packages")
    p_list.set_defaults(func=_cmd_list)

    p_add = sub.add_parser("add", help="pin one Asas package into an existing project")
    p_add.add_argument("package", help="short key (e.g. 'lookups') or dist name (e.g. 'asas-lookups')")
    p_add.add_argument(
        "--version", default=None, help="this package's own version to pin, e.g. 0.11.0 (default: latest)"
    )
    p_add.add_argument("--path", default="pyproject.toml", help="path to the project's pyproject.toml")
    p_add.set_defaults(func=_cmd_add)

    p_new = sub.add_parser("new", help="scaffold a new project wired for a set of packages")
    p_new.add_argument("name", help="project name (also the directory created)")
    p_new.add_argument(
        "--with", dest="with_", required=True,
        help="comma-separated packages, by short key or dist name, e.g. lookups,asas-ratelimit"
    )
    p_new.add_argument("--dir", default=None, help="parent directory to create the project in (default: cwd)")
    p_new.set_defaults(func=_cmd_new)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
