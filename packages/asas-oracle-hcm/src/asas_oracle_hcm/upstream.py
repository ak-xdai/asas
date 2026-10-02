"""How the Oracle upstream is behaving, from this process's side.

Three things, all in memory and per client (each process keeps its own, which
is the honest scope: a process that cannot reach the gateway is the one that
should stop asking):

* :class:`Breaker`, a circuit breaker. After ``failures`` consecutive faults
  (a transport error, a 5xx, a 429; never another 4xx, which is an answer)
  READS fail at once with :class:`OracleUnavailableError` for
  ``cooldown_seconds``, instead of each waiting out the timeout against a
  gateway that is down. Then ONE probe goes out: success closes the breaker, a
  fault opens it again. Writes are counted but never refused, because the
  caller's outbox owns retry and cadence. ``failures=0`` turns it off.
* Call statistics per method and resource: calls, faults, average and worst
  latency, for a status page (:meth:`UpstreamHealth.snapshot`).
* A per-request call counter in a context variable (:func:`count_calls`), so a
  host can log how many Oracle calls one of its requests made. A request path
  built to make none is how an integration stays fast; this is what notices
  when one starts to.
"""

from __future__ import annotations

import contextvars
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

# [calls, closed]. Background work a request spawns copies the context, so the
# counter is CLOSED when the request ends rather than unset: a call that work
# makes later counts against nothing.
_calls: contextvars.ContextVar[list[int] | None] = contextvars.ContextVar(
    "asas_oracle_hcm_calls", default=None
)


class CallCount:
    """What :func:`count_calls` yields: ``calls`` so far in this context."""

    def __init__(self, counter: list[int]) -> None:
        self._counter = counter

    @property
    def calls(self) -> int:
        return self._counter[0]


@contextmanager
def count_calls() -> Iterator[CallCount]:
    """Count the Oracle requests made inside the block (and by work it awaits).

    ::

        with count_calls() as counted:
            response = await call_next(request)
        log.info("request", oracle_calls=counted.calls)

    Cache hits are not calls. Work spawned inside the block that finishes after
    it is not counted."""
    counter = [0, 0]
    token = _calls.set(counter)
    try:
        yield CallCount(counter)
    finally:
        counter[1] = 1
        _calls.reset(token)


def _count_call() -> None:
    counter = _calls.get()
    if counter is not None and not counter[1]:
        counter[0] += 1


@dataclass
class Breaker:
    """A consecutive-fault circuit breaker. See the module docstring."""

    failures: int = 5
    cooldown_seconds: float = 30.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    consecutive: int = field(default=0, init=False)
    opened_at: float | None = field(default=None, init=False)
    opened_wall: datetime | None = field(default=None, init=False)
    probing: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.failures < 0:
            raise ValueError("failures must be 0 (off) or more")
        if self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")

    @property
    def enabled(self) -> bool:
        return self.failures > 0

    @property
    def state(self) -> str:
        """``closed``, ``open``, ``half_open`` or ``disabled``."""
        if not self.enabled:
            return "disabled"
        if self.opened_at is None:
            return "closed"
        if self.clock() - self.opened_at >= self.cooldown_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """Whether a READ may go out now. In half-open, exactly one probe."""
        if not self.enabled or self.opened_at is None:
            return True
        if self.state == "open" or self.probing:
            return False
        self.probing = True
        return True

    def release_probe(self) -> None:
        """A probe ended with no verdict (cancelled): let the next one try."""
        self.probing = False

    def record(self, *, fault: bool) -> None:
        if not fault:
            self.consecutive = 0
            self.opened_at = None
            self.opened_wall = None
            self.probing = False
            return
        self.consecutive += 1
        was_probe = self.probing
        self.probing = False
        if self.enabled and (was_probe or self.consecutive >= self.failures):
            self.opened_at = self.clock()
            self.opened_wall = datetime.now(timezone.utc)

    def retry_after_seconds(self) -> int:
        if self.opened_at is None:
            return 0
        return max(0, int(self.cooldown_seconds - (self.clock() - self.opened_at)) + 1)


@dataclass
class _Stat:
    calls: int = 0
    faults: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0


class UpstreamHealth:
    """One client's breaker and call statistics."""

    def __init__(self, breaker: Breaker) -> None:
        self.breaker = breaker
        self.refused = 0
        self.since = datetime.now(timezone.utc)
        self._stats: dict[tuple[str, str], _Stat] = {}

    def record(self, method: str, resource: str, elapsed_ms: float, *, fault: bool) -> None:
        stat = self._stats.setdefault((method, resource), _Stat())
        stat.calls += 1
        stat.total_ms += elapsed_ms
        stat.max_ms = max(stat.max_ms, elapsed_ms)
        if fault:
            stat.faults += 1
        self.breaker.record(fault=fault)
        _count_call()

    def snapshot(self) -> dict[str, Any]:
        """A JSON-ready view for a status page."""
        rows = [
            {
                "method": method,
                "resource": resource,
                "calls": s.calls,
                "faults": s.faults,
                "avg_ms": int(s.total_ms / s.calls) if s.calls else 0,
                "max_ms": int(s.max_ms),
            }
            for (method, resource), s in sorted(self._stats.items())
        ]
        return {
            "breaker": self.breaker.state,
            "consecutive_faults": self.breaker.consecutive,
            "opened_at": self.breaker.opened_wall,
            "refused": self.refused,
            "since": self.since,
            "calls": sum(r["calls"] for r in rows),
            "faults": sum(r["faults"] for r in rows),
            "resources": rows,
        }
