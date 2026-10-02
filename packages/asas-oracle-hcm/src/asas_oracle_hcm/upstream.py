"""How the Oracle upstream is behaving, from this process's side.

Three things, all in memory and per client (each process keeps its own, which
is the honest scope: a process that cannot reach the gateway is the one that
should stop asking):

* Circuit breakers, ONE PER RESOURCE plus one for the host
  (:class:`BreakerPolicy`). After ``failures`` consecutive faults READS of that
  resource fail at once with :class:`OracleUnavailableError` for
  ``cooldown_seconds``, instead of each waiting out the timeout; then ONE probe
  goes out, and success closes the breaker while a fault opens it again.

  **What counts as a fault** (:func:`is_outage`): a connection that failed or
  timed out, a 502/503/504, a 429. NOT a 500, and not any other 4xx: those are
  ANSWERS. A 500 is what a gateway returns for an operation registered wrong
  and what Fusion returns for a query it cannot run, so counting it would let
  one broken endpoint switch every read off. Per resource for the same reason:
  ``/positions`` failing must not stop ``/grades``. A connection failure is
  also counted against the HOST breaker, because when the host is down every
  resource is, and the host breaker stops them all after the same count.

  Writes are counted but never refused, because the caller's outbox owns
  retry and cadence. ``failures=0`` turns the breakers off.
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


#: Statuses that mean the upstream is unavailable rather than answering.
OUTAGE_STATUSES = frozenset({429, 502, 503, 504})


def is_outage(status: int | None) -> bool:
    """Whether a request's outcome counts against a breaker: no response at
    all (``None``), or one of :data:`OUTAGE_STATUSES`."""
    return status is None or status in OUTAGE_STATUSES


@dataclass(frozen=True)
class BreakerPolicy:
    """How every breaker of one client behaves. ``failures=0`` disables them."""

    failures: int = 5
    cooldown_seconds: float = 30.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.failures < 0:
            raise ValueError("failures must be 0 (off) or more")
        if self.cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be positive")


@dataclass
class Breaker:
    """One consecutive-fault circuit breaker (a resource's, or the host's)."""

    policy: BreakerPolicy = field(default_factory=BreakerPolicy)
    consecutive: int = field(default=0, init=False)
    opened_at: float | None = field(default=None, init=False)
    opened_wall: datetime | None = field(default=None, init=False)
    probing: bool = field(default=False, init=False)

    @property
    def enabled(self) -> bool:
        return self.policy.failures > 0

    @property
    def state(self) -> str:
        """``closed``, ``open``, ``half_open`` or ``disabled``."""
        if not self.enabled:
            return "disabled"
        if self.opened_at is None:
            return "closed"
        if self.policy.clock() - self.opened_at >= self.policy.cooldown_seconds:
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
        if self.enabled and (was_probe or self.consecutive >= self.policy.failures):
            self.opened_at = self.policy.clock()
            self.opened_wall = datetime.now(timezone.utc)

    def retry_after_seconds(self) -> int:
        if self.opened_at is None:
            return 0
        left = self.policy.cooldown_seconds - (self.policy.clock() - self.opened_at)
        return max(0, int(left) + 1)


@dataclass
class _Stat:
    calls: int = 0
    faults: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0


@dataclass(frozen=True)
class RequestEvent:
    """One request, as the ``on_request`` hook sees it. ``status`` is None when
    no response arrived; ``refused`` is True when a breaker answered instead
    of Oracle (then nothing was sent)."""

    method: str
    resource: str
    path: str
    status: int | None
    elapsed_ms: float
    fault: bool
    refused: bool = False


#: The breaker every connection failure also counts against.
HOST = "*"


class UpstreamHealth:
    """One client's breakers and call statistics."""

    def __init__(self, policy: BreakerPolicy | None = None) -> None:
        self.policy = policy or BreakerPolicy()
        self.refused = 0
        self.since = datetime.now(timezone.utc)
        self._breakers: dict[str, Breaker] = {}
        self._stats: dict[tuple[str, str], _Stat] = {}

    def breaker(self, resource: str) -> Breaker:
        """The breaker for one resource (``HOST`` for the host's)."""
        if resource not in self._breakers:
            self._breakers[resource] = Breaker(self.policy)
        return self._breakers[resource]

    def allow(self, resource: str) -> Breaker | None:
        """``None`` when a read of ``resource`` may go out, else the breaker
        that refused it (the host's first)."""
        for name in (HOST, resource):
            b = self.breaker(name)
            if not b.allow():
                return b
        return None

    def release(self, resource: str) -> None:
        for name in (HOST, resource):
            self.breaker(name).release_probe()

    def record(self, method: str, resource: str, elapsed_ms: float, *, status: int | None) -> bool:
        """Count one request; returns whether it was a fault."""
        fault = is_outage(status)
        stat = self._stats.setdefault((method, resource), _Stat())
        stat.calls += 1
        stat.total_ms += elapsed_ms
        stat.max_ms = max(stat.max_ms, elapsed_ms)
        if fault:
            stat.faults += 1
        self.breaker(resource).record(fault=fault)
        # The host breaker hears connection failures (no status) and any
        # successful answer; a single resource's 503 is that resource's affair.
        if status is None:
            self.breaker(HOST).record(fault=True)
        elif not fault:
            self.breaker(HOST).record(fault=False)
        _count_call()
        return fault

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
        breakers = {
            name: {"state": b.state, "consecutive_faults": b.consecutive, "opened_at": b.opened_wall}
            for name, b in sorted(self._breakers.items())
            if b.state not in ("closed", "disabled") or b.consecutive
        }
        if not self.policy.failures:
            overall = "disabled"
        elif any(b["state"] == "open" for b in breakers.values()):
            overall = "open"
        elif any(b["state"] == "half_open" for b in breakers.values()):
            overall = "half_open"
        else:
            overall = "closed"
        return {
            "breaker": overall,
            "breakers": breakers,
            "refused": self.refused,
            "since": self.since,
            "calls": sum(r["calls"] for r in rows),
            "faults": sum(r["faults"] for r in rows),
            "resources": rows,
        }
