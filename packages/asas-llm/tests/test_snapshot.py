import json

from asas_llm.prompts import InMemoryPromptStore, LayeredPromptStore, LocalPromptStore, Prompt
from asas_llm.snapshot import snapshot


def test_snapshot_writes_and_local_store_reads_back(tmp_path):
    src = InMemoryPromptStore()
    src.text("greet", "Hello {{ name }}", model="gpt-x")
    src.chat("chatty", [("system", "s"), ("user", "{{input}}")])
    report = snapshot(src, tmp_path / "prompts")
    assert report.ok and set(report.written) == {"greet", "chatty"}

    local = LocalPromptStore(tmp_path / "prompts")
    assert local.names() == ["chatty", "greet"]
    greet = local.get("greet")
    assert greet.content == "Hello {{ name }}" and greet.config == {"model": "gpt-x"} and greet.source == "local"
    assert local.get("chatty").messages == (("system", "s"), ("user", "{{input}}"))


def test_snapshot_leaves_unchanged_files_alone(tmp_path):
    src = InMemoryPromptStore()
    src.text("greet", "same")
    first = snapshot(src, tmp_path)
    path = tmp_path / "greet.json"
    before = path.read_text()
    second = snapshot(src, tmp_path)
    assert first.written == {"greet": None} and second.unchanged == ["greet"] and second.written == {}
    assert path.read_text() == before  # not even the timestamp moved


def test_snapshot_rewrites_on_change_and_reports_failures(tmp_path):
    src = InMemoryPromptStore()
    src.text("greet", "v1")
    snapshot(src, tmp_path)
    src.text("greet", "v2")
    report = snapshot(src, tmp_path, ["greet", "missing"])
    assert report.written == {"greet": None}
    assert "missing" in report.failed and not report.ok
    assert json.loads((tmp_path / "greet.json").read_text())["prompt"] == "v2"


def test_snapshot_feeds_the_layered_fallback(tmp_path):
    """The full loop: registry -> snapshot -> local -> served during an outage."""
    registry = InMemoryPromptStore([Prompt(name="p", type="text", content="live", version=4)])
    snapshot(registry, tmp_path)

    class Down:
        def get(self, *a, **k):
            raise ConnectionError("down")

        def names(self):
            raise ConnectionError("down")

    layered = LayeredPromptStore(Down(), LocalPromptStore(tmp_path))
    served = layered.get("p")
    assert served.content == "live" and served.version == 4 and served.source == "local"
