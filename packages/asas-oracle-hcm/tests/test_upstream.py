"""The breaker, the call statistics, the per-request counter, the pooled
transport and gateway-only auth, through the real client."""

import asyncio

import httpx
import pytest

from asas_oracle_hcm import (
    Breaker,
    OracleConfigError,
    OracleFusionClient,
    OracleNotFoundError,
    OracleSettings,
    OracleUnavailableError,
    OracleUpstreamError,
    count_calls,
)

from conftest import BASE, collection


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _breaker(failures: int = 3) -> tuple[Breaker, Clock]:
    clock = Clock()
    return Breaker(failures=failures, cooldown_seconds=30.0, clock=clock), clock


def test_consecutive_faults_open_the_breaker_and_reads_then_fail_fast(make_client, oracle):
    oracle.route("/grades", (503, {}))
    oracle.route("/grades/1", (503, {}))
    breaker, clock = _breaker()
    client = make_client(breaker=breaker)

    async def go():
        for _ in range(3):
            with pytest.raises(OracleUpstreamError) as exc:
                await client.get("/grades")
            assert not isinstance(exc.value, OracleUnavailableError)
        assert client.health.breaker.state == "open"
        with pytest.raises(OracleUnavailableError) as refused:
            await client.get("/grades")
        assert refused.value.retry_after_seconds > 0 and refused.value.is_transient
        assert len(oracle.calls) == 3, "the fourth read never left the process"

        # Writes are never refused: the caller's outbox owns their retry.
        with pytest.raises(OracleUpstreamError):
            await client.patch("/grades/1", {"Name": "x"})
        assert oracle.calls[-1].method == "PATCH"

        # After the cooldown one probe goes out; its fault reopens at once.
        clock.now += 31
        assert client.health.breaker.state == "half_open"
        with pytest.raises(OracleUpstreamError):
            await client.get("/grades")
        assert client.health.breaker.state == "open"

    run(go())
    snap = client.health.snapshot()
    assert snap["refused"] == 1 and snap["breaker"] == "open"


def test_a_successful_probe_closes_it_and_only_one_probe_goes_out(make_client, oracle):
    healthy = {"on": False}
    oracle.route("/grades", lambda rec: httpx.Response(200, json={"items": []}) if healthy["on"] else httpx.Response(502, json={}))
    breaker, clock = _breaker()
    client = make_client(breaker=breaker)

    async def go():
        for _ in range(3):
            with pytest.raises(OracleUpstreamError):
                await client.get("/grades")
        clock.now += 31
        assert breaker.allow() is True, "the probe"
        assert breaker.allow() is False, "and no second one while it is out"
        breaker.release_probe()
        healthy["on"] = True
        assert await client.get("/grades") == {"items": []}

    run(go())
    assert breaker.state == "closed" and breaker.consecutive == 0


def test_a_404_or_a_400_is_an_answer_not_a_fault(make_client, oracle):
    oracle.route("/recruitingJobRequisitions/missing", (404, {}))
    oracle.route("/grades", (400, {}))
    breaker, _ = _breaker()
    client = make_client(breaker=breaker)

    async def go():
        for _ in range(5):
            with pytest.raises(OracleNotFoundError):
                await client.get("/recruitingJobRequisitions/missing")
            with pytest.raises(OracleUpstreamError):
                await client.get("/grades")

    run(go())
    snap = client.health.snapshot()
    assert (snap["breaker"], snap["faults"], snap["calls"]) == ("closed", 0, 10)


def test_a_transport_failure_counts_and_the_breaker_can_be_turned_off(settings):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    client = OracleFusionClient(settings, http=http, breaker=Breaker(failures=0))

    async def go():
        for _ in range(6):
            with pytest.raises(OracleUpstreamError) as exc:
                await client.get("/grades")
            assert not isinstance(exc.value, OracleUnavailableError)

    run(go())
    snap = client.health.snapshot()
    assert snap["breaker"] == "disabled" and snap["faults"] == 6


def test_statistics_are_per_method_and_resource(client, oracle):
    oracle.route("/grades", collection({"GradeId": 1}))

    async def go():
        await client.get("/grades", {"q": "GradeId=1"})
        await client.get("/grades", {"q": "GradeId=2"})
        await client.get("/jobs")

    run(go())
    rows = {r["resource"]: r for r in client.health.snapshot()["resources"]}
    assert rows["grades"]["calls"] == 2 and rows["jobs"]["calls"] == 1


def test_the_request_counter_counts_calls_not_cache_hits_and_closes(client, oracle):
    oracle.route("/grades", collection({"GradeId": 1}))

    async def go():
        with count_calls() as counted:
            await client.get("/grades", {"q": "GradeId=1"})
            await client.get("/grades", {"q": "GradeId=1"})  # a cache hit
            await client.get("/jobs")
        assert counted.calls == 2
        await client.get("/departments")
        assert counted.calls == 2, "closed when the block ended"

    run(go())


def test_use_cache_false_goes_to_oracle_and_stores_nothing(client, oracle):
    oracle.route("/grades", collection({"GradeId": 1}))

    async def go():
        await client.get("/grades")
        await client.get("/grades", use_cache=False)
        await client.get("/grades")  # still the first answer's cache entry

    run(go())
    assert len(oracle.calls_to("/grades")) == 2


def test_gateway_key_alone_is_allowed_and_sends_no_basic_auth(make_client, oracle):
    keyed = OracleSettings(base_url=BASE, gateway_api_key="gw-1", gateway_api_key_header="x-CentraSite-APIKey")
    assert keyed.basic_auth is None
    run(make_client(keyed).get("/grades"))
    call = oracle.calls[-1]
    assert call.headers["x-centrasite-apikey"] == "gw-1"
    assert "authorization" not in call.headers, "no Basic Og== for an empty pair"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # neither credentials nor a key
        {"username": "u"},  # half a pair
        {"password": "p", "gateway_api_key": "k"},
        {"username": "u", "password": "p", "max_connections": 0},
        {"username": "u", "password": "p", "connect_retries": -1},
    ],
)
def test_settings_refuse_an_unusable_shape(kwargs):
    with pytest.raises(OracleConfigError):
        OracleSettings(base_url=BASE, **kwargs)


def test_the_owned_pool_is_bounded_and_retries_connects():
    client = OracleFusionClient(
        OracleSettings(base_url=BASE, username="u", password="p", max_connections=7, connect_retries=2)
    )
    pool = client._http()._transport._pool
    assert pool._max_connections == 7 and pool._retries == 2
    run(client.aclose())
