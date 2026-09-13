import json
import logging

import pytest

from asas_llm.errors import PromptFormatError, PromptUnavailable
from asas_llm.prompts import InMemoryPromptStore, LayeredPromptStore, LocalPromptStore, Prompt


class FlakyStore:
    """A registry we can switch off."""

    def __init__(self, prompts: dict[str, Prompt]):
        self.prompts = prompts
        self.up = True
        self.calls = 0

    def get(self, name, *, label=None, version=None):
        self.calls += 1
        if not self.up:
            raise ConnectionError("registry down")
        try:
            return self.prompts[name]
        except KeyError:
            raise PromptUnavailable(f"no {name}", prompt=name) from None

    def names(self):
        if not self.up:
            raise ConnectionError("registry down")
        return sorted(self.prompts)


def remote(name="greet", version=3, body="Hello {{ name }}, you are {{ role }}."):
    return Prompt(name=name, type="text", content=body, version=version, labels=("production",), config={"model": "gpt-x"}, source="langfuse")


# --- Prompt -------------------------------------------------------------------


def test_variables_and_compile_text():
    p = remote()
    assert p.variables() == ("name", "role")
    assert p.compile(name="Ana") == "Hello Ana, you are {{ role }}."


def test_chat_prompt_messages_and_compile():
    p = Prompt(name="c", type="chat", content=[("system", "Be {{tone}}"), ("user", "{{input}}")])
    assert p.messages == (("system", "Be {{tone}}"), ("user", "{{input}}"))
    assert p.compile(tone="brief", input="hi") == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "hi"},
    ]


def test_text_prompt_becomes_one_system_message():
    assert remote().messages == (("system", "Hello {{ name }}, you are {{ role }}."),)


def test_type_body_mismatch_is_a_format_error():
    with pytest.raises(PromptFormatError):
        Prompt(name="x", type="chat", content="not a list")
    with pytest.raises(PromptFormatError):
        Prompt(name="x", type="text", content=[("system", "s")])


def test_round_trip_through_dict_drops_native():
    p = Prompt(name="c", type="chat", content=[("system", "s")], version=2, config={"t": 0}, native=object())
    again = Prompt.from_dict(p.to_dict(), source="local")
    assert again == Prompt(name="c", type="chat", content=[("system", "s")], version=2, config={"t": 0}, source="local")
    assert again.native is None
    assert again.source == "local"


# --- InMemory -----------------------------------------------------------------


def test_in_memory_store():
    store = InMemoryPromptStore()
    store.text("a", "body", model="m")
    store.chat("b", [("system", "s"), ("user", "{{input}}")])
    assert store.names() == ["a", "b"]
    assert store.get("a").config == {"model": "m"}
    assert store.get("b", label="production").type == "chat"
    with pytest.raises(PromptUnavailable):
        store.get("missing")


# --- Local --------------------------------------------------------------------


def test_local_store_reads_json_and_markdown(tmp_path):
    (tmp_path / "greet.json").write_text(json.dumps(remote().to_dict()))
    (tmp_path / "plain.md").write_text("You are terse.\n")
    store = LocalPromptStore(tmp_path)
    assert store.names() == ["greet", "plain"]
    greet = store.get("greet", label="production", version=99)  # label/version ignored
    assert greet.version == 3 and greet.source == "local" and greet.type == "text"
    plain = store.get("plain")
    assert plain.content == "You are terse." and plain.version is None


def test_local_store_encodes_awkward_names(tmp_path):
    store = LocalPromptStore(tmp_path)
    p = Prompt(name="team/parse cv", type="text", content="x")
    store.path_for(p.name).write_text(json.dumps(p.to_dict()))
    assert store.names() == ["team/parse cv"]
    assert store.get("team/parse cv").content == "x"


def test_local_store_missing_and_malformed(tmp_path):
    store = LocalPromptStore(tmp_path)
    with pytest.raises(PromptUnavailable):
        store.get("nope")
    (tmp_path / "bad.json").write_text("{not json")
    with pytest.raises(PromptFormatError):
        store.get("bad")
    (tmp_path / "wrong.json").write_text(json.dumps({**remote().to_dict(), "name": "other"}))
    with pytest.raises(PromptFormatError):
        store.get("wrong")
    (tmp_path / "list.json").write_text("[]")
    with pytest.raises(PromptFormatError):
        store.get("list")


def test_local_store_on_missing_directory(tmp_path):
    store = LocalPromptStore(tmp_path / "absent")
    assert store.names() == []
    with pytest.raises(PromptUnavailable):
        store.get("x")


# --- Layered ------------------------------------------------------------------


@pytest.fixture
def layers(tmp_path):
    registry = FlakyStore({"greet": remote()})
    local_dir = tmp_path / "prompts"
    local_dir.mkdir()
    (local_dir / "greet.json").write_text(json.dumps(remote(version=1, body="old {{ name }}").to_dict()))
    (local_dir / "only-local.md").write_text("shipped only")
    clock = {"t": 0.0}
    store = LayeredPromptStore(registry, LocalPromptStore(local_dir), cache_ttl=60, log_every=1000, clock=lambda: clock["t"])
    return registry, store, clock


