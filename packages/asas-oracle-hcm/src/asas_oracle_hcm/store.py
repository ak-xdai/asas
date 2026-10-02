"""A persistent home for lookup answers, shared by every process of a host.

:class:`OracleLookups` remembers answers in process memory. That is lost on a
restart and not shared between replicas, so a new process asks Oracle again,
one request per id. A :class:`LookupStore` is the seam for keeping answers
somewhere durable and shared (a database table, typically), with these rules,
which the lookups apply and a store does not have to:

* **Stale-while-revalidate.** An answer older than its freshness is still
  served at once and refreshed in the background, so after the first sight of
  an id no caller waits on Oracle for it again.
* **Negative answers are kept.** "Oracle has no such id" is stored as an empty
  name and honoured while fresh, or every request would ask again. A lookup
  that FAILED is never stored: a failure is not an answer.
* **A refresh skips the client's read cache.** When the store decides its own
  answer is out of date, a cached copy of the same read is no newer, and
  answering from it would re-save the old answer as fresh.

The package ships :class:`MemoryLookupStore` (one process, for tests and small
hosts). A host with a database implements the two methods over a table keyed
on ``(kind, oracle_id)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class StoredAnswer:
    """One kept answer. ``name == ""`` means Oracle has no such id."""

    name: str
    extra: Mapping[str, Any] | None
    fetched_at: datetime


class LookupStore(Protocol):
    """Where lookup answers live between processes. Both methods may raise:
    the lookups treat a failing store as an empty one and carry on."""

    async def read(self, kind: str, ids: Sequence[str]) -> Mapping[str, StoredAnswer]:
        """The answers held for these ids (absent ids are simply missing)."""
        ...

    async def write(
        self, kind: str, answers: Mapping[str, tuple[str, Mapping[str, Any] | None]]
    ) -> None:
        """Keep ``{id: (name, extra)}``, stamped now, replacing what was held."""
        ...


class MemoryLookupStore:
    """A :class:`LookupStore` in one process's memory."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], StoredAnswer] = {}

    async def read(self, kind: str, ids: Sequence[str]) -> Mapping[str, StoredAnswer]:
        return {i: self._rows[(kind, i)] for i in ids if (kind, i) in self._rows}

    async def write(
        self, kind: str, answers: Mapping[str, tuple[str, Mapping[str, Any] | None]]
    ) -> None:
        now = datetime.now(timezone.utc)
        for oracle_id, (name, extra) in answers.items():
            self._rows[(kind, str(oracle_id))] = StoredAnswer(name or "", extra, now)

    def age(self, kind: str, oracle_id: str, fetched_at: datetime) -> None:
        """Test seam: backdate one answer."""
        held = self._rows[(kind, oracle_id)]
        self._rows[(kind, oracle_id)] = StoredAnswer(held.name, held.extra, fetched_at)

    def clear(self) -> None:
        self._rows.clear()
