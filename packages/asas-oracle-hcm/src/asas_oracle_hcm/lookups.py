"""Turning Oracle's bare ids into words, and finding people.

**One request per id, concurrently, and that is not a shortcut.** A Fusion pod
answers a filtered lookup quickly (about 0.2s for a grade, 2s for a department)
but refuses several ids in one query: ``IN (a,b)`` and an ``or`` chain both
500, exactly as on the recruiting resources. Fetching a table whole is worse
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
from typing import Any, Mapping, Sequence

from .client import MAX_PAGE_SIZE, OracleFusionClient
from .query import and_, child_items, eq, flag, text

logger = logging.getLogger(__name__)

#: How many lookups run at once. Measured against a real pod on the 25 people a
#: page of requisitions names: sequential 10.9s, six at a time 5.8s, twelve at a
#: time 1.6s with no failures. Twelve is where the curve flattens and is still a
#: modest ask of a shared HR system.
DEFAULT_CONCURRENCY = 12

#: What each id kind is called upstream: ``(resource, id field, name field)``.
#: ``department`` is keyed on ``OrganizationId``, NOT ``DepartmentId``: in
#: Oracle's model a department IS an organization, and the id a requisition or
#: an assignment carries as ``DepartmentId`` is that organization's id.
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
    ids straight off their rows. Answers are also subject to the client's own
    read cache; this memo is what saves the per-id fan-out itself.
    """

    def __init__(
        self,
        client: OracleFusionClient,
        *,
        concurrency: int = DEFAULT_CONCURRENCY,
        name_ttl_seconds: float = 21_600.0,
        people_ttl_seconds: float = 3_600.0,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        self._client = client
        self._concurrency = concurrency
        self._names = {kind: _Memo(name_ttl_seconds) for kind in NAME_LOOKUPS}
        self._people = _Memo(people_ttl_seconds)

    def clear(self) -> None:
        """Forget every remembered answer (after a catalogue edit, or in tests)."""
        for memo in self._names.values():
            memo.clear()
        self._people.clear()

    def _semaphore(self) -> asyncio.Semaphore:
        return asyncio.Semaphore(self._concurrency)

    # -- names -----------------------------------------------------------------

    async def names(
        self,
        kind: str,
        ids: Sequence[str],
        *,
        semaphore: asyncio.Semaphore | None = None,
    ) -> dict[str, str]:
        """``{id: Oracle's name}`` for one kind (see :data:`NAME_LOOKUPS`).

        Ids Oracle does not know, and ids whose lookup failed, are absent from
        the answer rather than mapped to ``""``."""
        if kind not in NAME_LOOKUPS:
            raise ValueError(f"no Oracle name lookup for {kind!r}")
        resource, key, name_field = NAME_LOOKUPS[kind]
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
        limit = semaphore or self._semaphore()

        async def fetch(value: str) -> tuple[str, str]:
            async with limit:
                rows, _, _ = await self._client.get_collection(
                    resource,
                    {"q": eq(key, value, quote=False), "limit": 1, "fields": f"{key},{name_field}"},
                )
            return value, text(rows[0], name_field) if rows else ""

        results = await asyncio.gather(*(fetch(v) for v in missing), return_exceptions=True)
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            value, name = result
            if name:
                out[value] = name
                memo.put(value, name, now)
        if failures:
            logger.warning("oracle %s lookup failed for %d of %d ids", kind, failures, len(missing))
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
                )
            if not rows:
                return pid, None
            return pid, Person(pid, text(rows[0], "DisplayName"), worker_address(rows[0]))

        results = await asyncio.gather(*(fetch(p) for p in missing), return_exceptions=True)
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            pid, person = result
            if person is not None:
                out[pid] = person
                self._people.put(pid, person, now)
        if failures:
            logger.warning("oracle worker lookup failed for %d of %d people", failures, len(missing))
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
        candidates = list(dict.fromkeys([address, address.lower()]))
        params: dict[str, Any] = {"limit": 1}
        if expand:
            params["expand"] = expand
        try:
            for field_name in ("WorkEmail", "Username"):
                for value in candidates:
                    rows, _, _ = await self._client.get_collection(
                        WORKERS, {**params, "q": eq(field_name, value)}
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