def test_registry_first_then_cache_until_ttl(layers):
    registry, store, clock = layers
    assert store.get("greet").source == "langfuse"
    assert store.get("greet").source == "langfuse"
    assert registry.calls == 1  # cached
    clock["t"] = 61
    store.get("greet")
    assert registry.calls == 2  # refreshed after TTL


def test_registry_outage_serves_last_good_copy(layers, caplog):
    registry, store, clock = layers
    store.get("greet")
    registry.up = False
    clock["t"] = 61
    with caplog.at_level(logging.WARNING, logger="asas_llm.prompts"):
        p = store.get("greet")
    assert p.source == "cache" and p.version == 3 and p.content == remote().content
    assert "last-good copy" in caplog.text


def test_cold_start_without_registry_serves_local_file(layers):
    registry, store, _ = layers
    registry.up = False
    p = store.get("greet")
    assert p.source == "local" and p.version == 1 and p.content == "old {{ name }}"


def test_registry_recovers_after_local_fallback(layers):
    """A failed refresh IS cached for one TTL window (otherwise every request
    during an outage pays the full registry timeout — the fleet-wide latency
    the layered store exists to absorb). Recovery therefore happens on the
    first fetch after the window, not mid-window."""
    registry, store, clock = layers
    registry.up = False
    assert store.get("greet").source == "local"
    registry.up = True
    assert store.get("greet").source in ("local", "cache")  # still inside the window
    clock["t"] += store.cache_ttl + 1
    assert store.get("greet").source == "langfuse"  # first post-window fetch recovers


def test_outage_probes_once_per_window_not_per_request(layers):
    """The negative-cache regression pin: during an outage, the dead registry
    is probed once per TTL window; every other call inside the window is a
    cache hit that never touches the registry."""
    registry, store, clock = layers
    store.get("greet")                      # warm
    registry.up = False
    clock["t"] += store.cache_ttl + 1
    before = registry.calls
    store.get("greet")                      # pays one probe, caches the miss
    store.get("greet")
    store.get("greet")
    assert registry.calls == before + 1


def test_stale_ok_false_skips_cache_layer(tmp_path):
    registry = FlakyStore({"greet": remote()})
    local = tmp_path / "p"
    local.mkdir()
    (local / "greet.json").write_text(json.dumps(remote(version=1).to_dict()))
    clock = {"t": 0.0}
    store = LayeredPromptStore(registry, LocalPromptStore(local), cache_ttl=1, stale_ok=False, clock=lambda: clock["t"])
    store.get("greet")
    registry.up = False
    clock["t"] = 5
    assert store.get("greet").source == "local"


def test_every_layer_empty_names_them_all(layers):
    registry, store, _ = layers
    registry.up = False
    with pytest.raises(PromptUnavailable) as info:
        store.get("never-existed")
    msg = str(info.value)
    assert "registry" in msg and "last-good" in msg and "local" in msg


def test_unknown_prompt_with_registry_up_is_unavailable(layers):
    registry, store, _ = layers
    with pytest.raises(PromptUnavailable):
        store.get("never-existed")


def test_no_fallback_store_survives_outage_only_after_first_fetch():
    registry = FlakyStore({"greet": remote()})
    clock = {"t": 0.0}
    store = LayeredPromptStore(registry, None, cache_ttl=1, clock=lambda: clock["t"])
    registry.up = False
    with pytest.raises(PromptUnavailable) as info:
        store.get("greet")
    assert "no fallback store" in str(info.value)
    registry.up = True
    store.get("greet")
    registry.up = False
    clock["t"] = 5
    assert store.get("greet").source == "cache"


def test_warm_reports_source_per_prompt(layers):
    registry, store, _ = layers
    registry.up = False
    report = store.warm(["greet", "only-local", "missing"])
    assert report == {"greet": "local", "only-local": "local", "missing": "unavailable"}


def test_outage_is_logged_once_per_window(layers, caplog):
    registry, store, clock = layers
    store.get("greet")
    registry.up = False
    clock["t"] = 61
    with caplog.at_level(logging.WARNING, logger="asas_llm.prompts"):
        for _ in range(5):
            store.get("greet")
    assert caplog.text.count("registry unavailable") == 1


def test_names_fall_back_to_local(layers):
    registry, store, _ = layers
    assert store.names() == ["greet"]
    registry.up = False
    assert store.names() == ["greet", "only-local"]


def test_invalidate(layers):
    registry, store, _ = layers
    store.get("greet")
    store.invalidate("greet")
    store.get("greet")
    assert registry.calls == 2


def test_warm_survives_a_corrupt_local_file(tmp_path):
    """Boot is the wrong moment to crash: registry down + a corrupt shipped
    file maps to 'unavailable', never a raw PromptFormatError out of warm()."""
    registry = FlakyStore({})
    registry.up = False
    local = tmp_path / "p"
    local.mkdir()
    (local / "broken.json").write_text("{not json")
    store = LayeredPromptStore(registry, LocalPromptStore(local), cache_ttl=1)
    assert store.warm(["broken"]) == {"broken": "unavailable"}
