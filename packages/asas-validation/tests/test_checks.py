"""The check library (DR 0004 phase 1): the 44 shipped checks, the two doors,
the derived catalog, enforce, the clock seam, and include_checks."""

import types
from datetime import date, datetime, timedelta

import pytest
from fastapi import HTTPException

from asas_validation import catalog, configure_clock, enforce, include_checks, validate

TODAY = date(2026, 9, 12)


@pytest.fixture(autouse=True)
def _frozen_clock():
    configure_clock(lambda: TODAY)
    yield
    configure_clock(None)


def bad(v):  # a violation, with the field attached where given
    assert v is not None
    return v


# ── dates and time ────────────────────────────────────────────────────────────

def test_temporal_checks():
    d = lambda s: date.fromisoformat(s)
    assert validate.not_in_future(d("2026-09-12")) is None
    assert bad(validate.not_in_future(d("2026-09-13"))).code == "not_in_future"
    assert validate.not_in_past(d("2026-09-12")) is None
    assert bad(validate.not_in_past(d("2026-09-11"))).code == "not_in_past"

    assert validate.after(d("2026-09-14"), d("2026-09-11"), days=3) is None
    v = bad(validate.after(d("2026-09-13"), d("2026-09-11"), days=3))
    assert v.code == "after" and v.params["days"] == 3
    assert validate.before(d("2026-09-08"), d("2026-09-11"), days=3) is None
    assert bad(validate.before(d("2026-09-09"), d("2026-09-11"), days=3)).code == "before"

    assert validate.between(d("2026-09-12"), d("2026-09-01"), d("2026-09-30")) is None
    assert bad(validate.between(d("2026-10-01"), d("2026-09-01"), d("2026-09-30"))).code == "between"

    assert validate.not_older_than(d("2023-09-13"), years=3) is None
    assert bad(validate.not_older_than(d("2023-09-11"), years=3)).code == "not_older_than"
    assert validate.not_beyond(d("2026-09-20"), days=30) is None
    assert bad(validate.not_beyond(d("2026-10-13"), days=30)).code == "not_beyond"
    with pytest.raises(TypeError, match="exactly one"):
        validate.not_older_than(d("2026-01-01"))

    assert validate.age_at_least(d("2008-09-12"), years=18) is None
    assert bad(validate.age_at_least(d("2008-09-13"), years=18)).code == "age_at_least"
    assert validate.age_at_most(d("1962-01-01"), years=65) is None
    assert bad(validate.age_at_most(d("1960-09-11"), years=65)).code == "age_at_most"

    assert validate.on_weekday(d("2026-09-11")) is None            # a Friday
    assert bad(validate.on_weekday(d("2026-09-13"))).code == "on_weekday"  # a Sunday
    assert validate.on_weekday(d("2026-09-13"), allowed=(6,)) is None

    assert validate.within_period(d("2026-09-05"), d("2026-09-10"),
                                  d("2026-09-01"), d("2026-09-30")) is None
    assert bad(validate.within_period(d("2026-08-30"), d("2026-09-10"),
                                      d("2026-09-01"), d("2026-09-30"))).code == "within_period"
    assert validate.no_overlap(d("2026-09-01"), d("2026-09-05"),
                               d("2026-09-06"), d("2026-09-09")) is None
    assert bad(validate.no_overlap(d("2026-09-01"), d("2026-09-07"),
                                   d("2026-09-06"), d("2026-09-09"))).code == "no_overlap"


def test_datetime_meets_date_coerces_instead_of_crashing():
    assert validate.after(datetime(2026, 9, 14, 10, 30), date(2026, 9, 11), days=3) is None
    assert validate.not_in_future(datetime(2026, 9, 12, 23, 59)) is None


def test_clock_is_configurable():
    configure_clock(lambda: date(2030, 1, 1))
    assert validate.not_in_past(date(2029, 12, 31)) is not None


# ── numbers ───────────────────────────────────────────────────────────────────

def test_numeric_checks():
    assert validate.at_least(3, 2) is None
    assert bad(validate.at_least(1, 2)).params["minimum"] == 2
    assert validate.at_most(2, 2) is None
    assert bad(validate.at_most(3, 2)).code == "at_most"
    assert validate.positive(0.1) is None
    assert bad(validate.positive(0)).code == "positive"
    assert validate.non_negative(0) is None
    assert bad(validate.non_negative(-1)).code == "non_negative"
    assert validate.multiple_of(0.15, 0.05) is None
    assert bad(validate.multiple_of(0.16, 0.05)).code == "multiple_of"
    assert validate.max_decimals(12.25) is None
    assert bad(validate.max_decimals(12.256)).code == "max_decimals"
    assert validate.max_decimals(10.0, places=0) is None   # trailing zeros are not decimals
    assert validate.percent(100) is None
    assert bad(validate.percent(101)).code == "percent"
    assert validate.luhn("4539 1488 0343 6467") is None
    assert bad(validate.luhn("4539148803436468")).code == "luhn"
    assert bad(validate.luhn("not-digits")).code == "luhn"


