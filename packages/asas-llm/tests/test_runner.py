import json

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import BaseModel

import asas_llm
from asas_llm import context
from asas_llm.errors import EmptyOutput, GenerationFailed, PromptUnavailable, StructuredOutputError
from asas_llm.prompts import InMemoryPromptStore, Prompt
from asas_llm.runners import LLMRunner, ModelOptions, message_text, static_model, to_chat_prompt_template
from asas_llm.tracing import NullTracer
from tests.conftest import run


class Verdict(BaseModel):
    ok: bool
    reason: str


class RecordingFactory:
    """Remembers what the runner asked for and hands out a fake model."""

    def __init__(self, responses):
        self.model = FakeListChatModel(responses=list(responses))
        self.asked: list[tuple[str, ModelOptions]] = []

    def __call__(self, model, options):
        self.asked.append((model, options))
        return self.model


def make_runner(responses, prompts=None, **kw):
    store = prompts or InMemoryPromptStore()
    if prompts is None:
        store.chat("judge", [("system", "Judge {{ thing }}. Reply JSON."), ("user", "{{ input }}")])
        store.text("free", "Say something about {{ topic }}.")
        store.text("picked", "x", model="from-config", temperature=0.2)
    factory = RecordingFactory(responses)
    tracer = NullTracer()
    return LLMRunner(model_factory=factory, prompts=store, tracer=tracer, default_model="default-model", **kw), factory, tracer


def test_structured_call_returns_typed_value_and_trace_id():
    runner, factory, tracer = make_runner([json.dumps({"ok": True, "reason": "fine"})])
    context.bind_request("req-1")

    result = run(runner.ainvoke("judge", {"thing": "a CV", "input": "here"}, output_model=Verdict))

    assert isinstance(result.value, Verdict) and result.value.ok is True
    assert result.trace_id == tracer.traces[0].id
    assert tracer.traces[0].session_id == "req-1" and tracer.traces[0].name == "judge"
    assert context.trace_ids() == (result.trace_id,)
    assert result.prompt.name == "judge" and result.model == "default-model"
    assert tracer.updates[0][1]["output"] == {"ok": True, "reason": "fine"}
    assert factory.asked[0][1].name == "judge"


def test_text_call_appends_input_turn_when_prompt_lacks_it():
    runner, factory, _ = make_runner(["some text"])
    result = run(runner.ainvoke("free", {"topic": "rain", "input": "go"}))
    assert result.value == "some text" and result.text == "some text"
    assert factory.model.responses == ["some text"]


def test_prompt_object_can_be_passed_directly():
    runner, _, tracer = make_runner(["hi"])
    p = Prompt(name="inline", type="text", content="Say hi")
    result = run(runner.ainvoke(p, {}, name="custom-name"))
    assert result.value == "hi" and result.name == "custom-name" and tracer.traces[0].name == "custom-name"


def test_model_and_temperature_come_from_prompt_config_unless_overridden():
    runner, factory, _ = make_runner(["a", "b"])
    run(runner.ainvoke("picked", {}, input_variable=None))
    run(runner.ainvoke("picked", {}, model="explicit", temperature=0.9, input_variable=None))
    assert factory.asked[0] == ("from-config", ModelOptions(temperature=0.2, name="picked"))
    assert factory.asked[1] == ("explicit", ModelOptions(temperature=0.9, name="picked"))


def test_missing_prompt_raises_before_any_trace():
    runner, _, tracer = make_runner(["x"])
    with pytest.raises(PromptUnavailable) as info:
        run(runner.ainvoke("nope", {}))
    assert info.value.trace_id is None and tracer.traces == []


def test_provider_failure_is_typed_and_carries_trace_id():
    class Boom(FakeListChatModel):
        def _call(self, *args, **kwargs):
            raise TimeoutError("upstream timed out")

    store = InMemoryPromptStore()
    store.text("p", "x")
    tracer = NullTracer()
    runner = LLMRunner(model_factory=static_model(Boom(responses=['unused'])), prompts=store, tracer=tracer, default_model="m")
    context.bind_request("r")
    with pytest.raises(GenerationFailed) as info:
        run(runner.ainvoke("p", {}, input_variable=None))
    assert info.value.trace_id == tracer.traces[0].id
    assert isinstance(info.value.__cause__, TimeoutError)
    assert context.last_trace_id() == info.value.trace_id  # handed back even on failure


def test_empty_output_raises():
    runner, _, _ = make_runner(["   "])
    with pytest.raises(EmptyOutput):
        run(runner.ainvoke("free", {"topic": "x"}))


def test_structured_failure_raises_with_raw():
    runner, _, tracer = make_runner(["not json at all"])
    with pytest.raises(StructuredOutputError) as info:
        run(runner.ainvoke("judge", {"thing": "t", "input": "i"}, output_model=Verdict))
    assert info.value.raw == "not json at all" and info.value.trace_id == tracer.traces[0].id


