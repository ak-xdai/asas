"""Call every Oracle recruiting read once, and say how each one answered.

A deployment that reaches Fusion through an API gateway registers each
operation on its own (registered, tested and activated one by one), so "the
gateway works" is eighteen facts, not one. This walks the reads in the order a
path parameter becomes available (a requisition number from the list, then its
record and descriptive flexfield; a candidate number, then the attachments and
one file), with the host's own settings, so what it checks is what the host
would send: the base URL, the gateway key header, and Basic auth only when a
username is set.

It never prints a body. The HTTP status, the gateway's or Oracle's error text
trimmed to one line, a row count and the time taken: enough to tell a missing
registration (a 404 from the gateway) from a refused key (401/403), a transport
the gateway was not registered for, and a fault behind it (5xx). The two writes
(POST and PATCH on requisitions) are listed and deliberately NOT called: a
check must not create or change a requisition in somebody's HCM.

From a shell, configured like :meth:`OracleSettings.from_env`::

    ORACLE_HCM_BASE_URL=... ORACLE_HCM_GATEWAY_API_KEY=... asas-oracle-check
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .client import OracleFusionClient
from .recruiting import enclosure_key
from .settings import OracleSettings

#: The registered collection reads, in call order.
LISTS: tuple[str, ...] = (
    "/recruitingJobRequisitions",
    "/recruitingJobRequisitionsLOV",
    "/recruitingHierarchyLocations",
    "/recruitingCandidates",
    "/recruitingJobApplications",
    "/publicWorkers",
    "/grades",
    "/departments",
    "/hcmBusinessUnitsLOV",
    "/organizations",
    "/jobs",
    "/positions",
    "/jobFamilies",
)
#: The reads addressed by a key an earlier answer supplies.
BY_KEY: tuple[str, ...] = (
    "/recruitingJobRequisitions/{RequisitionNumber}",
    "/recruitingJobRequisitions/{RequisitionNumber}/child/requisitionDFF",
    "/recruitingCandidates/{CandidateNumber}",
    "/recruitingCandidates/{CandidateNumber}/child/attachments",
    "/recruitingCandidates/{CandidateNumber}/child/attachments/{AttachmentKey}/enclosure/FileContents",
)
#: Listed, never called.
NOT_CALLED: tuple[str, ...] = (
    "POST /recruitingJobRequisitions",
    "PATCH /recruitingJobRequisitions/{RequisitionNumber}",
)
#: How many candidates are searched for one with an attachment, so the file
#: read can be exercised (many candidates on a test pod carry none).
ATTACHMENT_SEARCH = 25


@dataclass
class CheckResult:
    operation: str
    status: str
    detail: str = ""
    ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status.startswith("2")


def _one_line(value: str, limit: int = 160) -> str:
    return " ".join((value or "").split())[:limit]


async def _get(client: OracleFusionClient, path: str, params: dict[str, Any], *, binary: bool = False) -> tuple[CheckResult, Any]:
    http = client._http()
    headers = client._headers()
    if binary:
        headers["Accept"] = "*/*"
    started = time.perf_counter()
    try:
        response = await http.get(
            client._url(path), params=params, headers=headers, auth=client._settings.basic_auth
        )
    except httpx.HTTPError as exc:
        return CheckResult(path, "unreachable", _one_line(f"{type(exc).__name__}: {exc}")), None
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


def _first(body: Any, field: str) -> list[str]:
    items = body.get("items") if isinstance(body, dict) else None
    return [str(r[field]) for r in items or [] if isinstance(r, dict) and r.get(field) not in (None, "")]


async def check(client: OracleFusionClient, *, limit: int = 1) -> list[CheckResult]:
    """One result per registered operation, reads called and writes listed."""
    if not client.configured:
        return [CheckResult("(configuration)", "not configured", "no base URL")]
    results: list[CheckResult] = []
    found: dict[str, str] = {}
    candidates: list[str] = []
    for path in LISTS:
        size = ATTACHMENT_SEARCH if path == "/recruitingCandidates" else limit
        result, body = await _get(client, path, {"limit": size, "onlyData": "true"})
        results.append(result)
        if path == "/recruitingJobRequisitions":
            found["RequisitionNumber"] = next(iter(_first(body, "RequisitionNumber")), "")
        elif path == "/recruitingCandidates":
            candidates = _first(body, "CandidateNumber")
            found["CandidateNumber"] = next(iter(candidates), "")
    for template in BY_KEY:
        if template.endswith("/child/attachments"):
            if not candidates:
                results.append(CheckResult(template, "skipped", "no CandidateNumber to call it with"))
                continue
            # The attachment listing of the first candidate that has one, so
            # the file read below can be exercised.
            first: CheckResult | None = None
            for number in candidates:
                result, body = await _get(
                    client, f"/recruitingCandidates/{number}/child/attachments",
                    {"onlyData": "false", "limit": 25},
                )
                first = first or result
                rows = body.get("items") if isinstance(body, dict) else None
                key = next((k for r in rows or [] if (k := enclosure_key(r))), "")
                if key:
                    found["CandidateNumber"], found["AttachmentKey"] = number, key
                    first = result
                    break
                if not result.ok:
                    break
            assert first is not None
            first.operation = template
            results.append(first)
            continue
        if "{AttachmentKey}" in template and not found.get("AttachmentKey"):
            results.append(CheckResult(template, "skipped", "no candidate with an attachment"))
            continue
        missing = [k for k in ("RequisitionNumber", "CandidateNumber") if "{" + k + "}" in template and not found.get(k)]
        if missing:
            results.append(CheckResult(template, "skipped", f"no {missing[0]} to call it with"))
            continue
        path = template.format(**found)
        binary = template.endswith("/enclosure/FileContents")
        result, _ = await _get(client, path, {} if binary else {"onlyData": "true"}, binary=binary)
        result.operation = template
        results.append(result)
    for operation in NOT_CALLED:
        results.append(CheckResult(operation, "not called", "a write; a check must not change Oracle"))
    return results


def render(settings: OracleSettings, results: list[CheckResult]) -> str:
    key = "set" if settings.gateway_api_key else "not set"
    basic = "sent" if settings.basic_auth else "not sent"
    lines = [
        f"base URL : {settings.base_url or '(empty)'}",
        f"API key  : {settings.gateway_api_key_header} ({key}); Basic auth {basic}",
    ]
    width = max(len(r.operation) for r in results)
    for r in results:
        timing = f"{r.ms:>6} ms" if r.ms else " " * 9
        lines.append(f"{r.operation:<{width}}  {r.status:<14} {timing}  {r.detail}")
    called = [r for r in results if r.status not in ("skipped", "not called")]
    lines.append(f"{sum(r.ok for r in called)} of {len(called)} calls answered 2xx")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prefix", default="ORACLE_HCM_", help="environment variable prefix")
    parser.add_argument("--limit", type=int, default=1, help="rows asked of each list")
    args = parser.parse_args(argv)
    settings = OracleSettings.from_env(args.prefix)

    async def go() -> list[CheckResult]:
        async with OracleFusionClient(settings) as client:
            return await check(client, limit=args.limit)

    results = asyncio.run(go())
    print(render(settings, results))
    called = [r for r in results if r.status not in ("skipped", "not called")]
    return 0 if called and all(r.ok for r in called) else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
