"""Breakers per resource and for the host, what counts as an outage, the call
statistics, the hook, the per-request counter, the auth and throttle errors,
the pooled transport and the public request()."""

import asyncio

import httpx
import pytest

from asas_oracle_hcm import (
    BreakerPolicy,
    OracleAuthError,
    OracleConfigError,
    OracleFusionClient,
    OracleNotFoundError,
    OracleSettings,
    OracleUnavailableError,
    OracleUpstreamError,
    count_calls,
    is_outage,
)

from conftest import BASE, collection


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _policy(failures: int = 3) -> tuple[BreakerPolicy, Clock]:
    clock = Clock()
    return BreakerPolicy(failures=failures, cooldown_seconds=30.0, clock=clock), clock


def test_what_counts_as_an_outage():
    assert is_outage(None) and is_outage(429) and is_outage(502) and is_outage(503) and is_outage(504)
    for answer in (200, 400, 401, 403, 404, 409, 500):
        assert not is_outage(answer), answer


def test_a_resources_outages_open_its_breaker_only(make_client, oracle):
    oracle.route("/positions", (503, {}))
    oracle.route("/grades", collection({"GradeId": 1}))
    policy, clock = _policy()
    client = make_client(breaker=policy)

    async def go():
        for _ in range(3):
            with pytest.raises(OracleUpstreamError) as exc:
                await client.get("/positions", use_cache=False)
            assert not isinstance(exc.value, OracleUnavailableError)
        with pytest.raises(OracleUnavailableError) as refused:
            await client.get("/positions", use_cache=False)
        assert refused.value.retry_after_seconds > 0 and refused.value.is_transient
        assert len(oracle.calls_to("/positions")) == 3, "the fourth read never left the process"
        # Another resource is untouched by /positions failing.
        assert (await client.get("/grades", use_cache=False))["items"]

        # Writes are never refused: the caller's outbox owns their retry.
        oracle.route("/positions/1", (503, {}))
        with pytest.raises(OracleUpstreamError):
            await client.patch("/positions/1", {"Name": "x"})
        assert oracle.calls[-1].method == "PATCH"

        # After the cooldown one probe goes out; its fault reopens at once.
        clock.now += 31
        with pytest.raises(OracleUpstreamError):
            await client.get("/positions", use_cache=False)
        assert client.health.breaker("positions").state == "open"

    run(go())
    snap = client.health.snapshot()
    assert snap["breaker"] == "open" and "positions" in snap["breakers"] and "grades" not in snap["breakers"]
    assert snap["refused"] == 1


def test_a_500_is_an_answer_so_one_broken_operation_cannot_switch_reads_off(make_client, oracle):
    """A gateway answers 500 for an operation registered wrong, and Fusion for a
    query it cannot run. Neither is an outage."""
    oracle.route("/grades", (500, {"Exception": "Transport protocol not supported"}))
    policy, _ = _policy()
    client = make_client(breaker=policy)

    async def go():
        for _ in range(10):
            with pytest.raises(OracleUpstreamError):
                await client.get("/grades", use_cache=False)

    run(go())
    assert len(oracle.calls_to("/grades")) == 10, "never refused"
    assert client.health.snapshot()["breaker"] == "closed"


def test_connection_failures_open_the_host_breaker_for_every_resource(settings):
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    policy, _ = _policy()
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.path)
        return refuse(request)

    client = OracleFusionClient(settings, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)), breaker=policy)

    async def go():
        for path in ("/grades", "/jobs", "/positions"):
            with pytest.raises(OracleUpstreamError):
                await client.get(path, use_cache=False)
        # Three connection failures across three resources: the HOST is down.
        with pytest.raises(OracleUnavailableError):
            await client.get("/departments", use_cache=False)

    run(go())
    assert len(calls) == 3
    assert client.health.breaker("*").state == "open"


def test_a_successful_probe_closes_it_and_only_one_probe_goes_out(make_client, oracle):
    healthy = {"on": False}
    oracle.route("/grades", lambda rec: httpx.Response(200, json={"items": []}) if healthy["on"] else httpx.Response(502, json={}))
    policy, clock = _policy()
    client = make_client(breaker=policy)

    async def go():
        for _ in range(3):
            with pytest.raises(OracleUpstreamError):
                await client.get("/grades", use_cache=False)
        clock.now += 31
        breaker = client.health.breaker("grades")
        assert breaker.allow() is True, "the probe"
        assert breaker.allow() is False, "and no second one while it is out"
        breaker.release_probe()
        healthy["on"] = True
        assert await client.get("/grades", use_cache=False) == {"items": []}
        assert breaker.state == "closed" and breaker.consecutive == 0

    run(go())


def test_the_breakers_can_be_turned_off(make_client, oracle):
    oracle.route("/grades", (503, {}))
    client = make_client(breaker=BreakerPolicy(failures=0))

    async def go():
        for _ in range(6):
            with pytest.raises(OracleUpstreamError) as exc:
                await client.get("/grades", use_cache=False)
            assert not isinstance(exc.value, OracleUnavailableError)

    run(go())
    snap = client.health.snapshot()
    assert snap["breaker"] == "disabled" and snap["faults"] == 6


def test_401_and_403_are_auth_errors_never_outages(make_client, oracle):
    oracle.route("/grades", (401, {}))
    oracle.route("/jobs", (403, {}))
    policy, _ = _policy(failures=1)
    client = make_client(breaker=policy)

    async def go():
        for path in ("/grades", "/jobs", "/grades"):
            with pytest.raises(OracleAuthError) as exc:
                await client.get(path, use_cache=False)
            assert exc.value.is_transient is False

    run(go())
    assert client.health.snapshot()["breaker"] == "closed"


