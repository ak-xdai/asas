import asyncio

from asas_llm import context
from asas_llm.tracing import NullTracer, traced
from tests.conftest import run


def test_bind_generates_a_session_when_none_given():
    scope = context.bind_request()
    assert len(scope.session_id) == 32
    assert context.current_session_id() == scope.session_id


def test_bind_keeps_a_given_session_id():
    context.bind_request("corr-1", user="u1")
    assert context.current_session_id() == "corr-1"
    assert context.current_scope().metadata == {"user": "u1"}


def test_record_and_hand_back_in_order():
    context.bind_request("s")
    context.record_trace("a")
    context.record_trace("b")
    context.record_trace("a")  # duplicates collapse
    assert context.trace_ids() == ("a", "b")
    assert context.last_trace_id() == "b"


def test_record_outside_scope_is_a_noop():
    context.record_trace("orphan")
    assert context.trace_ids() == ()
    assert context.last_trace_id() is None


def test_request_scope_restores_parent():
    context.bind_request("outer")
    with context.request_scope("inner") as inner:
        assert inner.session_id == "inner"
        context.record_trace("t-inner")
    assert context.current_session_id() == "outer"
    assert context.trace_ids() == ()


def test_rebinding_drops_previous_trace_ids():
    context.bind_request("one")
    context.record_trace("t1")
    context.bind_request("two")
    assert context.trace_ids() == ()


def test_fan_out_records_on_the_parent_scope():
    """asyncio.gather copies the context per task; the list inside is shared,
    so every sub-call's trace lands on the request that will report them."""
    tracer = NullTracer()

    async def one(name):
        with traced(name, tracer):
            await asyncio.sleep(0)

    async def main():
        context.bind_request("req")
        await asyncio.gather(*(one(f"call-{i}") for i in range(5)))
        return context.trace_ids()

    ids = run(main())
    assert len(ids) == 5
    assert {t.name for t in tracer.traces} == {f"call-{i}" for i in range(5)}
    assert all(t.session_id == "req" for t in tracer.traces)


def test_nested_traced_joins_the_enclosing_trace():
    tracer = NullTracer()
    context.bind_request("req")
    with traced("agent", tracer) as outer:
        with traced("tool-call", tracer) as inner:
            assert inner.id == outer.id
            assert inner.owned is False
            assert inner.langchain_handler() is None
            inner.set_output("ignored")
    assert len(tracer.traces) == 1
    assert tracer.updates == []
    assert context.trace_ids() == (outer.id,)


def test_nest_false_opens_a_fresh_trace():
    tracer = NullTracer()
    with traced("agent", tracer) as outer:
        with traced("separate", tracer, nest=False) as inner:
            assert inner.id != outer.id
    assert len(tracer.traces) == 2


def test_traced_flushes_when_asked():
    tracer = NullTracer()
    with traced("x", tracer, flush=True):
        pass
    with traced("y", tracer):
        pass
    assert tracer.flushes == 1


def test_scope_metadata_reaches_the_trace():
    tracer = NullTracer()
    context.bind_request("req", tenant="t1")
    with traced("x", tracer, metadata={"area": "skills"}):
        pass
    assert tracer.traces[0].native["metadata"] == {"tenant": "t1", "area": "skills"}