# ── comparing fields ──────────────────────────────────────────────────────────

def test_cross_field_checks():
    assert validate.greater_than(5, 4) is None
    assert bad(validate.greater_than(4, 4)).code == "greater_than"
    assert validate.less_than(3, 4) is None
    assert bad(validate.less_than(4, 4)).code == "less_than"
    assert validate.equal_to("x@y.ae", "x@y.ae") is None
    assert bad(validate.equal_to("a", "b")).code == "equal_to"
    assert validate.different_from("new", "old") is None
    assert bad(validate.different_from("same", "same")).code == "different_from"
    assert validate.sums_to(30, 30, 40, total=100) is None
    assert bad(validate.sums_to(30, 30, total=100)).code == "sums_to"
    assert validate.ratio_at_least(120, 100, ratio=1.2) is None
    assert bad(validate.ratio_at_least(119, 100, ratio=1.2)).code == "ratio_at_least"


# ── presence family: absence is the subject, so None must NOT skip ───────────

def test_presence_checks_fire_on_absent_values():
    assert bad(validate.required_when(None, True)).code == "required_when"
    assert bad(validate.required_when("", True)).code == "required_when"
    assert validate.required_when(None, False) is None
    assert validate.required_when("x", True) is None
    assert bad(validate.forbidden_when("x", True)).code == "forbidden_when"
    assert validate.forbidden_when(None, True) is None
    assert validate.required_together("a", "b") is None
    assert validate.required_together(None, None) is None
    assert bad(validate.required_together("a", None)).code == "required_together"
    assert validate.at_least_one(None, "x") is None
    assert bad(validate.at_least_one(None, "")).code == "at_least_one"
    assert validate.exactly_one(None, "x") is None
    assert bad(validate.exactly_one("a", "b")).code == "exactly_one"


def test_every_non_presence_check_skips_absent_values():
    assert validate.after(None, TODAY) is None
    assert validate.after(TODAY, None) is None
    assert validate.email(None) is None
    assert validate.at_least(None, 2) is None
    assert validate.unique_items(None) is None


# ── formats ───────────────────────────────────────────────────────────────────

def test_format_checks():
    assert validate.email("sara@xdigit.ai") is None
    assert bad(validate.email("nope")).code == "email"
    assert bad(validate.email("sara@other.ai", domains=["xdigit.ai"])).code == "email"
    assert validate.url("https://asas.example") is None
    assert bad(validate.url("ftp://asas.example")).code == "url"
    assert bad(validate.url("https://")).code == "url"
    assert validate.phone("+971 50 123 4567") is None
    assert bad(validate.phone("abc")).code == "phone"
    assert validate.iban("AE07 0331 2345 6789 0123 456") is None
    assert bad(validate.iban("AE07033123456789012345X")).code == "iban"
    assert bad(validate.iban("XX00")).code == "iban"
    assert validate.uuid("6fa459ea-ee8a-3ca4-894e-db77e160355e") is None
    assert bad(validate.uuid("not-a-uuid")).code == "uuid"
    assert validate.slug("asas-validation-2") is None
    assert bad(validate.slug("no spaces!")).code == "slug"
    assert validate.matches("AB-1234", r"[A-Z]{2}-\d{4}") is None
    assert bad(validate.matches("AB1234", r"[A-Z]{2}-\d{4}")).code == "matches"
    assert validate.no_html("plain text") is None
    assert bad(validate.no_html("<b>bold</b>")).code == "no_html"
    assert validate.one_of("open", ["open", "closed"]) is None
    assert bad(validate.one_of("gone", ["open", "closed"])).code == "one_of"
    assert validate.not_in("fine", ["banned"]) is None
    assert bad(validate.not_in("banned", ["banned"])).code == "not_in"


def test_collection_checks():
    assert validate.unique_items([1, 2, 3]) is None
    assert bad(validate.unique_items([1, 2, 2])).code == "unique_items"
    assert validate.subset_of(["a"], ["a", "b"]) is None
    assert bad(validate.subset_of(["a", "z"], ["a", "b"])).params["stray"] == ["z"]
    assert validate.contains_none("a clean sentence", ["banned"]) is None
    assert bad(validate.contains_none("with BANNED word", ["banned"])).code == "contains_none"


