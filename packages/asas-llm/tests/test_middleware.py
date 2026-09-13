from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from asas_llm import context
from asas_llm.middleware import TraceIdMiddleware
from asas_llm.tracing import NullTracer, traced

tracer = NullTracer()


async def endpoint(request):
    with traced("first", tracer) as a:
        pass
    with traced("second", tracer) as b:
        pass
    return JSONResponse({"session": context.current_session_id(), "last": context.last_trace_id(), "ids": [a.id, b.id]})


async def quiet(request):
    return JSONResponse({"session": context.current_session_id()})


def make_app(**kw):
    app = Starlette(routes=[Route("/", endpoint), Route("/quiet", quiet)])
    app.add_middleware(TraceIdMiddleware, **kw)
    return TestClient(app)


def test_incoming_correlation_id_groups_traces_and_is_echoed():
    client = make_app()
    r = client.get("/", headers={"X-Correlation-Id": "corr-42"})
    body = r.json()
    assert body["session"] == "corr-42"
    assert r.headers["x-correlation-id"] == "corr-42"
    assert r.headers["x-trace-id"] == ",".join(body["ids"])
    assert body["last"] == body["ids"][-1]
    assert all(t.session_id == "corr-42" for t in tracer.traces[-2:])


def test_missing_correlation_id_is_generated():
    client = make_app()
    r = client.get("/quiet")
    assert len(r.json()["session"]) == 32
    assert r.headers["x-correlation-id"] == r.json()["session"]
    assert "x-trace-id" not in r.headers  # nothing traced, nothing claimed


def test_custom_header_names_and_no_echo():
    client = make_app(session_header="X-Request-Id", trace_header="X-LLM-Trace", echo_session=False)
    r = client.get("/", headers={"X-Request-Id": "abc"})
    assert r.json()["session"] == "abc"
    assert "x-llm-trace" in r.headers and "x-request-id" not in r.headers


def test_scope_is_cleared_after_the_request():
    client = make_app()
    client.get("/", headers={"X-Correlation-Id": "c"})
    assert context.current_scope() is None
