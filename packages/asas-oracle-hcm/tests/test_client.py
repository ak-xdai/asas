"""The HTTP layer against a recording ``MockTransport``."""

import asyncio
import base64

import httpx
import pytest

from asas_oracle_hcm import (
    reference_ttls,
    CachePolicy,
    NullCache,
    OracleAlreadyExistsError,
    OracleFusionClient,
    OracleNotConfiguredError,
    OracleNotFoundError,
    OracleSettings,
    OracleUpstreamError,
)

from conftest import BASE, collection


def run(coro):
    return asyncio.run(coro)


def test_get_sends_basic_auth_json_accept_and_only_data(client, oracle):
    oracle.route("/grades", collection({"GradeId": 1}))
    body = run(client.get("/grades", {"q": "GradeId=1", "limit": 1, "skip": None}))
    assert body["items"] == [{"GradeId": 1}]
    call = oracle.calls[0]
    assert call.method == "GET"
    assert call.params == {"onlyData": "true", "q": "GradeId=1", "limit": "1"}
    assert call.headers["accept"] == "application/json"
    expected = base64.b64encode(b"integration:s3cret").decode()
    assert call.headers["authorization"] == f"Basic {expected}"


def test_a_caller_can_ask_for_the_links(client, oracle):
    run(client.get("/workers/1/child/attachments", {"onlyData": "false"}))
    assert oracle.calls[0].params["onlyData"] == "false"


def test_gateway_key_is_sent_only_when_set(make_client, oracle):
    run(make_client().get("/workers"))
    assert "x-api-key" not in oracle.calls[-1].headers

    keyed = OracleSettings(
        base_url=BASE, username="u", password="p",
        gateway_api_key="gw-123", gateway_api_key_header="X-Gateway-Key",
    )
    run(make_client(keyed).get("/workers"))
    last = oracle.calls[-1]
    assert last.headers["x-gateway-key"] == "gw-123"
    assert last.headers["authorization"].startswith("Basic ")  # an addition, not a replacement


def test_unconfigured_client_reports_off_and_refuses_calls():
    client = OracleFusionClient(OracleSettings())
    assert client.configured is False
    with pytest.raises(OracleNotConfiguredError):
        run(client.get("/grades"))


def test_404_is_not_found_and_other_errors_are_upstream(client, oracle):
    oracle.route("/workers/9", (404, {"title": "nope"}))
    with pytest.raises(OracleNotFoundError) as missing:
        run(client.get("/workers/9"))
    assert missing.value.status == 404 and missing.value.method == "GET"
    assert not missing.value.is_transient

    oracle.route("/grades", (503, {"detail": "tenant detail that must not leak"}))
    with pytest.raises(OracleUpstreamError) as down:
        run(client.get("/grades"))
    assert type(down.value) is OracleUpstreamError
    assert down.value.status == 503 and down.value.is_transient
    assert "tenant detail" not in str(down.value)


def test_transport_failure_and_malformed_bodies_are_upstream_errors(make_client, oracle):
    def boom(_):
        raise httpx.ConnectError("refused")

    oracle.route("/grades", boom)
    with pytest.raises(OracleUpstreamError) as unreachable:
        run(make_client().get("/grades"))
    assert unreachable.value.status is None and unreachable.value.is_transient

    oracle.route("/jobs", (200, "<html>not json</html>"))
    with pytest.raises(OracleUpstreamError):
        run(make_client().get("/jobs"))

    oracle.route("/positions", (200, [1, 2, 3]))
    with pytest.raises(OracleUpstreamError):
        run(make_client().get("/positions"))


def test_collection_reads_total_and_treats_minus_one_as_unknown(client, oracle):
    oracle.route("/absences", collection({"a": 1}, has_more=True, total=8612))
    page = run(client.get_collection("/absences", {"totalResults": "true"}))
    assert page.items == [{"a": 1}] and page.has_more is True and page.total == 8612
    items, has_more, total = page  # unpacks like the tuple it replaces
    assert total == 8612

    oracle.route("/workers", collection({"c": 1}, total=-1))
    assert run(client.get_collection("/workers")).total is None
    oracle.route("/locations", collection())
    assert run(client.get_collection("/locations")).total is None


def test_iter_collection_pages_until_has_more_is_false(client, oracle):
    def pages(rec):
        offset = int(rec.params["offset"])
        rows = [{"n": offset + i} for i in range(2)] if offset < 4 else [{"n": 4}]
        return httpx.Response(200, json={"items": rows, "hasMore": offset < 4})

    oracle.route("/departments", pages)

    async def walk():
        return [r["n"] async for r in client.iter_collection("/departments", page_size=2)]

    assert run(walk()) == [0, 1, 2, 3, 4]
    assert [c.params["offset"] for c in oracle.calls] == ["0", "2", "4"]
    assert all(c.params["limit"] == "2" for c in oracle.calls)


def test_iter_collection_stops_on_an_empty_page_and_at_max_pages(client, oracle):
    oracle.route("/jobs", collection(has_more=True))

    async def walk(**kw):
        return [r async for r in client.iter_collection("/jobs", **kw)]

    assert run(walk()) == []
    oracle.route("/jobs", collection({"x": 1}, has_more=True))
    oracle.calls.clear()
    assert len(run(walk(page_size=1, max_pages=3))) == 3
    assert len(oracle.calls) == 3