# ── the doors, the catalog, enforce, include_checks ──────────────────────────

def test_machine_door_equals_attribute_door():
    a = validate.after(date(2026, 9, 10), date(2026, 9, 11))
    b = validate("after", date(2026, 9, 10), date(2026, 9, 11))
    assert a.code == b.code == "after"


def test_unknown_check_fails_loud_and_names_the_catalog():
    with pytest.raises(LookupError, match="unknown check 'nope'"):
        validate("nope", 1)


def test_catalog_is_derived_from_the_functions():
    cat = {c["name"]: c for c in catalog()}
    assert len(cat) == 44
    after = cat["after"]
    assert after["takes"] == ["value", "reference"]
    assert after["settings"] == {"days": 0}
    assert "must be after" in after["sentence"]
    assert cat["sums_to"]["takes"] == ["parts...", "total"]
    assert all(c["sentence"] for c in cat.values())


def test_include_checks_adds_a_host_module_and_rejects_collisions():
    mod = types.ModuleType("host_checks")
    src = '''
def emirates_id(value, *, field="", message_key=None):
    """{field} must be a valid Emirates ID"""
    from asas_validation.engine import Violation
    if value is None: return None
    if not str(value).startswith("784"):
        return Violation(field, "emirates_id", "Must be a valid Emirates ID")
    return None
'''
    exec(compile(src, "host_checks", "exec"), mod.__dict__)
    mod.emirates_id.__module__ = "host_checks"
    include_checks(mod)
    try:
        assert validate.emirates_id("784-1234") is None
        assert validate.emirates_id("123").code == "emirates_id"
        assert any(c["name"] == "emirates_id" for c in catalog())
        clash = types.ModuleType("clash")
        exec(compile("def after(value, *, field=''):\n    return None", "clash", "exec"),
             clash.__dict__)
        clash.after.__module__ = "clash"
        with pytest.raises(ValueError, match="already exists"):
            include_checks(clash)
    finally:
        from asas_validation import library
        library._MODULES.pop(mod.__name__, None)
        delattr(validate, "emirates_id")


def test_enforce_collects_everything_and_raises_one_422():
    with pytest.raises(HTTPException) as exc:
        enforce([
            validate.email("nope", field="candidate_email"),
            validate.after(date(2026, 9, 9), date(2026, 9, 8), days=3,
                           field="scheduled_date", message_key="interview.min_notice"),
            validate.at_least(3, 2, field="panel_size"),  # passes → dropped
        ])
    detail = exc.value.detail
    assert exc.value.status_code == 422 and len(detail) == 2
    assert detail[0]["loc"] == ["body", "candidate_email"]
    assert detail[1]["message_key"] == "interview.min_notice"
    assert detail[1]["params"]["days"] == 3
    # nothing wrong → enforce is silent
    enforce([None, validate.at_least(3, 2)])


# ── review-fix regressions (PR #53) ───────────────────────────────────────────

def test_empty_string_counts_as_absent_for_non_presence_checks():
    """Forms and CSV rows encode a cleared field as "" — skip, never crash."""
    assert validate.after("", date(2026, 9, 1)) is None
    assert validate.not_in_future("") is None
    assert validate.at_least("", 2) is None
    assert validate.email("") is None
    assert validate.unique_items("") is None
    # the presence family still sees "" as absent and FIRES
    assert validate.required_when("", True) is not None


def test_after_and_before_are_strict_at_zero_days():
    d0 = date(2026, 9, 10)
    assert bad(validate.after(d0, d0)).code == "after"
    assert bad(validate.before(d0, d0)).code == "before"
    assert validate.after(d0 + timedelta(days=1), d0) is None
    # with a gap, "at least N days" stays inclusive at exactly N
    assert validate.after(d0 + timedelta(days=3), d0, days=3) is None


def test_unique_items_handles_unhashable_items():
    assert bad(validate.unique_items([{"day": "mon"}, {"day": "mon"}])).code == "unique_items"
    assert validate.unique_items([{"day": "mon"}, {"day": "tue"}]) is None


def test_not_beyond_survives_leap_day():
    configure_clock(lambda: date(2028, 2, 29))
    assert validate.not_beyond(date(2029, 2, 20), years=1) is None
    assert bad(validate.not_beyond(date(2029, 3, 1), years=1)).code == "not_beyond"


def test_contains_none_matches_within_items_only():
    assert validate.contains_none(["alpha", "beta"], ["a b"]) is None  # never across items
    assert validate.contains_none("clean text", [""]) is None          # empty term is inert
    assert bad(validate.contains_none(["has a b inside"], ["a b"])).code == "contains_none"