def test_a_throttle_carries_retry_after(make_client, oracle):
    oracle.route("/grades", lambda rec: httpx.Response(429, headers={"Retry-After": "12"}, json={}))
    oracle.route("/jobs", lambda rec: httpx.Response(503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}, json={}))
    client = make_client(breaker=BreakerPolicy(failures=0))
    with pytest.raises(OracleUpstreamError) as exc:
        run(client.get("/grades", use_cache=False))
    assert exc.value.status == 429 and exc.value.retry_after_seconds == 12
    with pytest.raises(OracleUpstreamError) as past:
        run(client.get("/jobs", use_cache=False))
    assert past.value.retry_after_seconds == 0, "a date in the past means now"


def test_404_and_400_are_answers_not_faults(make_client, oracle):
    oracle.route("/workers/missing", (404, {}))
    oracle.route("/grades", (400, {}))
    policy, _ = _policy()
    client = make_client(breaker=policy)

    async def go():
        for _ in range(5):
            with pytest.raises(OracleNotFoundError):
                await client.get("/workers/missing")
            with pytest.raises(OracleUpstreamError):
                await client.get("/grades")

    run(go())
    snap = client.health.snapshot()
    assert (snap["breaker"], snap["faults"], snap["calls"]) == ("closed", 0, 10)


def test_the_hook_sees_every_request_and_a_raising_hook_is_ignored(make_client, oracle):
    oracle.route("/grades", collection({"GradeId": 1}))
    oracle.route("/jobs", (503, {}))
    events = []

    def hook(event):
        events.append(event)
        raise RuntimeError("a broken metrics exporter")

    client = make_client(on_request=hook, breaker=BreakerPolicy(failures=1))

    async def go():
        await client.get("/grades", use_cache=False)
        with pytest.raises(OracleUpstreamError):
            await client.get("/jobs", use_cache=False)
        with pytest.raises(OracleUnavailableError):
            await client.get("/jobs", use_cache=False)

    run(go())
    assert [(e.resource, e.status, e.fault, e.refused) for e in events] == [
        ("grades", 200, False, False),
        ("jobs", 503, True, False),
        ("jobs", None, False, True),
    ]
    assert all(e.elapsed_ms >= 0 for e in events)


def test_request_is_public_and_can_return_an_error_response(client, oracle):
    oracle.route("/grades", (500, {"Exception": "boom"}))
    response = run(client.request("GET", "/grades", raise_for_status=False))
    assert response.status_code == 500
    with pytest.raises(OracleUpstreamError):
        run(client.request("GET", "/grades"))


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


def test_extra_headers_ride_on_every_request(make_client, oracle):
    s = OracleSettings(base_url=BASE, username="u", password="p", extra_headers={"REST-Framework-Version": "4"})
    run(make_client(s).get("/grades"))
    assert oracle.calls[-1].headers["rest-framework-version"] == "4"


def test_gateway_key_alone_is_allowed_and_sends_no_basic_auth(make_client, oracle):
    keyed = OracleSettings(base_url=BASE, gateway_api_key="gw-1", gateway_api_key_header="X-Gateway-Key")
    assert keyed.basic_auth is None
    run(make_client(keyed).get("/grades"))
    call = oracle.calls[-1]
    assert call.headers["x-gateway-key"] == "gw-1"
    assert "authorization" not in call.headers, "no Basic Og== for an empty pair"


@pytest.mark.parametrize(
    "kwargs",
    [
        {},  # no credentials at all
        {"username": "u"},  # half a pair
        {"password": "p", "gateway_api_key": "k"},
        {"oauth_token_url": "https://idp/token", "oauth_client_id": "c"},  # half OAuth
        {"oauth_token_url": "http://idp/token", "oauth_client_id": "c", "oauth_client_secret": "s"},
        {"username": "u", "password": "p", "max_connections": 0},
        {"username": "u", "password": "p", "connect_retries": -1},
    ],
)
def test_settings_refuse_an_unusable_shape(kwargs):
    with pytest.raises(OracleConfigError):
        OracleSettings(base_url=BASE, **kwargs)


def test_settings_never_show_a_secret():
    basic = OracleSettings(base_url=BASE, username="u", password="pw-123", gateway_api_key="gw-456")
    oauth = OracleSettings(
        base_url=BASE, gateway_api_key="gw-456",
        oauth_token_url="https://idp/token", oauth_client_id="c", oauth_client_secret="sec-789",
    )
    shown = repr(basic) + repr(basic.auth()) + repr(oauth) + repr(oauth.auth())
    for secret in ("pw-123", "gw-456", "sec-789"):
        assert secret not in shown
    assert basic.describe_auth() == "Basic + API key (x-api-key)"
    assert oauth.describe_auth() == "API key (x-api-key) + OAuth client credentials"


def test_basic_and_oauth_cannot_both_own_the_authorization_header():
    with pytest.raises(OracleConfigError):
        OracleSettings(base_url=BASE, username="u", password="p", oauth_token_url="https://idp/token",
                       oauth_client_id="c", oauth_client_secret="s")


def test_the_owned_pool_is_bounded_and_retries_connects():
    client = OracleFusionClient(
        OracleSettings(base_url=BASE, username="u", password="p", max_connections=7, connect_retries=2)
    )
    pool = client._http()._transport._pool
    assert pool._max_connections == 7 and pool._retries == 2
    run(client.aclose())
