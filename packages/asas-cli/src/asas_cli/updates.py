"""The consumer side of the release discipline: `asas outdated`.

An Asas pin is frozen by design, so staying current is a deliberate act —
this module makes it a CHECKABLE one. Drift is tiered (RELEASING.md, "The
consumer contract"):

- current / patch-behind:  fine. Patches are fixes; refresh opportunistically.
- minor-behind:            breaking changes are waiting (pre-1.0 minor =
                           breaking). CI mode fails so the upgrade is a
                           decision, not a surprise.
- advisory-affected:       the pinned version is called out in the repo's
                           ADVISORIES.json. Refresh is mandatory; CI mode
                           fails hard regardless of tier.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .git_tags import REPO_URL, _semver_key, latest_tags

#: Where advisories live: one JSON file on the default branch, so publishing
#: an advisory is an ordinary reviewed commit, not new infrastructure.
ADVISORIES_URL = (
    "https://raw.githubusercontent.com/wlootah-a11y/asas/main/ADVISORIES.json"
)

_PIN_RE = re.compile(
    r"(asas-[a-z]+)\s*@\s*git\+[^\s\"']+@\1/(v\d+\.\d+\.\d+)#subdirectory="
)


@dataclass(frozen=True)
class PinStatus:
    dist_name: str
    pinned: str          # "v0.12.0"
    latest: str          # "v0.12.1"
    tier: str            # "current" | "patch-behind" | "minor-behind"
    advisory: Optional[str] = None  # the advisory note when the pin is affected


def parse_pins(pyproject_text: str) -> dict[str, str]:
    """Every Asas git-tag pin in a consumer's pyproject: {dist_name: vX.Y.Z}."""
    return {m.group(1): m.group(2) for m in _PIN_RE.finditer(pyproject_text)}


def _tier(pinned: str, latest: str) -> str:
    p, l = _semver_key(pinned), _semver_key(latest)
    if p >= l:
        return "current"
    if (p[0], p[1]) == (l[0], l[1]):
        return "patch-behind"
    return "minor-behind"


def fetch_advisories(url: str = ADVISORIES_URL, *, timeout: float = 5.0) -> list[dict]:
    """The advisory list, or [] when unreachable (a network hiccup must not
    fail a build on its own — the WARNING is printed so silence is loud)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.load(response).get("advisories", [])
    except Exception as exc:  # noqa: BLE001 - offline/CI without egress
        print(f"asas: could not fetch advisories ({exc}); skipping that check",
              file=sys.stderr)
        return []


def _affected(pinned: str, advisory: dict) -> bool:
    below = advisory.get("fixed_in")
    return bool(below) and _semver_key(pinned) < _semver_key(str(below))


def check_pins(
    pyproject_path: Path,
    *,
    repo_url: str = REPO_URL,
    advisories_url: str = ADVISORIES_URL,
) -> list[PinStatus]:
    pins = parse_pins(pyproject_path.read_text())
    if not pins:
        return []
    latest = latest_tags(sorted(pins), repo_url)
    advisories = fetch_advisories(advisories_url)
    statuses = []
    for dist, pinned in sorted(pins.items()):
        note = next(
            (a.get("note", "see ADVISORIES.json")
             for a in advisories
             if a.get("package") == dist and _affected(pinned, a)),
            None,
        )
        statuses.append(PinStatus(
            dist_name=dist, pinned=pinned, latest=latest[dist],
            tier=_tier(pinned, latest[dist]), advisory=note,
        ))
    return statuses


def outdated(pyproject_path: Path, *, ci: bool = False,
             repo_url: str = REPO_URL,
             advisories_url: str = ADVISORIES_URL) -> int:
    """Print the drift table. Exit 0 when nothing demands action; in --ci
    mode exit 1 on minor-behind (breaking changes waiting) and 2 on an
    advisory-affected pin (refresh mandatory, any tier)."""
    statuses = check_pins(pyproject_path, repo_url=repo_url,
                          advisories_url=advisories_url)
    if not statuses:
        print("no Asas pins found in", pyproject_path)
        return 0
    worst = 0
    for s in statuses:
        marker = {"current": "ok       ", "patch-behind": "patch    ",
                  "minor-behind": "MINOR    "}[s.tier]
        line = f"{marker}{s.dist_name:24}{s.pinned:12}latest {s.latest}"
        if s.advisory:
            line += f"   ADVISORY: {s.advisory}"
            worst = max(worst, 2)
        elif s.tier == "minor-behind" and ci:
            worst = max(worst, 1)
        print(line)
    if worst == 1:
        print("\nasas outdated: minor releases are breaking pre-1.0 — read the "
              "package CHANGELOG(s) and bump deliberately.", file=sys.stderr)
    if worst == 2:
        print("\nasas outdated: an advisory affects a pinned version — this "
              "refresh is mandatory.", file=sys.stderr)
    return worst
