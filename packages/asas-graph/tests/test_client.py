"""The HTTP layer against a recording ``MockTransport``."""

import asyncio
import ssl

import httpx
import pytest

from asas_graph import (
    GraphClient,
    GraphError,
    GraphRequestError,
    StaticTokenProvider,
    default_ssl_context,
)


def test_get_sends_bearer_token_and_accept_header(client, graph):
    graph.body = {"value": []}
    body = asyncio.run(client.get("/users/u1/events", params={"$top": 5}))
    assert body == {"value": []}
    call = graph.only
    assert call.method == "GET"
    assert call.url == "https://graph.microsoft.com/v1.0/users/u1/events?%24top=5"
    assert call.headers["authorization"] == "Bearer tok-123"
    assert call.headers["accept"] == "application/json"


def test_path_without_leading_slash_joins_the_same_way(client, graph):
    graph.body = {}
    asyncio.run(client.get("me"))
    assert graph.only.url == "https://graph.microsoft.com/v1.0/me"


def test_post_sends_json_and_returns_the_body(client, graph):
    graph.status, graph.body = 201, {"id": "evt-1"}
    body = asyncio.run(client.post("/users/u1/events", {"subject": "x"}))
    assert body == {"id": "evt-1"}
    assert graph.only.json == {"subject": "x"}
    assert graph.only.headers["content-type"] == "application/json"


@pytest.mark.parametrize("status", [202, 204])
def test_empty_successes_return_none(client, graph, status):
    """sendMail is 202 with no body; PATCH/DELETE are 204. Neither is an error."""
    graph.status = status
    assert asyncio.run(client.post("/users/u1/sendMail", {"message": {}})) is None


def test_patch_and_delete_use_their_verbs(client, graph):
    graph.status = 204
    asyncio.run(client.patch("/users/u1/events/e1", {"subject": "y"}))
    asyncio.run(client.delete("/users/u1/events/e1"))
    assert [c.method for c in graph.calls] == ["PATCH", "DELETE"]
    assert graph.calls[1].json is None


def test_a_graph_error_envelope_is_parsed_into_the_exception(client, graph):
    graph.status = 403
    graph.body = {
        "error": {
            "code": "ErrorAccessDenied",
            "message": "Access is denied. Check credentials and try again.",
            "innerError": {"request-id": "req-42", "date": "2026-09-11T10:00:00"},
        }
    }
    with pytest.raises(GraphRequestError) as exc:
        asyncio.run(client.post("/users/u1/events", {}))
    err = exc.value
    assert (err.status, err.method, err.path) == (403, "POST", "/users/u1/events")
    assert err.code == "ErrorAccessDenied"
    assert err.message.startswith("Access is denied")
    assert err.request_id == "req-42"
    assert err.detail == graph.body
    assert "403" in str(err) and "ErrorAccessDenied" in str(err)
    assert not err.is_transient and not err.is_throttled and not err.is_not_found


def test_throttling_exposes_retry_after(client, graph):
    graph.status, graph.headers = 429, {"Retry-After": "7"}
    graph.body = {"error": {"code": "TooManyRequests", "message": "throttled"}}
    with pytest.raises(GraphRequestError) as exc:
        asyncio.run(client.get("/me"))
    assert exc.value.is_throttled and exc.value.is_transient
    assert exc.value.retry_after == 7.0


def test_a_non_json_error_body_is_kept_as_text(settings):
    def handler(request):
        return httpx.Response(502, text="<html>Bad gateway</html>")

    client = GraphClient(
        settings,
        token_provider=StaticTokenProvider("t"),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GraphRequestError) as exc:
        asyncio.run(client.get("/me"))
    assert exc.value.status == 502
    assert exc.value.detail == "<html>Bad gateway</html>"
    assert exc.value.code is None and exc.value.request_id is None
    assert exc.value.is_transient


def test_404_is_not_found(client, graph):
    graph.status, graph.body = 404, {"error": {"code": "ErrorItemNotFound", "message": "gone"}}
    with pytest.raises(GraphRequestError) as exc:
        asyncio.run(client.delete("/users/u1/events/missing"))
    assert exc.value.is_not_found


def test_a_non_json_success_body_is_an_error_not_a_crash(settings):
    def handler(request):
        return httpx.Response(200, text="not json")

    client = GraphClient(
        settings,
        token_provider=StaticTokenProvider("t"),
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(GraphError, match="non-JSON"):
        asyncio.run(client.get("/me"))


def test_token_is_fetched_per_request_from_the_provider(settings, graph):
    """The client caches nothing: a provider that rotates is honoured."""
    tokens = iter(["first", "second"])

    class Rotating:
        async def access_token(self):
            return next(tokens)

    client = GraphClient(
        settings, token_provider=Rotating(),
        http=httpx.AsyncClient(transport=httpx.MockTransport(graph.handler)),
    )
    graph.body = {}
    asyncio.run(client.get("/a"))
    asyncio.run(client.get("/b"))
    assert [c.headers["authorization"] for c in graph.calls] == ["Bearer first", "Bearer second"]


def test_default_ssl_context_trusts_certifi_and_verifies():
    ctx = default_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.cert_store_stats()["x509_ca"] > 0, "certifi bundle not loaded"
    assert default_ssl_context() is ctx  # built once


def test_an_owned_http_client_is_closed_and_a_borrowed_one_is_not(settings):
    async def scenario():
        owned = GraphClient(settings, token_provider=StaticTokenProvider("t"))
        inner = owned._client()
        assert inner.is_closed is False
        async with owned:
            pass
        assert inner.is_closed is True

        borrowed_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        borrowed = GraphClient(settings, token_provider=StaticTokenProvider("t"), http=borrowed_http)
        await borrowed.aclose()
        assert borrowed_http.is_closed is False
        await borrowed_http.aclose()

    asyncio.run(scenario())


def test_owned_client_uses_the_certifi_context_and_settings_timeout(settings):
    client = GraphClient(settings, token_provider=StaticTokenProvider("t"))
    http = client._client()
    assert http.timeout == httpx.Timeout(settings.timeout_seconds)
    asyncio.run(client.aclose())


def test_transport_failures_are_typed_and_transient(settings):
    """Timeouts/DNS/TLS must honor the everything-derives-from-GraphError
    contract and read as transient for host retry loops."""
    import httpx
    from asas_graph.errors import GraphError, GraphTransportError

    class BoomHTTP:
        is_closed = False
        async def request(self, *a, **k):
            raise httpx.ReadTimeout("slow graph")

    client = GraphClient(settings, token_provider=StaticTokenProvider("t"), http=BoomHTTP())
    with pytest.raises(GraphTransportError) as exc:
        asyncio.run(client.get("/users/x/events"))
    assert isinstance(exc.value, GraphError)
    assert exc.value.is_transient and exc.value.status == 0


def test_retry_after_parses_http_date_and_rejects_inf():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    from asas_graph.client import _retry_after_seconds

    when = datetime.now(timezone.utc) + timedelta(seconds=90)
    parsed = _retry_after_seconds(format_datetime(when, usegmt=True))
    assert parsed is not None and 80 <= parsed <= 91
    assert _retry_after_seconds("inf") is None
    assert _retry_after_seconds("120") == 120.0


def test_settings_repr_never_prints_the_secret():
    from asas_graph.settings import GraphSettings
    s = GraphSettings(tenant_id="t", client_id="c", client_secret="TOP-SECRET")
    assert "TOP-SECRET" not in repr(s)
