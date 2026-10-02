"""Lookups backed by a LookupStore: answers shared across processes, stale
ones served and refreshed behind the caller, negative ones kept, failures
never stored, and refreshes that skip the client's read cache."""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from asas_oracle_hcm import (
    DIRECTORY_KIND,
    POSITION_BUDGET_KIND,
    MemoryLookupStore,
    OracleLookups,
)

from conftest import collection


def run(coro):
    return asyncio.run(coro)


def _grades(oracle, names):
    def answer(rec):
        gid = rec.params["q"].split("=", 1)[1]
        name = names.get(gid)
        return httpx.Response(200, json={"items": [{"GradeId": gid, "GradeName": name}] if name else [], "hasMore": False})

    oracle.route("/grades", answer)


def _old():
    return datetime.now(timezone.utc) - timedelta(days=2)


def test_a_new_process_reads_the_store_not_oracle(client, oracle):
    _grades(oracle, {"11": "Grade A"})
    store = MemoryLookupStore()
    assert run(OracleLookups(client, store=store).names("grade", ["11"])) == {"11": "Grade A"}
    fresh = OracleLookups(client, store=store)  # another replica, or a restart
    assert run(fresh.names("grade", ["11"])) == {"11": "Grade A"}
    assert len(oracle.calls_to("/grades")) == 1


def test_a_stale_answer_is_served_at_once_and_refreshed_behind(client, oracle):
    names = {"11": "Grade A"}
    _grades(oracle, names)
    store = MemoryLookupStore()
    run(OracleLookups(client, store=store).names("grade", ["11"]))
    store.age("grade", "11", _old())
    names["11"] = "Grade A (renamed)"

    lookups = OracleLookups(client, store=store)

    async def go():
        served = await lookups.names("grade", ["11"])
        assert served == {"11": "Grade A"}, "the stale answer, at once"
        await lookups.drain()

    run(go())
    held = run(store.read("grade", ["11"]))["11"]
    assert held.name == "Grade A (renamed)", "refreshed from ORACLE"
    assert len(oracle.calls_to("/grades")) == 2


def test_the_refresh_skips_the_clients_read_cache(client, oracle):
    """The D335 bug: with the client's cache under the store, a refresh was
    answered from the cached copy and re-saved the old name as fresh."""
    names = {"11": "Grade A"}
    _grades(oracle, names)
    # Warm the CLIENT cache with the old answer, the way a store-less reader would.
    run(client.get("/grades", {"q": "GradeId=11", "limit": 1, "fields": "GradeId,GradeName"}))
    store = MemoryLookupStore()
    run(store.write("grade", {"11": ("Grade A", None)}))
    store.age("grade", "11", _old())
    names["11"] = "Grade A (renamed)"
    lookups = OracleLookups(client, store=store)

    async def go():
        await lookups.names("grade", ["11"])
        await lookups.drain()

    run(go())
    assert run(store.read("grade", ["11"]))["11"].name == "Grade A (renamed)"


def test_an_unknown_id_is_kept_and_not_asked_again(client, oracle):
    _grades(oracle, {})
    store = MemoryLookupStore()
    assert run(OracleLookups(client, store=store).names("grade", ["99"])) == {}
    assert run(store.read("grade", ["99"]))["99"].name == "", "kept as a negative answer"
    assert run(OracleLookups(client, store=store).names("grade", ["99"])) == {}
    assert len(oracle.calls_to("/grades")) == 1


def test_a_failed_lookup_is_never_stored(client, oracle):
    oracle.route("/grades", (502, {}))
    store = MemoryLookupStore()
    assert run(OracleLookups(client, store=store).names("grade", ["11"])) == {}
    assert run(store.read("grade", ["11"])) == {}, "a failure is not an answer"


def test_a_failing_store_is_an_empty_one(client, oracle):
    _grades(oracle, {"11": "Grade A"})

    class Broken:
        async def read(self, kind, ids):
            raise RuntimeError("database down")

        async def write(self, kind, answers):
            raise RuntimeError("database down")

    assert run(OracleLookups(client, store=Broken()).names("grade", ["11"])) == {"11": "Grade A"}


def test_people_are_kept_with_their_address(client, oracle):
    oracle.route(
        "/publicWorkers",
        collection({"PersonId": "7", "DisplayName": "Mona", "WorkEmail": "mona@example.ae"}),
    )
    store = MemoryLookupStore()
    run(OracleLookups(client, store=store).people(["7"]))
    held = run(store.read(DIRECTORY_KIND, ["7"]))["7"]
    assert (held.name, held.extra) == ("Mona", {"address": "mona@example.ae"})
    person = run(OracleLookups(client, store=store).people(["7"]))["7"]
    assert person.address == "mona@example.ae"
    assert len(oracle.calls_to("/publicWorkers")) == 1


def test_a_position_is_read_once_for_its_name_and_its_budget_flag(client, oracle):
    oracle.route(
        "/positions",
        collection({"PositionId": "5", "Name": "Analyst", "BudgetedPositionFlag": "Y"}),
    )
    store = MemoryLookupStore()
    lookups = OracleLookups(client, store=store)
    position = run(lookups.positions(["5"]))["5"]
    assert (position.name, position.budgeted) == ("Analyst", True)
    assert run(lookups.names("position", ["5"])) == {"5": "Analyst"}, "names() reuses it"
    assert run(store.read(POSITION_BUDGET_KIND, ["5"]))["5"].name == "Y"
    assert run(OracleLookups(client, store=store).positions(["5"]))["5"].budgeted is True
    assert len(oracle.calls_to("/positions")) == 1
    call = oracle.calls_to("/positions")[0]
    assert call.params["fields"] == "PositionId,Name,BudgetedPositionFlag"


def test_an_unset_budget_flag_is_none_not_false(client, oracle):
    oracle.route("/positions", collection({"PositionId": "6", "Name": "Clerk"}))
    assert run(OracleLookups(client).positions(["6"]))["6"].budgeted is None
