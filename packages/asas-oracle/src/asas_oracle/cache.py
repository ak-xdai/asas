"""The read cache seam.

Oracle's reference data (a grade id and its name, a department, a worker's
display name) changes on the scale of days, while a lookup against a real pod
can take seconds. So reads are cached, but WHERE is the host's decision: one
process, or a store every replica shares. This package ships an in-memory
default and names the four operations a shared store must offer; a host with
Redis writes a ten-line adapter. The library imports no cache client.

Values are JSON-shaped dicts, so any store that can hold JSON fits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """What the client needs from a cache. Every method must be fail-soft:
    a store that is down answers ``None`` / ``0`` and swallows writes, so a
    cache outage costs speed, never a request."""

    async def get(self, key: str) -> dict[str, Any] | None: ...

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None: ...

    async def get_int(self, key: str) -> int: ...

    async def incr(self, key: str, ttl_seconds: int) -> int: ...


class MemoryCache:
    """A per-process cache with expiry. The default: right for one replica,
    and harmless for several (each keeps its own copy)."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[float, Any]] = {}

    def _live(self, key: str) -> Any:
        entry = self._values.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if time.monotonic() >= expires_at:
            self._values.pop(key, None)
            return None
        return value

    async def get(self, key: str) -> dict[str, Any] | None:
        value = self._live(key)
        return value if isinstance(value, dict) else None

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        if ttl_seconds > 0:
            self._values[key] = (time.monotonic() + ttl_seconds, value)

    async def get_int(self, key: str) -> int:
        value = self._live(key)
        return value if isinstance(value, int) else 0

    async def incr(self, key: str, ttl_seconds: int) -> int:
        value = await self.get_int(key) + 1
        self._values[key] = (time.monotonic() + max(ttl_seconds, 1), value)
        return value

    def clear(self) -> None:
        self._values.clear()


class NullCache:
    """Caches nothing. For a host that wants every read to reach Oracle."""

    async def get(self, key: str) -> dict[str, Any] | None:
        return None

    async def set(self, key: str, value: dict[str, Any], ttl_seconds: int) -> None:
        return None

    async def get_int(self, key: str) -> int:
        return 0

    async def incr(self, key: str, ttl_seconds: int) -> int:
        return 0


#: Reference data: an id and its name. Six hours.
LOOKUP_TTL_SECONDS = 21_600
#: How long an EMPTY collection is kept, whatever its resource's TTL. An empty
#: answer (a person, a grade Oracle has not got yet) is the one most likely to
#: change, and a new hire should not read as nobody until tomorrow.
EMPTY_ANSWER_TTL_SECONDS = 600

#: The HCM reference resources the lookups read: ids and their names, which
#: change on the scale of days.
REFERENCE_RESOURCES: tuple[str, ...] = (
    "grades",
    "departments",
    "hcmBusinessUnitsLOV",
    "organizations",
    "jobs",
    "positions",
    "jobFamilies",
    "publicWorkers",
)


def reference_ttls(seconds: int = LOOKUP_TTL_SECONDS) -> dict[str, int]:
    """``{resource: ttl}`` for :data:`REFERENCE_RESOURCES`, the default policy.

    A host caching its own resources too builds on it::

        CachePolicy(
            ttls={**reference_ttls(), "absences": 120},
            stale_on_write={"absences": ("absences",)},
        )
    """
    return {name: seconds for name in REFERENCE_RESOURCES}


@dataclass(frozen=True)
class CachePolicy:
    """Which resources are cached, for how long, and what a write makes stale.

    ``ttls`` maps a resource name (the first path segment, ``grades`` in
    ``/grades?q=...``) to seconds; a resource not listed is never cached, which
    is the right default for anything read to see what Oracle holds right now.
    The default caches the HCM reference data alone (:func:`reference_ttls`);
    which of its own resources a product caches, and for how long, is the
    product's call. ``stale_on_write`` maps a resource to the resources a POST
    or PATCH on it invalidates: each carries a version in its keys, and a write
    bumps it, so every cached read goes stale at once without scanning keys.
    """

    ttls: Mapping[str, int] = field(default_factory=reference_ttls)
    stale_on_write: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    empty_answer_ttl_seconds: int = EMPTY_ANSWER_TTL_SECONDS