def test_get_bytes_asks_for_any_type_and_returns_the_content_type(client, oracle):
    oracle.route(
        "/workers/1/child/attachments/abc/enclosure/FileContents",
        lambda rec: httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf; x=1"}),
    )
    content, kind = run(client.get_bytes("/workers/1/child/attachments/abc/enclosure/FileContents"))
    assert content == b"%PDF" and kind == "application/pdf"
    call = oracle.calls[0]
    assert call.headers["accept"] == "*/*" and "onlyData" not in call.params


def test_post_uses_plain_json_and_patch_uses_the_adf_type(client, oracle):
    oracle.route("/absences", (201, {"AbsenceNumber": "A-1"}))
    oracle.route("/absences/A-1", (200, {"Duration": 2}))
    assert run(client.post("/absences", {"Title": "x"})) == {"AbsenceNumber": "A-1"}
    assert run(client.patch("/absences/A-1", {"Duration": 2})) == {"Duration": 2}
    post, patch = oracle.calls
    assert post.headers["content-type"] == "application/json" and post.json == {"Title": "x"}
    assert patch.method == "PATCH"
    assert patch.headers["content-type"] == "application/vnd.oracle.adf.resourceitem+json"


def test_a_duplicate_create_is_already_exists_and_a_plain_400_is_not(client, oracle):
    oracle.route("/absences", (400, {"detail": "Absence number already exists."}))
    with pytest.raises(OracleAlreadyExistsError) as dup:
        run(client.post("/absences", {}))
    assert dup.value.status == 400

    oracle.route("/absences", (400, {"detail": "Title is required."}))
    with pytest.raises(OracleUpstreamError) as bad:
        run(client.post("/absences", {}))
    assert type(bad.value) is OracleUpstreamError


def test_lookup_reads_are_cached_and_live_reads_are_not(client, oracle):
    oracle.route("/grades", collection({"GradeId": 1}))
    run(client.get("/grades", {"q": "GradeId=1"}))
    run(client.get("/grades", {"q": "GradeId=1"}))
    assert len(oracle.calls_to("/grades")) == 1
    run(client.get("/grades", {"q": "GradeId=2"}))  # a different query is a different key
    assert len(oracle.calls_to("/grades")) == 2

    run(client.get("/workers"))
    run(client.get("/workers"))
    assert len(oracle.calls_to("/workers")) == 2


def test_a_write_makes_the_reads_its_policy_names_stale(make_client, oracle):
    policy = CachePolicy(
        ttls={**reference_ttls(), "absences": 120, "absencesLOV": 120},
        stale_on_write={"absences": ("absences", "absencesLOV")},
    )
    client = make_client(cache_policy=policy)
    oracle.route("/absences", collection({"AbsenceNumber": "1"}))
    oracle.route("/absencesLOV", collection({"AbsenceNumber": "1"}))
    oracle.route("/absences/1", (200, {}))
    for _ in range(2):
        run(client.get("/absences"))
        run(client.get("/absencesLOV"))
    run(client.patch("/absences/1", {"Duration": 3}))
    run(client.get("/absences"))
    run(client.get("/absencesLOV"))
    assert len(oracle.calls_to("/absences")) == 2
    assert len(oracle.calls_to("/absencesLOV")) == 2


def test_the_default_policy_caches_reference_data_and_nothing_else(client, oracle):
    run(client.get("/grades"))
    run(client.get("/grades"))
    run(client.get("/recruitingJobRequisitions"))
    run(client.get("/recruitingJobRequisitions"))
    assert len(oracle.calls_to("/grades")) == 1
    assert len(oracle.calls_to("/recruitingJobRequisitions")) == 2, "no product resource is cached by default"


def test_null_cache_and_an_empty_policy_cache_nothing(make_client, oracle):
    oracle.route("/grades", collection({"GradeId": 1}))
    for kwargs in ({"cache": NullCache()}, {"cache_policy": CachePolicy(ttls={})}):
        oracle.calls.clear()
        c = make_client(**kwargs)
        run(c.get("/grades"))
        run(c.get("/grades"))
        assert len(oracle.calls) == 2


def test_an_empty_answer_is_kept_briefly_not_for_the_resource_ttl(make_client, oracle):
    class Spy(NullCache):
        def __init__(self):
            self.ttls = []

        async def set(self, key, value, ttl_seconds):
            self.ttls.append(ttl_seconds)

    spy = Spy()
    c = make_client(cache=spy)
    oracle.route("/publicWorkers", collection())
    run(c.get("/publicWorkers", {"q": "PersonId=1"}))
    oracle.route("/grades", collection({"GradeId": 1}))
    run(c.get("/grades", {"q": "GradeId=1"}))
    assert spy.ttls == [600, 21_600]


def test_the_client_closes_only_what_it_owns():
    borrowed = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    async def scenario():
        async with OracleFusionClient(
            OracleSettings(base_url=BASE, username="u", password="p"), http=borrowed
        ) as c:
            await c.get("/x")
        return borrowed.is_closed

    assert run(scenario()) is False
