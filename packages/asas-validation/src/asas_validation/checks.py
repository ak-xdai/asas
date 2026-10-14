"""THE file: the shipped library of checks — plain functions, nothing else.

Adding a check to your application means writing a function shaped like these
in your own checks file and handing that module to
:func:`asas_validation.include_checks` once at startup. There is no
registration call. A check:

- is named so it completes the sentence "this field must be ...";
- takes the value(s) under test first, settings as named arguments;
- takes ``field=`` so the error lands under the right form input, and an
  optional ``message_key=`` for hosts that render translations;
- returns ``None`` when valid, or a :class:`~asas_validation.engine.Violation`;
- passes when a value it tests is absent (partial edits supply only touched
  fields) — except the presence family, whose subject is absence itself;
- starts its docstring with the sentence shown to humans.

The ``validate`` namespace and ``catalog()`` (see ``library.py``) are built by
reading this module — the signature says what a check takes, the docstring's
first line is its sentence — so nothing is declared twice.
"""

import math
import re
import uuid as _uuid_mod
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlparse

from .clock import today as _today
from .clock import years_ago as _years_ago
from .clock import years_ahead as _years_ahead
from .engine import Violation

__all__ = []  # the public surface is the validate namespace, not this module


def _v(field: str, code: str, message: str, message_key: Optional[str], **params) -> Violation:
    return Violation(
        field=field,
        code=code,
        message=message.format(**params) if params else message,
        params=params,
        message_key=message_key or f"validation.{code}",
    )


def _d(value):
    """Date-normalize: a datetime meeting a date must compare, never crash.

    Truncation is calendar-naive: an aware datetime is truncated on its own
    calendar, not re-expressed in the host clock's timezone (the clock seam
    carries no timezone). Hosts that need timezone-exact day boundaries
    should convert datetimes to local dates before validating."""
    return value.date() if isinstance(value, datetime) else value


def _absent(value) -> bool:
    """The skip rule for non-presence checks: ``None`` and blank strings both
    mean "the field was not provided" — forms and CSV rows encode absence as
    the empty (or whitespace) string, and a comparison against it must skip,
    never crash."""
    return value is None or (isinstance(value, str) and value.strip() == "")


class _Invalid:
    """Sentinel: a provided value that cannot be read as the needed type. Not
    absent (the field WAS provided), so the check answers with a violation
    instead of skipping — and never with a TypeError that 500s the request."""