def test_catalog_survives_whitespace_docstrings():
    mod = types.ModuleType("blank_doc")
    exec(compile('def odd_check(value, *, field=""):\n    "\\n"\n    return None',
                 "blank_doc", "exec"), mod.__dict__)
    mod.odd_check.__module__ = "blank_doc"
    include_checks(mod)
    try:
        entry = next(c for c in catalog() if c["name"] == "odd_check")
        assert entry["sentence"] == ""
    finally:
        from asas_validation import library
        library._MODULES.pop(mod.__name__, None)
        delattr(validate, "odd_check")


def test_same_module_reinclude_is_idempotent_cross_module_still_fails():
    m1 = types.ModuleType("reload_mod")
    exec(compile('def my_check(value, *, field=""):\n    """x"""\n    return None',
                 "reload_mod", "exec"), m1.__dict__)
    m1.my_check.__module__ = "reload_mod"
    include_checks(m1)
    try:
        m2 = types.ModuleType("reload_mod")  # the same module, re-executed
        exec(compile('def my_check(value, *, field=""):\n    """x"""\n    return None',
                     "reload_mod", "exec"), m2.__dict__)
        m2.my_check.__module__ = "reload_mod"
        include_checks(m2)  # replaces silently — a reload, not a conflict
        assert validate.my_check(1) is None
    finally:
        from asas_validation import library
        library._MODULES.pop("reload_mod", None)
        delattr(validate, "my_check")


def test_declared_engine_coerces_datetime_columns():
    from asas_validation import Rule, declare_rules, evaluate
    declare_rules([Rule(entity="job", kind="not_future", fields=("created",),
                        message="no future", code="c1")])
    out = evaluate("job", None, {"created": datetime(2020, 1, 1, 10, 0)})
    assert out == []  # datetime under a date rule compares instead of raising


# ── second-review regressions (PR #53, round 2) ──────────────────────────────

def _mk_module(mod_name, *fn_names):
    import types as _t
    m = _t.ModuleType(mod_name)
    body = "\n".join(
        f"def {n}(value, *, field=''):\n    'sentence'\n    return None" for n in fn_names
    )
    exec(compile(body, mod_name, "exec"), m.__dict__)
    for n in fn_names:
        getattr(m, n).__module__ = mod_name
    return m


def test_violation_stays_hashable():
    """0.11 hosts dedupe violations in sets; params must not break that."""
    from asas_validation import Violation
    v = Violation("f", "c", "m", params={"days": 3})
    assert v in {v}


def test_string_transport_values_answer_422_not_500():
    """CSV/form transport: a provided-but-unparseable value is INVALID data
    (a violation on the field), never a TypeError that 500s the request; a
    parseable ISO string simply works."""
    assert validate.after("2026-02-01", date(2026, 1, 1)) is None
    assert validate.after("junk", date(2026, 1, 1)).code == "invalid_date"
    assert validate.not_in_future("   ") is None          # whitespace = absent
    assert validate.at_least("5", 2) is None
    assert validate.at_least("N/A", 2).code == "invalid_number"
    assert validate.at_least(5, "") is None               # absent reference skips
    assert validate.max_decimals("N/A").code == "invalid_number"
    assert validate.greater_than("b", 1).code == "invalid_value"


def test_money_sums_and_float_artifacts():
    assert validate.sums_to(3178142.72, 91244163.07, total=94422305.79) is None
    assert validate.max_decimals(0.1 + 0.2, places=2) is None
    assert validate.max_decimals(12.256, places=2) is not None


def test_no_html_allows_comparison_prose():
    assert validate.no_html("priced < 100 and qty > 5") is None
    assert validate.no_html("<b>x</b>").code == "no_html"


def test_unique_items_string_and_cross_structure():
    assert validate.unique_items("aa") is None            # a value, not a collection
    assert validate.unique_items([{1}, frozenset({1})]).code == "unique_items"


def test_include_checks_is_atomic_and_reload_safe():
    from asas_validation import library
    m = _mk_module("atomic_mod", "zeta", "after")   # 'after' collides with shipped
    with pytest.raises(ValueError):
        include_checks(m)
    assert not hasattr(validate, "zeta")            # nothing half-applied
    assert all(c["name"] != "zeta" for c in catalog())
    # reload: the same module name re-included replaces, never duplicates
    include_checks(_mk_module("reload2", "my_r"))
    include_checks(_mk_module("reload2", "my_r"))
    try:
        assert sum(1 for c in catalog() if c["name"] == "my_r") == 1
    finally:
        library._MODULES.pop("reload2", None)
        delattr(validate, "my_r")
