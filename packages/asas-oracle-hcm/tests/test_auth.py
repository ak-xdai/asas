"""Credentials: each shape, composition, the OAuth token cache and refresh,
the one retry after a 401, and lookup kinds a host adds."""

import asyncio
import base64

import httpx
import pytest

from asas_oracle_hcm import (
    ApiKeyAuth,
    BasicAuth,
    BearerToken,
    CompositeAuth,
    OAuthClientCredentials,
    OracleAuthError,
    OracleFusionClient,
    OracleLookups,
    OracleSettings,
)

from conftest import BASE, collection

TOKEN_URL = "https://idcs.example.com/oauth2/v1/token"


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Identity:
    """A token endpoint that numbers the tokens it issues."""

    def __init__(self, *, lifetime: int = 3600, status: int = 200) -> None:
        self.issued = 0
        self.lifetime = lifetime
        self.status = status
        self.requests: list[httpx.Request] = []

    def answer(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "invalid_client"})
        self.issued += 1
        return httpx.Response(200, json={"access_token": f"tok-{self.issued}", "expires_in": self.lifetime})


def _client(oracle, identity, auth, **kw):
    def route(request: httpx.Request) -> httpx.Response:
        if str(request.url) == TOKEN_URL:
            return identity.answer(request)
        return oracle.handler(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(route))
    settings = OracleSettings(base_url=BASE, gateway_api_key="unused")  # auth= replaces it
    return OracleFusionClient(settings, http=http, auth=auth, **kw)


def test_basic_api_key_and_bearer_headers():
    http = httpx.AsyncClient()
    assert run(BasicAuth("u", "p").headers(http)) == {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    assert run(ApiKeyAuth("k1", "x-gw").headers(http)) == {"x-gw": "k1"}
    assert run(BearerToken("t1").headers(http)) == {"Authorization": "Bearer t1"}
    both = CompositeAuth([ApiKeyAuth("k1"), BearerToken("t1")])
    assert run(both.headers(http)) == {"x-api-key": "k1", "Authorization": "Bearer t1"}
    with pytest.raises(ValueError):
        ApiKeyAuth("k", " ")


def test_the_token_is_fetched_once_and_reused(oracle):
    identity = Identity()
    oauth = OAuthClientCredentials(TOKEN_URL, "client", "secret", scope="urn:opc:resource:consumer::all")
    client = _client(oracle, identity, oauth)

    async def go():
        await asyncio.gather(*(client.get("/grades", {"q": f"GradeId={i}"}) for i in range(5)))

    run(go())
    assert identity.issued == 1, "concurrent requests share one fetch"
    assert {c.headers["authorization"] for c in oracle.calls} == {"Bearer tok-1"}
    form = identity.requests[0].content.decode()
    assert "grant_type=client_credentials" in form and "scope=urn" in form
    assert identity.requests[0].headers["authorization"] == "Basic " + base64.b64encode(b"client:secret").decode()


def test_the_token_is_refreshed_before_it_expires(oracle):
    identity = Identity(lifetime=300)
    clock = Clock()
    oauth = OAuthClientCredentials(TOKEN_URL, "c", "s", refresh_margin_seconds=60, clock=clock)
    client = _client(oracle, identity, oauth)
    run(client.get("/grades", use_cache=False))
    clock.now = 230  # 70s left: still fresh
    run(client.get("/grades", use_cache=False))
    assert identity.issued == 1
    clock.now = 245  # 55s left: inside the margin
    run(client.get("/grades", use_cache=False))
    assert identity.issued == 2 and oracle.calls[-1].headers["authorization"] == "Bearer tok-2"


def test_a_401_renews_the_token_and_retries_once(oracle):
    identity = Identity()
    seen: list[str] = []

    def grades(rec):
        seen.append(rec.headers["authorization"])
        if rec.headers["authorization"] == "Bearer tok-1":
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"items": [{"GradeId": 1}]})

    oracle.route("/grades", grades)
    client = _client(oracle, identity, OAuthClientCredentials(TOKEN_URL, "c", "s"))
    assert run(client.get("/grades", use_cache=False))["items"]
    assert seen == ["Bearer tok-1", "Bearer tok-2"]


def test_a_401_with_a_fixed_credential_is_not_retried(oracle, make_client):
    oracle.route("/grades", (401, {}))
    client = make_client()
    with pytest.raises(OracleAuthError):
        run(client.get("/grades", use_cache=False))
    assert len(oracle.calls_to("/grades")) == 1


def test_a_401_that_survives_renewal_is_an_auth_error(oracle):
    identity = Identity()
    oracle.route("/grades", (401, {}))
    client = _client(oracle, identity, OAuthClientCredentials(TOKEN_URL, "c", "s"))
    with pytest.raises(OracleAuthError):
        run(client.get("/grades", use_cache=False))
    assert len(oracle.calls_to("/grades")) == 2 and identity.issued == 2


def test_a_refused_token_request_is_an_auth_error_and_sends_nothing(oracle):
    identity = Identity(status=401)
    client = _client(oracle, identity, OAuthClientCredentials(TOKEN_URL, "c", "wrong"))
    with pytest.raises(OracleAuthError) as exc:
        run(client.get("/grades", use_cache=False))
    assert exc.value.status == 401 and "wrong" not in str(exc.value)
    assert oracle.calls == []
    assert client.health.snapshot()["breaker"] == "closed"


def test_settings_build_oauth_with_a_gateway_key(oracle):
    s = OracleSettings(
        base_url=BASE, gateway_api_key="gw", oauth_token_url=TOKEN_URL,
        oauth_client_id="c", oauth_client_secret="s",
    )
    identity = Identity()

    def route(request):
        return identity.answer(request) if str(request.url) == TOKEN_URL else oracle.handler(request)

    client = OracleFusionClient(s, http=httpx.AsyncClient(transport=httpx.MockTransport(route)))
    run(client.get("/grades"))
    call = oracle.calls[-1]
    assert call.headers["x-api-key"] == "gw" and call.headers["authorization"] == "Bearer tok-1"


def test_a_host_adds_its_own_lookup_kind(client, oracle):
    oracle.route("/locations", collection({"LocationId": "77", "LocationName": "Head office"}))
    lookups = OracleLookups(client, kinds={"location": ("/locations", "LocationId", "LocationName")})
    assert run(lookups.names("location", ["77"])) == {"77": "Head office"}
    assert "grade" in lookups.kinds and "location" in lookups.kinds
    assert oracle.calls[0].params["q"] == "LocationId=77"
    with pytest.raises(ValueError):
        OracleLookups(client, kinds={"broken": ("/x", "", "Name")})


def test_an_address_the_grammar_cannot_express_finds_no_one(client, oracle):
    """A quote in an address cannot go into q; the lookup asks nothing for it
    rather than stripping it into somebody else's address."""
    assert run(OracleLookups(client).find_worker("o'brien@example.org")) is None
    assert oracle.calls == []