def test_fenced_json_is_repaired():
    runner, _, _ = make_runner(['```json\n{"ok": false, "reason": "meh",}\n```'])
    result = run(runner.ainvoke("judge", {"thing": "t", "input": "i"}, output_model=Verdict))
    assert result.value.reason == "meh"


def test_history_is_inserted_after_system():
    store = InMemoryPromptStore()
    store.chat("chat", [("system", "sys"), ("user", "{{ input }}")])
    template = to_chat_prompt_template(store.get("chat"), history_variable="chat_history")
    msgs = template.format_messages(input="now", chat_history=[HumanMessage("before"), AIMessage("earlier")])
    assert [m.type for m in msgs] == ["system", "human", "ai", "human"]
    assert msgs[-1].content == "now"


def test_template_escapes_literal_braces_and_maps_roles():
    p = Prompt(name="t", type="chat", content=[("system", 'Return {"a": 1} for {{ x }}'), ("assistant", "ok"), ("developer", "d")])
    template = to_chat_prompt_template(p)
    msgs = template.format_messages(x="X")
    assert msgs[0].content == 'Return {"a": 1} for X'
    assert [m.type for m in msgs] == ["system", "ai", "system"]
    assert template.input_variables == ["x"]


def test_template_links_native_langfuse_prompt_but_not_fallbacks():
    class Native:
        is_fallback = False

    class Fallback:
        is_fallback = True

    p = Prompt(name="t", type="text", content="x", native=Native())
    assert to_chat_prompt_template(p).metadata == {"langfuse_prompt": p.native}
    q = Prompt(name="t", type="text", content="x", native=Fallback())
    assert not (to_chat_prompt_template(q).metadata or {}).get("langfuse_prompt")


def test_nested_call_inside_runner_trace_joins_it():
    runner, _, tracer = make_runner(["inner"])
    context.bind_request("r")

    async def flow():
        with runner.trace("agent", input={"q": 1}) as (handle, config):
            assert config["run_name"] == "agent" and "callbacks" not in config  # NullTracer has no handler
            inner = await runner.ainvoke("free", {"topic": "x"})
            handle.set_output(inner.value)
            return handle, inner

    handle, inner = run(flow())
    assert inner.trace_id == handle.id
    assert len(tracer.traces) == 1 and context.trace_ids() == (handle.id,)
    assert tracer.updates[-1][1]["output"] == "inner"


def test_flush_after_trace():
    runner, _, tracer = make_runner(["x"], flush_after_trace=True)
    run(runner.ainvoke("free", {"topic": "x"}))
    assert tracer.flushes == 1


def test_message_text_shapes():
    assert message_text(AIMessage(content="hi")) == "hi"
    assert message_text(AIMessage(content=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])) == "ab"
    assert message_text("s") == "s"
    assert message_text({"output": "o"}) == "o"
    assert message_text(None) == ""


def test_configure_and_process_wide_runner():
    store = InMemoryPromptStore()
    store.text("p", "x")
    asas_llm.configure(prompts=store, model_factory=static_model(FakeListChatModel(responses=["y"])), default_model="m")
    r1 = asas_llm.runner()
    assert r1 is asas_llm.runner()
    assert run(r1.ainvoke("p", {}, input_variable=None)).value == "y"
    asas_llm.configure(prompts=store, model_factory=static_model(FakeListChatModel(responses=["z"])), default_model="m")
    assert asas_llm.runner() is not r1


def test_dotted_mustache_variables_render():
    """Native mustache templating: {{user.name}} is a nested lookup and must
    render — the old f-string re-writer raised KeyError on every such call."""
    from asas_llm.prompts import InMemoryPromptStore
    from asas_llm.runners import to_chat_prompt_template

    store = InMemoryPromptStore()
    p = store.text("greet", "Hello {{ user.name }}")
    template = to_chat_prompt_template(p)
    rendered = template.format_messages(user={"name": "Ana"})
    assert rendered[-1].content == "Hello Ana"


def test_pinned_version_drops_the_default_label():
    """Langfuse rejects label+version together; a pinned read must not carry
    the default label along and get silently served a fallback copy."""
    seen = {}

    class Spy:
        def get(self, name, *, label=None, version=None):
            seen.update(label=label, version=version)
            from asas_llm.prompts import InMemoryPromptStore
            return InMemoryPromptStore().text(name, "x")

    runner = LLMRunner(model_factory=lambda m, o: None, prompts=Spy(),
                       default_model="m", default_label="production")
    runner.get_prompt("p", version=7)
    assert seen == {"label": None, "version": 7}
    runner.get_prompt("p")
    assert seen["label"] == "production"
