"""Keep the local prompt copy current: pull the registry into a directory.

    python -m asas_llm.snapshot --dest app/prompts --label production
    python -m asas_llm.snapshot --dest app/prompts --names parse-cv jd-generate

Writes one ``<name>.json`` per prompt in the :meth:`Prompt.to_dict` format,
which :class:`~asas_llm.prompts.LocalPromptStore` reads. Run it in the build
that produces the image, or as a scheduled job that commits the diff, so the
fallback a cold replica serves is the version production was using, and a
prompt edit that never reached the repo shows up as a missing diff rather than
as a replica answering with last quarter's prompt.

The CLI reads ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY`` / ``LANGFUSE_HOST``
from the environment because it is a tool, not a library; the library itself
reads nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .errors import LLMError
from .prompts import LocalPromptStore, PromptStore


@dataclass
class SnapshotReport:
    written: dict[str, int | None] = field(default_factory=dict)  # name -> version
    unchanged: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


def snapshot(
    source: PromptStore,
    dest: str | Path,
    names: Iterable[str] | None = None,
    *,
    label: str | None = None,
) -> SnapshotReport:
    """Write every prompt (or ``names``) from ``source`` into ``dest``. A file
    whose content would not change is left untouched so a commit of the
    directory shows only real edits."""
    store = LocalPromptStore(dest)
    store.directory.mkdir(parents=True, exist_ok=True)
    report = SnapshotReport()
    for name in list(names) if names is not None else source.names():
        try:
            prompt = source.get(name, label=label)
        except LLMError as exc:
            report.failed[name] = str(exc)
            continue
        record = prompt.to_dict()
        path = store.path_for(name)
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if existing is not None and _same(existing, record):
                report.unchanged.append(name)
                continue
        path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        report.written[name] = prompt.version
    return report


_VOLATILE = ("snapshotted_at",)


def _same(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if k not in _VOLATILE} == {k: v for k, v in b.items() if k not in _VOLATILE}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m asas_llm.snapshot", description=__doc__.split("\n\n")[0])
    parser.add_argument("--dest", required=True, help="directory the local prompt copy lives in")
    parser.add_argument("--names", nargs="*", help="prompt names; default: every prompt in the registry")
    parser.add_argument("--label", default=None, help="registry label to pull, e.g. production")
    parser.add_argument("--host", default=os.environ.get("LANGFUSE_HOST"))
    args = parser.parse_args(argv)

    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        print("LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY must be set", file=sys.stderr)
        return 2

    from .langfuse import LangfusePromptStore, make_client

    client = make_client(public_key=public, secret_key=secret, host=args.host)
    try:
        if args.names == []:
            # nargs="*" makes an empty expansion ($EXTRA_PROMPTS unset) look
            # like "snapshot nothing", which would exit 0 having written
            # nothing and ship a stale fallback with no CI signal. Empty is a
            # mistake; "all prompts" is spelled by omitting --names.
            print("--names given with no values; omit it to snapshot every prompt", file=sys.stderr)
            return 2
        report = snapshot(LangfusePromptStore(client), args.dest, args.names, label=args.label)
    finally:
        client.shutdown()

    for name, version in report.written.items():
        print(f"written    {name} (version {version})")
    for name in report.unchanged:
        print(f"unchanged  {name}")
    for name, why in report.failed.items():
        print(f"FAILED     {name}: {why}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
