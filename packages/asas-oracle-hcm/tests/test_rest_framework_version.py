"""Per-read headers, and the grammar that is NOT the same on every resource.

Both come from running this package's shape against a live Fusion pod on
13 October 2026, and both share one nasty property: **the instance answers
200 with an empty collection rather than an error**. There is nothing to
catch in a log and nothing to retry; a reader simply sees "no rows".

  * ``/positions`` refuses ``;`` as AND with a 400 and wants the word ``AND``.
    The same two-clause filter answers 33,391 rows one way and 400 the other.
  * under the DEFAULT framework version, a multi-clause ``AND`` on that
    resource answers 200 and ZERO rows; with ``REST-Framework-Version: 2`` it
    answers the 33,391. One clause works either way, which is why the
    package's own single-id position lookups never met it.
"""

import asyncio

import pytest

from asas_oracle_hcm import (
    NullCache,
    OracleFusionClient,
    OracleSettings,
    REST_FRAMEWORK_V2,
    and_,
    and_words,
)

from conftest import BASE, collection


def run(coro):
    return asyncio.run(coro)


# ---- the grammar ------------------------------------------------------------


def test_the_two_joins_are_different_and_both_are_real():
    clauses = ("ActiveStatus = 'A'", "HiringStatus = 'APPROVED'")
    # recruitingJobRequisitions and friends
    assert and_(*clauses) == "ActiveStatus = 'A';HiringStatus = 'APPROVED'"
    # /positions, which 400s on the line above
    assert and_words(*clauses) == "ActiveStatus = 'A' AND HiringStatus = 'APPROVED'"


def test_a_single_clause_is_identical_under_both_which_is_why_it_hid():
    assert and_("PositionId=1") == and_words("PositionId=1") == "PositionId=1"


def test_nothing_to_filter_is_None_under_both_so_it_can_be_passed_as_q():
    assert and_words() is None
    assert and_words(None, "") is None
    assert and_() is None


# ---- the header -------------------------------------------------------------


def test_the_constant_is_the_header_the_instance_wants():
    assert REST_FRAMEWORK_V2 == {"REST-Framework-Version": "2"}


def test_a_read_sends_the_header_it_was_given(client, oracle):
    oracle.route("/positions", collection({"PositionId": 1}))
    run(client.get("/positions", {"q": "PositionId=1"}, headers=REST_FRAMEWORK_V2))
    assert oracle.calls[0].headers["rest-framework-version"] == "2"


def test_a_read_without_one_sends_no_such_header(client, oracle):
    oracle.route("/positions", collection({"PositionId": 1}))
    run(client.get("/positions", {"q": "PositionId=1"}))
    assert "rest-framework-version" not in oracle.calls[0].headers


def test_get_collection_passes_the_header_through(client, oracle):
    oracle.route("/positions", collection({"PositionId": 1}, total=1))
    page = run(
        client.get_collection("/positions", {"limit": 1}, headers=REST_FRAMEWORK_V2)
    )
    assert page.total == 1
    assert oracle.calls[0].headers["rest-framework-version"] == "2"


def test_the_header_is_part_of_the_cache_key(make_client, oracle):
    """THE WHOLE REASON THIS IS NOT COSMETIC. The two versions answer
    DIFFERENTLY for one query, so a body fetched under one must never be
    served to a reader asking under the other: that would be a wrong answer
    with no error anywhere."""

    class Keys(NullCache):
        def __init__(self):
            self.seen = []

        async def set(self, key, value, ttl_seconds):
            self.seen.append(key)

    keys = Keys()
    c = make_client(cache=keys)
    oracle.route("/positions", collection({"PositionId": 1}))
    run(c.get("/positions", {"q": "PositionId=1"}))
    run(c.get("/positions", {"q": "PositionId=1"}, headers=REST_FRAMEWORK_V2))
    assert len(keys.seen) == 2
    assert keys.seen[0] != keys.seen[1], "the two versions shared a cache key"


def test_the_same_header_reuses_the_same_key(make_client, oracle):
    class Keys(NullCache):
        def __init__(self):
            self.seen = []

        async def set(self, key, value, ttl_seconds):
            self.seen.append(key)

    keys = Keys()
    c = make_client(cache=keys)
    oracle.route("/positions", collection({"PositionId": 1}))
    run(c.get("/positions", {"q": "PositionId=1"}, headers=REST_FRAMEWORK_V2))
    run(c.get("/positions", {"q": "PositionId=1"}, headers=dict(REST_FRAMEWORK_V2)))
    assert keys.seen[0] == keys.seen[1]
