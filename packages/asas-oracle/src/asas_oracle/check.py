"""Call each Fusion read a deployment depends on once, and say how it answered.

A deployment that reaches Fusion through an API gateway registers each
operation on its own (registered, tested and activated one by one), so "the
gateway works" is one fact per operation. This walks a MANIFEST of probes in
order, with the host's own client, so what it checks is what the host would
send: the base URL, the credentials (Basic, a gateway key, an OAuth token) and
any extra headers.

It never prints a body. The HTTP status, the gateway's or Fusion's error text
trimmed to one line, a row count and the time taken: enough to tell a missing
registration (a 404 from the gateway) from a refused key (401/403), a transport
the gateway was not registered for, and a fault behind it (5xx). A write in the
manifest is listed and never called: a check must not change somebody's HCM.

**A probe can depend on an earlier answer.** ``provides`` names values to take
from a read's rows (``{"PersonId": "PersonId"}``), and a later probe's path
uses them (``/publicWorkers/{PersonId}``). Every row's value is collected, and
``tries`` lets a probe walk several of them until one answers what IT provides:
"the first record that has an attachment" is a listing that provides the key,
then an attachments probe with ``tries=25`` that provides ``"@enclosure_key"``.

The default manifest (:data:`REFERENCE_PROBES`) is the HCM reference reads the
lookups use. A product passes its own, in code or as JSON::

    asas-oracle-check --manifest probes.json

    {"probes": [
      {"path": "/workers", "params": {"limit": 5}, "provides": {"PersonId": "PersonId"}},
      {"path": "/workers/{PersonId}/child/attachments", "params": {"onlyData": "false"},
       "provides": {"Key": "@enclosure_key"}, "tries": 5},
      {"path": "/workers/{PersonId}/child/attachments/{Key}/enclosure/FileContents", "binary": true},
      {"label": "PATCH /workers/{PersonId}", "call": false}
    ]}
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .attachments import enclosure_key
from .client import OracleFusionClient
from .errors import OracleAuthError, OracleUpstreamError
from .lookups import NAME_LOOKUPS
from .settings import OracleSettings

#: ``provides`` value meaning "the enclosure key in a row's links".
ENCLOSURE_KEY = "@enclosure_key"

_PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")


@dataclass(frozen=True)
class Probe:
    """One operation to check.

    ``path`` may hold ``{Name}`` placeholders an earlier probe ``provides``.
    ``call=False`` lists the operation without calling it (a write); ``label``
    is what is printed (defaults to the path). ``binary`` reads an enclosure
    (``Accept: */*``, no ``onlyData``). ``tries`` is how many of a
    placeholder's collected values the probe may walk before giving up."""

    path: str = ""
    params: Mapping[str, Any] = field(default_factory=dict)
    provides: Mapping[str, str] = field(default_factory=dict)
    tries: int = 1
    binary: bool = False
    call: bool = True
    label: str = ""

    @property
    def name(self) -> str:
        return self.label or self.path

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Probe:
        unknown = set(raw) - {"path", "params", "provides", "tries", "binary", "call", "label"}
        if unknown:
            raise ValueError(f"unknown probe fields: {sorted(unknown)}")
        probe = cls(
            path=str(raw.get("path", "")),
            params=dict(raw.get("params") or {}),
            provides=dict(raw.get("provides") or {}),
            tries=int(raw.get("tries", 1)),
            binary=bool(raw.get("binary", False)),
            call=bool(raw.get("call", True)),
            label=str(raw.get("label", "")),
        )
        if probe.call and not probe.path:
            raise ValueError("a probe that is called needs a path")
        if probe.tries < 1:
            raise ValueError("tries must be at least 1")
        return probe


def _reference_probes() -> tuple[Probe, ...]:
    seen: list[str] = []
    for resource, _key, _name in NAME_LOOKUPS.values():
        if resource not in seen:
            seen.append(resource)
    return tuple(Probe(path=r, params={"limit": 1}) for r in seen)


#: The HCM reference reads the lookups depend on, one row each.
REFERENCE_PROBES: tuple[Probe, ...] = _reference_probes()


def load_manifest(path: str | Path) -> list[Probe]:
    """Probes from a JSON file: ``{"probes": [{...}, ...]}`` or a bare list."""
    raw = json.loads(Path(path).read_text())
    items = raw.get("probes") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise ValueError("a manifest is a list of probes, or {'probes': [...]}")
    return [Probe.from_dict(item) for item in items]


@dataclass
class CheckResult:
    operation: str
    status: str
    detail: str = ""
    ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status.startswith("2")

    @property
    def called(self) -> bool:
        return self.status not in ("skipped", "not called")


def _one_line(value: str, limit: int = 160) -> str:
    return " ".join((value or "").split())[:limit]


async def _get(
    client: OracleFusionClient, path: str, params: Mapping[str, Any], *, binary: bool
) -> tuple[CheckResult, Any]:
    query = dict(params)
    headers: dict[str, str] = {}
    if binary:
        headers["Accept"] = "*/*"
    else:
        query.setdefault("onlyData", "true")
    started = time.perf_counter()
    try:
        # guard=False: a check must reach the upstream even with a breaker open,
        # and it still feeds the client's statistics and hook.
        response = await client.request(
            "GET", path, params=query, headers=headers, raise_for_status=False, guard=False
        )
    except OracleAuthError as exc:
        # The credential itself could not be had (a token endpoint refused):
        # nothing reached the operation, and the fix is the configuration.
        return CheckResult(path, "auth failed", _one_line(str(exc))), None
    except OracleUpstreamError as exc:
        cause = exc.__cause__ or exc
        return CheckResult(path, "unreachable", _one_line(f"{type(cause).__name__}: {cause}")), None
    ms = int((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        return CheckResult(path, str(response.status_code), _one_line(response.text), ms), None
    if binary or "json" not in response.headers.get("content-type", ""):
        return CheckResult(path, str(response.status_code), f"{len(response.content)} bytes", ms), None
    try:
        body = response.json()
    except ValueError:
        return CheckResult(path, str(response.status_code), "body is not JSON", ms), None
    items = body.get("items") if isinstance(body, dict) else None
    detail = f"{len(items)} rows" if isinstance(items, list) else "one record"
    return CheckResult(path, str(response.status_code), detail, ms), body


def _rows(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, dict) and isinstance(body.get("items"), list):
        return [r for r in body["items"] if isinstance(r, dict)]
    return [body] if isinstance(body, dict) else []


def _collect(body: Any, provides: Mapping[str, str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, source in provides.items():
        values: list[str] = []
        for row in _rows(body):
            value = enclosure_key(row) if source == ENCLOSURE_KEY else row.get(source)
            if value not in (None, "") and str(value) not in values:
                values.append(str(value))
        out[name] = values
    return out


async def check(
    client: OracleFusionClient, probes: Sequence[Probe] = REFERENCE_PROBES
) -> list[CheckResult]:
    """One result per probe: reads called in order, writes listed."""
    if not client.configured:
        return [CheckResult("(configuration)", "not configured", "no base URL")]
    found: dict[str, list[str]] = {}
    results: list[CheckResult] = []
    for probe in probes:
        if not probe.call:
            results.append(CheckResult(probe.name, "not called", "a write; a check must not change Oracle"))
            continue
        needed = _PLACEHOLDER.findall(probe.path)
        missing = [n for n in needed if not found.get(n)]
        if missing:
            results.append(CheckResult(probe.name, "skipped", f"no {missing[0]} to call it with"))
            continue
        # Walk the first placeholder's values (up to `tries`); the others are
        # bound to their first value.
        varying = needed[0] if needed else None
        choices = found[varying][: probe.tries] if varying else [""]
        last: CheckResult | None = None
        for choice in choices:
            values = {n: found[n][0] for n in needed}
            if varying:
                values[varying] = choice
            path = probe.path.format(**values)
            result, body = await _get(client, path, probe.params, binary=probe.binary)
            result.operation = probe.name
            last = result
            if not result.ok:
                break
            got = _collect(body, probe.provides)
            if all(got.get(n) for n in probe.provides):
                for name, vals in got.items():
                    found[name] = vals
                if varying:
                    # Later probes use the value that answered.
                    found[varying] = [choice] + [v for v in found[varying] if v != choice]
                break
        assert last is not None
        if last.ok and probe.provides and not all(found.get(n) for n in probe.provides):
            last.detail += f"; none of {len(choices)} provided {', '.join(probe.provides)}"
        results.append(last)
    return results


def render(settings: OracleSettings, results: list[CheckResult]) -> str:
    lines = [
        f"base URL : {settings.base_url or '(empty)'}",
        f"auth     : {settings.describe_auth()}",
    ]
    width = max((len(r.operation) for r in results), default=10)
    for r in results:
        timing = f"{r.ms:>6} ms" if r.ms else " " * 9
        lines.append(f"{r.operation:<{width}}  {r.status:<14} {timing}  {r.detail}")
    called = [r for r in results if r.called]
    lines.append(f"{sum(r.ok for r in called)} of {len(called)} calls answered 2xx")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--prefix", default="ORACLE_HCM_", help="environment variable prefix")
    parser.add_argument("--manifest", help="a JSON manifest of probes (default: the HCM reference reads)")
    parser.add_argument("--path", action="append", default=[], help="a read to probe (repeatable), instead of a manifest")
    args = parser.parse_args(argv)
    if args.manifest and args.path:
        parser.error("pass --manifest or --path, not both")
    probes: Sequence[Probe] = REFERENCE_PROBES
    if args.manifest:
        probes = load_manifest(args.manifest)
    elif args.path:
        probes = [Probe(path=p, params={"limit": 1}) for p in args.path]
    settings = OracleSettings.from_env(args.prefix)

    async def go() -> list[CheckResult]:
        async with OracleFusionClient(settings) as client:
            return await check(client, probes)

    results = asyncio.run(go())
    print(render(settings, results))
    called = [r for r in results if r.called]
    return 0 if called and all(r.ok for r in called) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
