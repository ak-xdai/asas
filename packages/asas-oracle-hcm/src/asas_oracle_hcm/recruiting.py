"""Helpers for Oracle Recruiting Cloud resources whose limits are Oracle's and
not any product's. They return Oracle's raw rows; what a row MEANS is the
host's business.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import OracleFusionClient
from .query import eq

CANDIDATES = "/recruitingCandidates"

#: ``/recruitingCandidates`` refuses a page over 200 items (IRC-1590415) ...
CANDIDATE_MAX_PAGE_SIZE = 200
#: ... and any offset at or past 10,000 (FND-2420), so the collection cannot be
#: walked to its end. Getting past it means slicing the collection with ``q``.
CANDIDATE_OFFSET_CEILING = 10_000


@dataclass(frozen=True)
class CandidatePage:
    """One page of candidates. ``at_ceiling`` is True when this page reaches
    the offset ceiling: there may be more candidates, but none reachable by
    paging. Oracle reports ``hasMore=false`` at exactly 10,000, which reads as
    "that was the last candidate" when it means "that is the last I will page
    to"; ``has_more`` here is False past the ceiling for the same reason."""

    items: list[dict[str, Any]]
    limit: int
    offset: int
    has_more: bool
    at_ceiling: bool


async def candidate_page(
    client: OracleFusionClient,
    *,
    limit: int = 25,
    offset: int = 0,
    q: str | None = None,
) -> CandidatePage:
    """A page of ``/recruitingCandidates`` that never walks into Oracle's caps.

    The page size is clamped to 200, an offset at or past the ceiling returns
    an empty page flagged ``at_ceiling`` without asking Oracle, and the last
    reachable page is trimmed, because Oracle refuses the WHOLE request when
    ``offset + limit`` crosses the ceiling, not just the part past it."""
    limit = max(1, min(limit, CANDIDATE_MAX_PAGE_SIZE))
    offset = max(0, offset)
    if offset >= CANDIDATE_OFFSET_CEILING:
        return CandidatePage([], limit, offset, False, True)
    limit = min(limit, CANDIDATE_OFFSET_CEILING - offset)
    rows, has_more, _ = await client.get_collection(
        CANDIDATES, {"limit": limit, "offset": offset, "q": q}
    )
    at_ceiling = offset + limit >= CANDIDATE_OFFSET_CEILING
    return CandidatePage(rows, limit, offset, has_more and not at_ceiling, at_ceiling)


async def candidate_attachments(
    client: OracleFusionClient,
    candidate_number: str,
    *,
    category: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """A candidate's attachment rows, newest first, WITH their ``links``.

    The one read that must not send ``onlyData=true``: the key that addresses
    the file bytes exists only inside ``links`` (see :func:`enclosure_key`).
    ``category`` is Oracle's ``CategoryName``, for example
    ``IRC_CANDIDATE_RESUME`` for CVs. The rows include a signed ``FileUrl``;
    never forward it to a browser."""
    params: dict[str, Any] = {
        "orderBy": "LastUpdateDate:desc",
        "limit": max(1, min(limit, 200)),
        "onlyData": "false",
    }
    if category:
        params["q"] = eq("CategoryName", category)
    rows, _, _ = await client.get_collection(
        f"{CANDIDATES}/{candidate_number}/child/attachments", params
    )
    return rows


def enclosure_key(row: dict[str, Any]) -> str:
    """The FileContents enclosure key in an attachment row's ``links``, or
    ``""`` when the row has none (or the links were stripped).

    The href ends ``.../attachments/<key>/enclosure/FileContents``, and the key
    is a long hex string that is NOT ``AttachedDocumentId``."""
    links = row.get("links")
    if not isinstance(links, list):
        return ""
    for link in links:
        if not isinstance(link, dict) or link.get("name") != "FileContents":
            continue
        href = str(link.get("href") or "")
        marker = "/attachments/"
        start = href.find(marker)
        if start == -1:
            continue
        key, _, tail = href[start + len(marker) :].partition("/enclosure/")
        if key and tail:
            return key
    return ""


async def download_attachment(
    client: OracleFusionClient, candidate_number: str, key: str
) -> tuple[bytes, str]:
    """The attachment's bytes and Oracle's content type.

    Oracle serves every enclosure as ``application/octet-stream`` whatever it
    is, so prefer the row's own ``UploadedFileContentType`` when you have it.
    Check first that ``key`` belongs to this candidate (it came from
    :func:`candidate_attachments` for the same number), or a key can be
    replayed against another candidate's record."""
    return await client.get_bytes(
        f"{CANDIDATES}/{candidate_number}/child/attachments/{key}/enclosure/FileContents"
    )
