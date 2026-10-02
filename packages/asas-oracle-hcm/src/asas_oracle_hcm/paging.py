"""Paging a collection without walking into the limits Fusion puts on it.

Fusion refuses some requests outright rather than trimming them, and which
resources do so is a property of the resource, not of any product:

* a **page cap**: a ``limit`` over the cap is refused (200 on most resources,
  :data:`asas_oracle_hcm.MAX_PAGE_SIZE`);
* an **offset ceiling**: an ``offset`` at or past it is refused (FND-2420, at
  10,000 on the resources that have one), and so is the WHOLE request when
  ``offset + limit`` crosses it, not just the part past it. Such a collection
  cannot be paged to its end; getting past the ceiling means slicing it with
  ``q`` (by date, say) and paging each slice.

At the ceiling Fusion also reports ``hasMore=false``, which reads as "that was
the last row" when it means "that is the last I will page to".
:func:`capped_page` clamps the request, trims the last reachable page, answers
an offset past the ceiling without asking, and says ``at_ceiling`` so a caller
can tell the two apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .client import MAX_PAGE_SIZE, OracleFusionClient

#: The offset ceiling the resources that have one share.
DEFAULT_OFFSET_CEILING = 10_000


@dataclass(frozen=True)
class CappedPage:
    """One page. ``at_ceiling`` is True when this page reaches the offset
    ceiling: there may be more rows, but none reachable by paging, so
    ``has_more`` is False there for the reason in the module docstring."""

    items: list[dict[str, Any]]
    limit: int
    offset: int
    has_more: bool
    at_ceiling: bool


async def capped_page(
    client: OracleFusionClient,
    path: str,
    *,
    limit: int = 25,
    offset: int = 0,
    params: dict[str, Any] | None = None,
    page_cap: int = MAX_PAGE_SIZE,
    offset_ceiling: int | None = None,
    use_cache: bool = True,
) -> CappedPage:
    """A page of ``path`` that never asks for what Fusion would refuse.

    ``offset_ceiling=None`` means the resource has none (paging is clamped to
    ``page_cap`` only); pass :data:`DEFAULT_OFFSET_CEILING` for one that does.
    ``params`` are sent as given (``q``, ``fields``, ``orderBy``); ``limit``
    and ``offset`` here win over any in ``params``."""
    if page_cap < 1:
        raise ValueError("page_cap must be at least 1")
    limit = max(1, min(limit, page_cap))
    offset = max(0, offset)
    if offset_ceiling is not None:
        if offset >= offset_ceiling:
            return CappedPage([], limit, offset, False, True)
        limit = min(limit, offset_ceiling - offset)
    rows, has_more, _ = await client.get_collection(
        path, {**(params or {}), "limit": limit, "offset": offset}, use_cache=use_cache
    )
    at_ceiling = offset_ceiling is not None and offset + limit >= offset_ceiling
    return CappedPage(rows, limit, offset, has_more and not at_ceiling, at_ceiling)
