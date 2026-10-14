"""The clock seam (DR 0004 R-9 analog): every temporal check asks this module
for "today" instead of calling ``date.today()`` directly, so a host can supply
a timezone-aware answer (the user's today, not the server's) and tests can
freeze time. Without configuration the server-local date is used — correct for
single-timezone deployments, documented as the fallback."""

from datetime import date, datetime
from typing import Callable, Optional

_clock: Optional[Callable[[], date]] = None


def configure_clock(fn: Optional[Callable[[], date]]) -> None:
    """Install (or, with ``None``, remove) the host's today-provider — once,
    at startup. The callable returns a ``date``, typically the current date
    in the deployment's timezone. The slot is a plain process-global: do NOT
    reconfigure it per request (concurrent requests would race). A host that
    needs per-request timezones installs ONE callable that reads its own
    request-context (e.g. a ContextVar it manages) and answers accordingly."""
    global _clock
    _clock = fn


def today() -> date:
    value = _clock() if _clock else date.today()
    return value.date() if isinstance(value, datetime) else value


def years_ago(anchor: date, years: int) -> date:
    """``anchor`` shifted back ``years`` years, clamping a Feb-29 anchor to
    Feb-28 in a non-leap target year. Shared by the checks and the engine."""
    try:
        return anchor.replace(year=anchor.year - years)
    except ValueError:
        return anchor.replace(month=2, day=28, year=anchor.year - years)


def years_ahead(anchor: date, years: int) -> date:
    """``anchor`` shifted forward ``years`` years, with the same Feb-29 clamp."""
    try:
        return anchor.replace(year=anchor.year + years)
    except ValueError:
        return anchor.replace(month=2, day=28, year=anchor.year + years)


def date_of(value):
    """Datetime→date truncation, THE one copy of the policy (calendar-naive:
    an aware datetime truncates on its own calendar; hosts needing
    timezone-exact day boundaries convert before validating)."""
    return value.date() if isinstance(value, datetime) else value
