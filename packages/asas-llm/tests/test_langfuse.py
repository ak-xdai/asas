"""The Langfuse adapter against a stub client: no network, no keys. The real
SDK is imported only for the compat-shim test, which is skipped without it."""

import sys
import types

import pytest

from asas_llm.errors import PromptUnavailable
from asas_llm.langfuse import LangfusePromptStore, LangfuseTracer, install_langchain_compat, prompt_from_client
from asas_llm.prompts import LayeredPromptStore
from asas_llm.tracing import traced


class StubPromptClient:
    def __init__(self, name, prompt, version=1, labels=("production",), config=None, is_fallback=False):
        self.name, self.prompt, self.version, self.labels = name, prompt, version, list(labels)
        self.config = config or {}
        self.is_fallback = is_fallback


class StubTrace:
    def __init__(self, **kw):
        self.kw = kw
        self.updates = []
        self.handler_args = None

    def get_langchain_handler(self, update_parent=False):
        self.handler_args = update_parent
        return f"handler:{self.kw['id']}"

    def update(self, **kw):
        self.updates.append(kw)


class StubClient:
    def __init__(self, prompts=None, fail=False):
        self.prompts = prompts or {}
        self.fail = fail
        self.traces = []
        self.flushed = self.shutdowns = 0
        self.get_calls = []
        self.api = types.SimpleNamespace(prompts=types.SimpleNamespace(list=self._list))

    def get_prompt(self, name, version=None, **kw):
        self.get_calls.append((name, version, kw))
        if self.fail:
            raise ConnectionError("no route to langfuse")
        if name not in self.prompts:
            raise Exception("404 prompt not found")
        return self.prompts[name]

    def _list(self, page=1, limit=100):
        names = sorted(self.prompts)
        return types.SimpleNamespace(
            data=[types.SimpleNamespace(name=n) for n in names[(page - 1) * limit : page * limit]],
            meta=types.SimpleNamespace(total_pages=max(1, -(-len(names) // limit))),
        )

    def trace(self, **kw):
        t = StubTrace(**kw)
        self.traces.append(t)
        return t

    def flush(self):
        self.flushed += 1

    def shutdown(self):
        self.shutdowns += 1


def test_prompt_conversion_text_and_chat():
    text = prompt_from_client(StubPromptClient("t", "Hello {{ name }}", version=4, config={"model": "gpt-x"}))
    assert text.type == "text" and text.version == 4 and text.config == {"model": "gpt-x"} and text.source == "langfuse"
    assert text.native is not None
    chat = prompt_from_client(StubPromptClient("c", [{"role": "system", "content": "s"}, {"role": "user", "content": "{{input}}"}]))
    assert chat.type == "chat" and chat.messages == (("system", "s"), ("user", "{{input}}"))


def test_store_passes_label_version_and_bounds():
    client = StubClient({"p": StubPromptClient("p", "x")})
    store = LangfusePromptStore(client, fetch_timeout_seconds=3, max_retries=0)
    store.get("p", label="production", version=2)
    name, version, kw = client.get_calls[0]
    assert (name, version) == ("p", 2)
    assert kw == {"cache_ttl_seconds": 0, "label": "production", "fetch_timeout_seconds": 3, "max_retries": 0}


def test_store_failures_become_unavailable():
    down = LangfusePromptStore(StubClient(fail=True))
    with pytest.raises(PromptUnavailable):
        down.get("p")
    missing = LangfusePromptStore(StubClient({}))
    with pytest.raises(PromptUnavailable):
        missing.get("p")
    fallback = LangfusePromptStore(StubClient({"p": StubPromptClient("p", "x", is_fallback=True)}))
    with pytest.raises(PromptUnavailable):
        fallback.get("p")


def test_store_lists_names_across_pages():
    client = StubClient({f"p{i:03d}": StubPromptClient(f"p{i:03d}", "x") for i in range(250)})
    assert len(LangfusePromptStore(client).names()) == 250


def test_layered_over_langfuse_survives_outage(tmp_path):
    client = StubClient({"p": StubPromptClient("p", "live", version=9)})
    layered = LayeredPromptStore(LangfusePromptStore(client), None)
    assert layered.get("p").source == "langfuse"
    client.fail = True
    layered.invalidate()  # force a refresh attempt
    layered._cache  # noqa: B018 - cache cleared, so this falls to "no fallback"
    with pytest.raises(PromptUnavailable):
        layered.get("p")


def test_tracer_starts_updates_and_flushes():
    client = StubClient()
    tracer = LangfuseTracer(client)
    with traced("run", tracer, input={"a": 1}, session_id="sess", metadata={"k": "v"}, flush=True) as handle:
        assert client.traces[0].kw == {"id": handle.id, "name": "run", "session_id": "sess", "input": {"a": 1}, "metadata": {"k": "v"}}
        handle.set_output({"done": True})
        handle.set_metadata(extra=1)
    assert client.traces[0].updates == [{"output": {"done": True}}, {"metadata": {"extra": 1}}]
    assert client.flushed == 1
    tracer.shutdown()
    assert client.shutdowns == 1


def test_tracer_handler_applies_compat_when_langchain_present():
    pytest.importorskip("langchain_core")
    client = StubClient()
    tracer = LangfuseTracer(client)
    with traced("run", tracer) as handle:
        assert handle.langchain_handler(update_parent=False) == f"handler:{handle.id}"
        assert client.traces[0].handler_args is False


def test_compat_shim_makes_langfuse_callback_importable():
    pytest.importorskip("langchain_core")
    pytest.importorskip("langfuse")
    assert install_langchain_compat() is True
    import langchain.callbacks.base  # noqa: F401  (aliased or real)
    from langfuse.callback import CallbackHandler  # the import the source engine needed the shim for

    assert CallbackHandler is not None
    assert "langchain.schema.agent" in sys.modules
