"""Turning Oracle's bare ids into words, and finding people.

**One request per id, concurrently, and that is not a shortcut.** A Fusion pod
answers a filtered lookup quickly (about 0.2s for a grade, 2s for a department)
but refuses several ids in one query: ``IN (a,b)`` and an ``or`` chain both
500, exactly as on Fusion's other filtered collections. Fetching a table whole is worse
still: one 500-row page of grades took seventy seconds on a real instance, so
the pages behind thousands of grades and departments would take most of an
hour. Per id, bounded concurrency, remembered, is the only shape a real
instance supports.

Everything here is FAIL-SOFT: a lookup that will not answer costs a name, never
the caller's request. The id is what the caller holds either way.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Mapping, Sequence

from .client import MAX_PAGE_SIZE, OracleFusionClient
from .errors import OracleQueryError
from .query import and_, child_items, eq, flag, text
from .store import LookupStore, StoredAnswer

logger = logging.getLogger(__name__)

#: How many lookups run at once. Measured against a real pod on the 25 people a
#: page of 25 records names: sequential 10.9s, six at a time 5.8s, twelve at a
#: time 1.6s with no failures. Twelve is where the curve flattens and is still a
#: modest ask of a shared HR system.
DEFAULT_CONCURRENCY = 12

#: What each id kind is called upstream: ``(resource, id field, name field)``.
#: ``department`` is keyed on ``OrganizationId``, NOT ``DepartmentId``: in
#: Oracle's model a department IS an organization, and the id a record or an
#: assignment carries as ``DepartmentId`` is that organization's id.
NAME_LOOKUPS: dict[str, tuple[str, str, str]] = {
    "grade": ("/grades", "GradeId", "GradeName"),
    "department": ("/departments", "OrganizationId", "Name"),
    # The plain /businessUnitsLOV is a 404 on at least one production-shaped
    # pod; the HCM LOV answers.
    "business_unit": ("/hcmBusinessUnitsLOV", "BusinessUnitId", "Name"),
    "organization": ("/organizations", "OrganizationId", "Name"),
    "job": ("/jobs", "JobId", "Name"),
    "position": ("/positions", "PositionId", "Name"),
    "job_family": ("/jobFamilies", "JobFamilyId", "JobFamilyName"),
    "person": ("/publicWorkers", "PersonId", "DisplayName"),
}

WORKERS = "/publicWorkers"
DEPARTMENTS = "/departments"
POSITIONS = "/positions"

#: The store kinds this module writes besides the :data:`NAME_LOOKUPS` kinds.
DIRECTORY_KIND = "directory"  # a person's name and address
POSITION_BUDGET_KIND = "position_budget"  # BudgetedPositionFlag, "Y"/"N"/""

#: The placeholder a NON-PRODUCTION Oracle pod writes over every worker's work
#: address, so a test instance can never mail a real person. It is one address
#: shared by the whole directory, so it is treated as absent, never matched on.
SCRUBBED_WORK_EMAIL = "sendmail-test-discard@oracle.com"


def worker_address(row: dict[str, Any]) -> str:
    """The usable email address on a ``/publicWorkers`` row, or ``""``.

    ``WorkEmail`` first, ``Username`` second. On a non-production pod the first
    is scrubbed and ``Username`` still carries the real address, so the
    fallback is what makes a test instance usable, and the order is what keeps
    production correct. A ``Username`` without an ``@`` is a login name, not an
    address."""
    work = text(row, "WorkEmail").strip()
    if work and work.lower() != SCRUBBED_WORK_EMAIL:
        return work
    username = text(row, "Username").strip()
    return username if "@" in username else ""


@dataclass(frozen=True)
class Person:
    """A worker as the directory names them."""

    person_id: str
    display_name: str
    address: str


@dataclass(frozen=True)
class Position:
    """An HR position: its name and whether it is BUDGETED.

    ``budgeted`` is ``None`` when Oracle left ``BudgetedPositionFlag`` unset,
    which is not the same answer as ``False``."""

    position_id: str
    name: str
    budgeted: bool | None


def _budget_flag(value: Any) -> bool | None:
    raw = str(value or "").strip().upper()
    if raw in ("Y", "TRUE", "1"):
        return True
    if raw in ("N", "FALSE", "0"):
        return False
    return None


@dataclass(frozen=True)
class WorkerDepartment:
    """Where Oracle places one worker: the department on their PRIMARY
    assignment. ``department_id`` is an ``OrganizationId``, the key
    ``/departments`` uses."""

    person_id: str
    department_id: str
    department_name: str


class _Memo:
    """``id -> value`` with a per-entry age, so an answer is reused for
    ``ttl`` seconds and then asked for again."""

    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._values: dict[str, Any] = {}
        self._at: dict[str, float] = {}

    def get(self, key: str, now: float) -> Any:
        if key in self._values and now - self._at.get(key, 0.0) < self.ttl:
            return self._values[key]
        return None

    def put(self, key: str, value: Any, now: float) -> None:
        self._values[key] = value
        self._at[key] = now

    def clear(self) -> None:
        self._values.clear()
        self._at.clear()


class OracleLookups:
    """Id-to-name and people lookups over one :class:`OracleFusionClient`.

    Hold ONE per process: it remembers answers (names for six hours, people for
    an hour by default), because a page of records points at the same handful
    of grades and people over and over. It deduplicates, so callers can pass
    ids straight off their rows.

    Pass a :class:`LookupStore` to keep answers across processes and restarts
    (see :mod:`asas_oracle_hcm.store` for the rules it gets: stale answers
    served and refreshed in the background, negative answers kept, refreshes
    that skip the client's read cache). The freshness windows are the same two
    TTLs. Background refreshes are tasks this object holds; ``await
    lookups.drain()`` waits for them (in tests, or before shutdown).
    """

    def __init__(
        self,
        client: OracleFusionClient,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        name_ttl_seconds: float = 21_600.0,
        people_ttl_seconds: float = 3_600.0,
        store: LookupStore | None = None,
        kinds: Mapping[str, tuple[str, str, str]] | None = None,
    ) -> None:
        """``kinds`` adds to (or overrides) :data:`NAME_LOOKUPS`: ``{kind:
        (resource, id field, name field)}`` for whatever else a product names
        ids against (``{"location": ("/locations", "LocationId",
        "LocationName")}``)."""
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._client = client
        self._concurrency = concurrency
        self._name_ttl = name_ttl_seconds
        self._people_ttl = people_ttl_seconds
        self._kinds: dict[str, tuple[str, str, str]] = {**NAME_LOOKUPS, **dict(kinds or {})}
        for kind, spec in self._kinds.items():
            if len(spec) != 3 or not all(isinstance(part, str) and part for part in spec):
                raise ValueError(f"lookup kind {kind!r} needs (resource, id field, name field)")
        self._names = {kind: _Memo(name_ttl_seconds) for kind in self._kinds}
        self._people = _Memo(people_ttl_seconds)
        self._positions = _Memo(name_ttl_seconds)
        self._store = store
        self._background: set[asyncio.Task[Any]] = set()

    @property
    def kinds(self) -> dict[str, tuple[str, str, str]]:
        """Every kind this instance can name: ``{kind: (resource, id, name)}``."""
        return dict(self._kinds)

    def clear(self) -> None:
        """Forget every answer held in MEMORY (after a catalogue edit, or in
        tests). The store, if any, is the store's to clear."""
        for memo in self._names.values():
            memo.clear()
        self._people.clear()
        self._positions.clear()

    async def drain(self) -> None:
        """Wait for the background refreshes in flight."""
        while self._background:
            await asyncio.gather(*list(self._background), return_exceptions=True)

    def _semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(self._concurrency)

    # -- the store --------------------------------------------------------------

    @property
    def _use_cache(self) -> bool:
        # With a store, the store is the shared layer and decides when Oracle
        # must be asked; a client cache under it could only answer with a copy
        # no newer than the one it just judged out of date.
        return self._store is None

    @staticmethod
    def _fresh(answer: StoredAnswer, ttl: float) -> bool:
        fetched = answer.fetched_at
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched).total_seconds() < ttl

    async def _read(self, kind: str, ids: Sequence[str]) -> Mapping[str, StoredAnswer]:
        if self._store is None or not ids:
            return {}
        try:
            return await self._store.read(kind, list(ids))
        except Exception:  # noqa: BLE001 - a store is an accelerator, never a dependency
            logger.warning("oracle lookup store read failed for %s", kind, exc_info=True)
            return {}

    async def _write(self, kind: str, answers: Mapping[str, tuple[str, Mapping[str, Any] | None]]) -> None:
        if self._store is None or not answers:
            return
        try:
            await self._store.write(kind, answers)
        except Exception:  # noqa: BLE001
            logger.warning("oracle lookup store write failed for %s", kind, exc_info=True)

    def _refresh_later(self, make: Callable[[], Coroutine[Any, Any, Any]]) -> None:
        try:
            task: asyncio.Task[Any] = asyncio.get_running_loop().create_task(make())
        except RuntimeError:  # pragma: no cover - no loop, nothing to schedule
            return
        self._background.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                logger.warning("oracle lookup refresh failed", exc_info=t.exception())

        task.add_done_callback(done)

    def _split(
        self, stored: Mapping[str, StoredAnswer], ttl: float
    ) -> tuple[set[str], list[str]]:
        """``(answered, stale)``: ids the store settles now, and those of them
        to refresh behind the caller."""
        answered: set[str] = set()
        stale: list[str] = []
        for oracle_id, answer in stored.items():
            answered.add(oracle_id)
            if not self._fresh(answer, ttl):
                stale.append(oracle_id)
        return answered, sorted(stale)

    # -- names -----------------------------------------------------------------

    async def _fetch_names(
        self, kind: str, values: Sequence[str], limit: asyncio.Semaphore
    ) -> dict[str, str]:
        """Ask Oracle. ``{id: name}`` for every id that ANSWERED, ``""`` for one
        Oracle does not know; ids whose request failed are absent."""
        resource, key, name_field = self._kinds[kind]

        async def fetch(value: str) -> tuple[str, str]:
            async with limit:
                rows, _, _ = await self._client.get_collection(
                    resource,
                    {"q": eq(key, value, quote=False), "limit": 1, "fields": f"{key},{name_field}"},
                    use_cache=self._use_cache,
                )
            return value, text(rows[0], name_field) if rows else ""

        results = await asyncio.gather(*(fetch(v) for v in values), return_exceptions=True)
        answered: dict[str, str] = {}
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            answered[result[0]] = result[1]
        if failures:
            logger.warning("oracle %s lookup failed for %d of %d ids", kind, failures, len(values))
        return answered

    async def _ask_names(
        self, kind: str, values: Sequence[str], limit: asyncio.Semaphore
    ) -> dict[str, str]:
        answered = await self._fetch_names(kind, values, limit)
        await self._write(kind, {v: (n, None) for v, n in answered.items()})
        memo, now = self._names[kind], time.monotonic()
        for value, name in answered.items():
            if name:
                memo.put(value, name, now)
        return answered

    async def names(
        self,
        kind: str,
        ids: Sequence[str],
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> dict[str, str]:
        """``{id: Oracle's name}`` for one kind (:data:`NAME_LOOKUPS`, plus any
        the constructor was given in ``kinds``).

        Ids Oracle does not know, and ids whose lookup failed, are absent from
        the answer rather than mapped to ``""``."""
        if kind not in self._kinds:
            raise ValueError(f"no Oracle name lookup for {kind!r}")
        memo = self._names[kind]
        now = time.monotonic()
        wanted = {str(i) for i in ids if i}
        out: dict[str, str] = {}
        for value in wanted:
            hit = memo.get(value, now)
            if hit is not None:
                out[value] = hit
        missing = sorted(wanted - out.keys())
        if not missing:
            return out
        stored = await self._read(kind, missing)
        answered, stale = self._split(stored, self._name_ttl)
        for value in answered:
            name = stored[value].name
            if name:
                out[value] = name
                memo.put(value, name, now)
        limit = semaphore or self._semaphore()
        unknown = [v for v in missing if v not in answered]
        if unknown:
            for value, name in (await self._ask_names(kind, unknown, limit)).items():
                if name:
                    out[value] = name
        if stale:
            self._refresh_later(lambda: self._ask_names(kind, stale, self._semaphore()))
        return out

    async def names_many(self, wanted: Mapping[str, Sequence[str]]) -> dict[str, dict[str, str]]:
        """Several kinds at once, under ONE concurrency budget (a budget per
        kind would put ``concurrency x kinds`` requests in flight)."""
        semaphore = self._semaphore()
        kinds = list(wanted)
        found = await asyncio.gather(
            *(self.names(kind, wanted[kind], semaphore=semaphore) for kind in kinds)
        )
        return dict(zip(kinds, found))

    # -- people ----------------------------------------------------------------

    async def _ask_people(self, person_ids: Sequence[str]) -> dict[str, Person | None]:
        """Ask Oracle. ``{pid: Person}`` for every id that answered, ``None``
        for one the directory does not hold; failed requests are absent."""
        semaphore = self._semaphore()

        async def fetch(pid: str) -> tuple[str, Person | None]:
            async with semaphore:
                rows, _, _ = await self._client.get_collection(
                    WORKERS,
                    {
                        "q": eq("PersonId", pid, quote=False),
                        "limit": 1,
                        "fields": "PersonId,DisplayName,WorkEmail,Username",
                    },
                    use_cache=self._use_cache,
                )
            if not rows:
                return pid, None
            return pid, Person(pid, text(rows[0], "DisplayName"), worker_address(rows[0]))

        results = await asyncio.gather(*(fetch(p) for p in person_ids), return_exceptions=True)
        answered: dict[str, Person | None] = {}
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            answered[result[0]] = result[1]
        if failures:
            logger.warning("oracle worker lookup failed for %d of %d people", failures, len(person_ids))
        await self._write(
            DIRECTORY_KIND,
            {
                pid: (person.display_name, {"address": person.address}) if person else ("", None)
                for pid, person in answered.items()
            },
        )
        now = time.monotonic()
        for pid, person in answered.items():
            if person is not None:
                self._people.put(pid, person, now)
        return answered

    async def people(self, person_ids: Sequence[str]) -> dict[str, Person]:
        """``{person id: Person}`` for the people Oracle's directory knows.

        ``/publicWorkers`` lists CURRENT workers only, so a suspended or former
        worker is absent from the answer. A worker with no usable address is
        still returned (``address == ""``)."""
        now = time.monotonic()
        wanted = {str(p) for p in person_ids if p}
        out: dict[str, Person] = {}
        for pid in wanted:
            hit = self._people.get(pid, now)
            if hit is not None:
                out[pid] = hit
        missing = sorted(wanted - out.keys())
        if not missing:
            return out
        stored = await self._read(DIRECTORY_KIND, missing)
        answered, stale = self._split(stored, self._people_ttl)
        for pid in answered:
            held = stored[pid]
            if held.name or (held.extra or {}).get("address"):
                person = Person(pid, held.name, str((held.extra or {}).get("address") or ""))
                out[pid] = person
                self._people.put(pid, person, now)
        unknown = [p for p in missing if p not in answered]
        if unknown:
            for pid, asked in (await self._ask_people(unknown)).items():
                if asked is not None:
                    out[pid] = asked
        if stale:
            self._refresh_later(lambda: self._ask_people(stale))
        return out

    # -- positions ---------------------------------------------------------------

    async def _ask_positions(self, ids: Sequence[str]) -> dict[str, Position | None]:
        semaphore = self._semaphore()

        async def fetch(pid: str) -> tuple[str, Position | None]:
            async with semaphore:
                rows, _, _ = await self._client.get_collection(
                    POSITIONS,
                    {
                        "q": eq("PositionId", pid, quote=False),
                        "limit": 1,
                        "fields": "PositionId,Name,BudgetedPositionFlag",
                    },
                    use_cache=self._use_cache,
                )
            if not rows:
                return pid, None
            return pid, Position(
                pid, text(rows[0], "Name"), _budget_flag(rows[0].get("BudgetedPositionFlag"))
            )

        results = await asyncio.gather(*(fetch(p) for p in ids), return_exceptions=True)
        answered: dict[str, Position | None] = {}
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            answered[result[0]] = result[1]
        if failures:
            logger.warning("oracle position lookup failed for %d of %d ids", failures, len(ids))
        flag_text = {True: "Y", False: "N", None: ""}
        await self._write("position", {p: (pos.name if pos else "", None) for p, pos in answered.items()})
        await self._write(
            POSITION_BUDGET_KIND,
            {p: (flag_text[pos.budgeted] if pos else "", None) for p, pos in answered.items()},
        )
        now = time.monotonic()
        for pid, pos in answered.items():
            if pos is not None:
                self._positions.put(pid, pos, now)
                if pos.name:
                    self._names["position"].put(pid, pos.name, now)
        return answered

    async def positions(self, position_ids: Sequence[str]) -> dict[str, Position]:
        """``{position id: Position}``: the name AND the budget flag, one request
        per position for both (asking ``names("position")`` and then the flag
        separately would read every position twice). Ids Oracle does not know,
        and failed lookups, are absent. Kept in the store as ``position`` and
        :data:`POSITION_BUDGET_KIND`."""
        now = time.monotonic()
        wanted = {str(p) for p in position_ids if p}
        out: dict[str, Position] = {}
        for pid in wanted:
            hit = self._positions.get(pid, now)
            if hit is not None:
                out[pid] = hit
        missing = sorted(wanted - out.keys())
        if not missing:
            return out
        names = await self._read("position", missing)
        flags = await self._read(POSITION_BUDGET_KIND, missing)
        both = {p: names[p] for p in names if p in flags}
        answered, stale = self._split(both, self._name_ttl)
        for pid in answered:
            if both[pid].name or flags[pid].name:
                position = Position(pid, both[pid].name, _budget_flag(flags[pid].name))
                out[pid] = position
                self._positions.put(pid, position, now)
        unknown = [p for p in missing if p not in answered]
        if unknown:
            for pid, pos in (await self._ask_positions(unknown)).items():
                if pos is not None:
                    out[pid] = pos
        if stale:
            self._refresh_later(lambda: self._ask_positions(stale))
        return out

    async def find_worker(self, address: str, *, expand: str | None = None) -> dict[str, Any] | None:
        """The ``/publicWorkers`` row behind an email address, or ``None``.

        Looked up by ``WorkEmail`` first and ``Username`` second (the order
        :func:`worker_address` reads them in, so a non-production pod whose work
        addresses are scrubbed still matches), each as given and then
        lowercased, because Oracle's equality is case-sensitive. The value is
        quoted, the form string filters are known to take. Up to four requests;
        the first hit wins. Fail-soft."""
        address = (address or "").strip()
        if not address or "@" not in address or not self._client.configured:
            return None
        spellings = list(dict.fromkeys([address, address.lower()]))
        params: dict[str, Any] = {"limit": 1}
        if expand:
            params["expand"] = expand
        try:
            for field_name in ("WorkEmail", "Username"):
                for value in spellings:
                    try:
                        clause = eq(field_name, value)
                    except OracleQueryError:
                        # An address the q grammar cannot express (a quote in
                        # it) cannot be looked up this way: no match, no guess.
                        continue
                    rows, _, _ = await self._client.get_collection(
                        WORKERS, {**params, "q": clause}
                    )
                    if rows:
                        return rows[0]
        except Exception:  # noqa: BLE001 - enrichment; never fails the caller
            logger.warning("oracle worker lookup by address failed", exc_info=True)
        return None

    async def worker_department(self, address: str) -> WorkerDepartment | None:
        """Where Oracle places the worker behind ``address``: the department on
        their primary assignment (``PrimaryFlag``), or the first assignment that
        names one. ``None`` when the address is unknown or nothing names a
        department. Fail-soft."""
        row = await self.find_worker(address, expand="assignments")
        if row is None:
            return None
        placed = [a for a in child_items(row, "assignments") if text(a, "DepartmentId")]
        if not placed:
            return None
        primary = next(
            (a for a in placed if flag(a, "PrimaryFlag") or flag(a, "PrimaryAssignmentFlag")),
            placed[0],
        )
        return WorkerDepartment(
            person_id=text(row, "PersonId"),
            department_id=text(primary, "DepartmentId"),
            department_name=text(primary, "DepartmentName").strip(),
        )

    # -- departments -----------------------------------------------------------

    async def departments_in_set(
        self, set_code: str, *, active_only: bool = True
    ) -> list[tuple[str, str]]:
        """Every department in one reference set, as ``(OrganizationId, Name)``.

        ``/departments`` carries no business unit: it is partitioned by Fusion's
        reference sets (``SetCode``), so an entity's departments are found by
        set code. A pod can hold many thousands of departments across sets, and
        many retired ones, hence ``active_only``. Paged to exhaustion in id
        order, so the window cannot shift under the walk. NOT fail-soft: a
        caller syncing a catalogue must know it got the whole list."""
        code = (set_code or "").strip()
        if not code:
            return []
        clauses = [eq("SetCode", code)]
        if active_only:
            clauses.append(eq("ActiveStatus", "A"))
        out: list[tuple[str, str]] = []
        async for row in self._client.iter_collection(
            DEPARTMENTS,
            {"orderBy": "OrganizationId:asc", "fields": "OrganizationId,Name", "q": and_(*clauses)},
            page_size=MAX_PAGE_SIZE,
        ):
            oracle_id, name = text(row, "OrganizationId"), text(row, "Name").strip()
            if oracle_id and name:
                out.append((oracle_id, name))
        return out