def _to_date(value):
    """date | datetime pass through (truncated); ISO strings parse (the JSON/
    CSV transport this library serves); anything else is _Invalid."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            return date.fromisoformat(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text).date()
            except ValueError:
                return _Invalid
    return _Invalid


def _to_num(value):
    if isinstance(value, bool):
        return _Invalid
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return _Invalid
    try:
        return float(value)  # Decimal and friends
    except (TypeError, ValueError):
        return _Invalid


def _invalid_date(field, message_key):
    return _v(field, "invalid_date", "Must be a valid date", message_key)


def _invalid_number(field, message_key):
    return _v(field, "invalid_number", "Must be a valid number", message_key)


def _present(value) -> bool:
    return value is not None and value != ""


# ── dates and time ────────────────────────────────────────────────────────────

def not_in_future(value, *, field="", message_key=None):
    """{field} must not be in the future"""
    if _absent(value): return None
    value = _to_date(value)
    if value is _Invalid: return _invalid_date(field, message_key)
    if value > _today():
        return _v(field, "not_in_future", "Must not be in the future", message_key)
    return None


def not_in_past(value, *, field="", message_key=None):
    """{field} must not be in the past"""
    if _absent(value): return None
    value = _to_date(value)
    if value is _Invalid: return _invalid_date(field, message_key)
    if value < _today():
        return _v(field, "not_in_past", "Must not be in the past", message_key)
    return None


def after(value, reference, *, days=0, field="", message_key=None):
    """{field} must be after {reference}, by at least {days} day(s)"""
    if _absent(value) or _absent(reference): return None
    value, reference = _to_date(value), _to_date(reference)
    if value is _Invalid or reference is _Invalid:
        return _invalid_date(field, message_key)
    v, limit = value, reference + timedelta(days=days)
    # days=0 means strictly after — the name and the sentence promise it;
    # with a gap, "at least N days after" is inclusive at exactly N.
    if v < limit or (days == 0 and v == limit):
        return _v(field, "after",
                  "Must be after {reference}" if days == 0
                  else "Must be at least {days} day(s) after {reference}",
                  message_key, days=days, reference=str(reference))
    return None


def before(value, reference, *, days=0, field="", message_key=None):
    """{field} must be before {reference}, by at least {days} day(s)"""
    if _absent(value) or _absent(reference): return None
    value, reference = _to_date(value), _to_date(reference)
    if value is _Invalid or reference is _Invalid:
        return _invalid_date(field, message_key)
    v, limit = value, reference - timedelta(days=days)
    if v > limit or (days == 0 and v == limit):
        return _v(field, "before",
                  "Must be before {reference}" if days == 0
                  else "Must be at least {days} day(s) before {reference}",
                  message_key, days=days, reference=str(reference))
    return None


def between(value, start, end, *, field="", message_key=None):
    """{field} must fall between {start} and {end}"""
    if _absent(value) or _absent(start) or _absent(end): return None
    value, start, end = _to_date(value), _to_date(start), _to_date(end)
    if _Invalid in (value, start, end):
        return _invalid_date(field, message_key)
    if not (start <= value <= end):
        return _v(field, "between", "Must fall between {start} and {end}",
                  message_key, start=str(start), end=str(end))
    return None


def _age_window(years, days):
    if (years is None) == (days is None):
        raise TypeError("pass exactly one of years= or days=")
    return _years_ago(_today(), years) if years is not None else _today() - timedelta(days=days)


def not_older_than(value, *, years=None, days=None, field="", message_key=None):
    """{field} must be no older than {years} year(s) / {days} day(s)"""
    if _absent(value): return None
    value = _to_date(value)
    if value is _Invalid: return _invalid_date(field, message_key)
    if value < _age_window(years, days):
        return _v(field, "not_older_than", "Must be no older than {limit}",
                  message_key, limit=f"{years} year(s)" if years is not None else f"{days} day(s)")
    return None


def not_beyond(value, *, years=None, days=None, field="", message_key=None):
    """{field} must be at most {years} year(s) / {days} day(s) ahead of today"""
    if _absent(value): return None
    value = _to_date(value)
    if value is _Invalid: return _invalid_date(field, message_key)
    if (years is None) == (days is None):
        raise TypeError("pass exactly one of years= or days=")
    limit = (_years_ahead(_today(), years) if years is not None
             else _today() + timedelta(days=days))
    if value > limit:
        return _v(field, "not_beyond", "Must be at most {limit} ahead of today",
                  message_key, limit=f"{years} year(s)" if years is not None else f"{days} day(s)")
    return None


def age_at_least(value, *, years=18, field="", message_key=None):
    """{field} must imply an age of at least {years} year(s)"""
    if _absent(value): return None
    value = _to_date(value)
    if value is _Invalid: return _invalid_date(field, message_key)
    if value > _years_ago(_today(), years):
        return _v(field, "age_at_least", "Must imply an age of at least {years}",
                  message_key, years=years)
    return None


def age_at_most(value, *, years, field="", message_key=None):
    """{field} must imply an age of at most {years} year(s)"""
    if _absent(value): return None
    value = _to_date(value)
    if value is _Invalid: return _invalid_date(field, message_key)
    if value < _years_ago(_today(), years + 1) + timedelta(days=1):
        return _v(field, "age_at_most", "Must imply an age of at most {years}",
                  message_key, years=years)
    return None


def on_weekday(value, *, allowed=(0, 1, 2, 3, 4), field="", message_key=None):
    """{field} must fall on an allowed weekday"""
    if _absent(value): return None
    value = _to_date(value)
    if value is _Invalid: return _invalid_date(field, message_key)
    if value.weekday() not in allowed:
        return _v(field, "on_weekday", "Must fall on an allowed weekday",
                  message_key, allowed=list(allowed))
    return None


def within_period(start, end, period_start, period_end, *, field="", message_key=None):
    """{field} range must lie inside the parent range {period_start}..{period_end}"""
    if any(_absent(x) for x in (start, end, period_start, period_end)): return None
    start, end = _to_date(start), _to_date(end)
    period_start, period_end = _to_date(period_start), _to_date(period_end)
    if _Invalid in (start, end, period_start, period_end):
        return _invalid_date(field, message_key)
    if not (period_start <= start and end <= period_end):
        return _v(field, "within_period", "Must lie inside {period_start} to {period_end}",
                  message_key, period_start=str(period_start), period_end=str(period_end))
    return None


def no_overlap(start, end, other_start, other_end, *, field="", message_key=None):
    """{field} range must not overlap {other_start}..{other_end}"""
    if any(_absent(x) for x in (start, end, other_start, other_end)): return None
    start, end = _to_date(start), _to_date(end)
    other_start, other_end = _to_date(other_start), _to_date(other_end)
    if _Invalid in (start, end, other_start, other_end):
        return _invalid_date(field, message_key)
    if start <= other_end and other_start <= end:
        return _v(field, "no_overlap", "Must not overlap {other_start} to {other_end}",
                  message_key, other_start=str(other_start), other_end=str(other_end))
    return None


# ── numbers ───────────────────────────────────────────────────────────────────

def at_least(value, minimum, *, field="", message_key=None):
    """{field} must be at least {minimum}"""
    if _absent(value) or _absent(minimum): return None
    value, minimum = _to_num(value), _to_num(minimum)
    if _Invalid in (value, minimum): return _invalid_number(field, message_key)
    if value < minimum:
        return _v(field, "at_least", "Must be at least {minimum}", message_key, minimum=minimum)
    return None


def at_most(value, maximum, *, field="", message_key=None):
    """{field} must be at most {maximum}"""
    if _absent(value) or _absent(maximum): return None
    value, maximum = _to_num(value), _to_num(maximum)
    if _Invalid in (value, maximum): return _invalid_number(field, message_key)
    if value > maximum:
        return _v(field, "at_most", "Must be at most {maximum}", message_key, maximum=maximum)
    return None


def positive(value, *, field="", message_key=None):
    """{field} must be greater than zero"""
    if _absent(value): return None
    value = _to_num(value)
    if value is _Invalid: return _invalid_number(field, message_key)
    if not value > 0:
        return _v(field, "positive", "Must be greater than zero", message_key)
    return None


def non_negative(value, *, field="", message_key=None):
    """{field} must be zero or greater"""
    if _absent(value): return None
    value = _to_num(value)
    if value is _Invalid: return _invalid_number(field, message_key)
    if value < 0:
        return _v(field, "non_negative", "Must be zero or greater", message_key)
    return None


def multiple_of(value, step, *, field="", message_key=None):
    """{field} must be a multiple of {step}"""
    if _absent(value) or _absent(step): return None
    value, step = _to_num(value), _to_num(step)
    if _Invalid in (value, step): return _invalid_number(field, message_key)
    try:
        ok = Decimal(str(value)) % Decimal(str(step)) == 0
    except InvalidOperation:
        ok = False
    if not ok:
        return _v(field, "multiple_of", "Must be a multiple of {step}", message_key, step=step)
    return None


def max_decimals(value, *, places=2, field="", message_key=None):
    """{field} must have at most {places} decimal place(s)"""
    if _absent(value): return None
    value = _to_num(value)
    if value is _Invalid: return _invalid_number(field, message_key)
    # Numeric, not textual: str(0.1 + 0.2) is '0.30000000000000004', which is
    # 0.3 for every practical purpose — compare against the rounded value with
    # a float-noise tolerance instead of counting string digits.
    if abs(value - round(value, places)) > 1e-9:
        return _v(field, "max_decimals", "Must have at most {places} decimal place(s)",
                  message_key, places=places)
    return None


def percent(value, *, field="", message_key=None):
    """{field} must be between 0 and 100"""
    if _absent(value): return None
    value = _to_num(value)
    if value is _Invalid: return _invalid_number(field, message_key)
    if not (0 <= value <= 100):
        return _v(field, "percent", "Must be between 0 and 100", message_key)
    return None


def luhn(value, *, field="", message_key=None):
    """{field} must carry a valid checksum"""
    if _absent(value): return None
    digits = re.sub(r"[ \-]", "", str(value))
    if not digits.isdigit():
        return _v(field, "luhn", "Must contain digits only", message_key)
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9: d -= 9
        total += d
    if total % 10 != 0:
        return _v(field, "luhn", "Must carry a valid checksum", message_key)
    return None


# ── comparing fields ──────────────────────────────────────────────────────────

def greater_than(value, reference, *, field="", message_key=None):
    """{field} must be greater than the reference value"""
    if _absent(value) or _absent(reference): return None
    try:
        failed = not value > reference
    except TypeError:
        return _v(field, "invalid_value", "Values are not comparable", message_key)
    if failed:
        return _v(field, "greater_than", "Must be greater than {reference}",
                  message_key, reference=reference)
    return None


def less_than(value, reference, *, field="", message_key=None):
    """{field} must be less than the reference value"""
    if _absent(value) or _absent(reference): return None
    try:
        failed = not value < reference
    except TypeError:
        return _v(field, "invalid_value", "Values are not comparable", message_key)
    if failed:
        return _v(field, "less_than", "Must be less than {reference}",
                  message_key, reference=reference)
    return None


def equal_to(value, reference, *, field="", message_key=None):
    """{field} must match the reference value"""
    if _absent(value) or _absent(reference): return None
    if value != reference:
        return _v(field, "equal_to", "Must match", message_key)
    return None


def different_from(value, reference, *, field="", message_key=None):
    """{field} must differ from the reference value"""
    if _absent(value) or _absent(reference): return None
    if value == reference:
        return _v(field, "different_from", "Must be different", message_key)
    return None


def sums_to(*parts, total, field="", message_key=None):
    """{field} parts must add up to {total}"""
    if any(_absent(p) for p in parts) or total is None or not parts: return None
    nums = [_to_num(p) for p in parts]
    total_n = _to_num(total)
    if _Invalid in nums or total_n is _Invalid:
        return _invalid_number(field, message_key)
    # rel_tol matters: ordinary float representation error on large monetary
    # sums already exceeds a bare 1e-9 absolute tolerance.
    if not math.isclose(sum(nums), total_n, rel_tol=1e-9, abs_tol=1e-9):
        return _v(field, "sums_to", "Must add up to {total}", message_key, total=total_n)
    return None


def ratio_at_least(value, reference, *, ratio=1.0, field="", message_key=None):
    """{field} must be at least the reference value times {ratio}"""
    if _absent(value) or _absent(reference): return None
    value, reference = _to_num(value), _to_num(reference)
    if _Invalid in (value, reference): return _invalid_number(field, message_key)
    if value < reference * ratio:
        return _v(field, "ratio_at_least", "Must be at least {ratio} times {reference}",
                  message_key, ratio=ratio, reference=reference)
    return None


# ── present or absent (the deliberate exception: absence is the subject) ─────

def required_when(value, when, *, field="", message_key=None):
    """{field} must be present when the condition holds"""
    if when and not _present(value):
        return _v(field, "required_when", "Required", message_key)
    return None


def forbidden_when(value, when, *, field="", message_key=None):
    """{field} must be absent when the condition holds"""
    if when and _present(value):
        return _v(field, "forbidden_when", "Must be left empty", message_key)
    return None


def required_together(*values, field="", message_key=None):
    """{field} fields must be present together or absent together"""
    present = [_present(v) for v in values]
    if any(present) and not all(present):
        return _v(field, "required_together", "These fields go together: fill all or none",
                  message_key)
    return None


def at_least_one(*values, field="", message_key=None):
    """{field} at least one of the fields must be present"""
    if not any(_present(v) for v in values):
        return _v(field, "at_least_one", "At least one of these is required", message_key)
    return None


def exactly_one(*values, field="", message_key=None):
    """{field} exactly one of the fields must be present"""
    if sum(1 for v in values if _present(v)) != 1:
        return _v(field, "exactly_one", "Provide exactly one of these", message_key)
    return None


# ── formats ───────────────────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def email(value, *, domains=None, field="", message_key=None):
    """{field} must be a valid email address"""
    if _absent(value): return None
    if not _EMAIL_RE.match(str(value)):
        return _v(field, "email", "Must be a valid email address", message_key)
    if domains and str(value).rsplit("@", 1)[1].lower() not in {d.lower() for d in domains}:
        return _v(field, "email", "Email must be in an allowed domain",
                  message_key, domains=list(domains))
    return None


def url(value, *, schemes=("https",), field="", message_key=None):
    """{field} must be a valid web address"""
    if _absent(value): return None
    parsed = urlparse(str(value))
    if parsed.scheme not in schemes or not parsed.netloc:
        return _v(field, "url", "Must be a valid web address ({schemes})",
                  message_key, schemes=list(schemes))
    return None


def phone(value, *, region=None, field="", message_key=None):
    """{field} must be a valid phone number"""
    # Lenient E.164 shape; a host needing carrier-grade parsing wraps its own
    # check around a dedicated library in its checks file.
    if _absent(value): return None
    digits = re.sub(r"[ \-().]", "", str(value))
    if not re.fullmatch(r"\+?\d{7,15}", digits):
        return _v(field, "phone", "Must be a valid phone number", message_key)
    return None


def iban(value, *, field="", message_key=None):
    """{field} must be a valid IBAN"""
    if _absent(value): return None
    compact = re.sub(r"\s", "", str(value)).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{11,30}", compact):
        return _v(field, "iban", "Must be a valid IBAN", message_key)
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(c, 36)) for c in rearranged)
    if int(numeric) % 97 != 1:
        return _v(field, "iban", "Must be a valid IBAN", message_key)
    return None


def uuid(value, *, field="", message_key=None):
    """{field} must be a well-formed UUID"""
    if _absent(value): return None
    try:
        _uuid_mod.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return _v(field, "uuid", "Must be a well-formed UUID", message_key)
    return None


def slug(value, *, field="", message_key=None):
    """{field} must contain only letters, digits, and dashes"""
    if _absent(value): return None
    if not re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", str(value)):
        return _v(field, "slug", "Must contain only letters, digits, and dashes", message_key)
    return None


def matches(value, pattern, *, field="", message_key=None):
    """{field} must match the required pattern"""
    if _absent(value) or pattern is None: return None
    if not re.fullmatch(pattern, str(value)):
        return _v(field, "matches", "Must match the required format", message_key)
    return None


def no_html(value, *, field="", message_key=None):
    """{field} must contain no HTML"""
    if _absent(value): return None
    if re.search(r"</?[A-Za-z][^>]*>", str(value)):
        return _v(field, "no_html", "Must contain no HTML", message_key)
    return None


def one_of(value, allowed, *, field="", message_key=None):
    """{field} must be one of the allowed values"""
    if _absent(value) or allowed is None: return None
    if value not in allowed:
        return _v(field, "one_of", "Must be one of the allowed values", message_key)
    return None


def not_in(value, forbidden, *, field="", message_key=None):
    """{field} must not be one of the forbidden values"""
    if _absent(value) or forbidden is None: return None
    if value in forbidden:
        return _v(field, "not_in", "This value is not allowed", message_key)
    return None


# ── lists ─────────────────────────────────────────────────────────────────────

def unique_items(values, *, field="", message_key=None):
    """{field} items must contain no duplicates"""
    if _absent(values): return None
    if isinstance(values, str):
        return None  # a string is a value, not a collection of items
    seen: list = []
    seen_hashable: set = set()
    for item in values:
        try:
            dup = item in seen_hashable
        except TypeError:  # unhashable (dicts, lists)
            dup = False
        # cross-check the other structure too: a set and an equal frozenset
        # must count as duplicates even though they land in different pools
        dup = dup or item in seen
        if dup:
            return _v(field, "unique_items", "Must contain no duplicates", message_key)
        try:
            seen_hashable.add(item)
        except TypeError:
            pass
        seen.append(item)
    return None


def subset_of(values, allowed, *, field="", message_key=None):
    """{field} every item must be in the allowed set"""
    if _absent(values) or allowed is None: return None
    stray = [item for item in values if item not in allowed]
    if stray:
        return _v(field, "subset_of", "Contains values that are not allowed",
                  message_key, stray=stray)
    return None


def contains_none(values, terms, *, field="", message_key=None):
    """{field} must contain none of the listed terms"""
    if _absent(values) or terms is None: return None
    items = [values.lower()] if isinstance(values, str) else [str(v).lower() for v in values]
    terms_l = [t.lower() for t in terms if t]  # an empty term matches nothing, not everything
    found = sorted({t for t in terms_l if any(t in item for item in items)})
    if found:
        return _v(field, "contains_none", "Contains terms that are not allowed",
                  message_key, found=found)
    return None
