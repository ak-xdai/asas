# asas-llm

**The shared way to call a language model.** Prompts are data, served
registry-first with a last-good copy and a shipped local file underneath, so a
replica that cannot reach the registry still answers. Every call is traced,
every trace is grouped under the request that made it, and the trace id is
handed back on the result, on the error and on the response header. Structured
output is asked for strictly, repaired before parsing, and fails with the raw
text attached rather than with a bare `None`.

```python
import asas_llm
from asas_llm.langfuse import LangfusePromptStore, LangfuseTracer, make_client
from asas_llm.middleware import TraceIdMiddleware
from asas_llm.runners import openai_compatible

# boot: the host builds every piece from its own settings
client = make_client(public_key=cfg.LF_PUBLIC, secret_key=cfg.LF_SECRET, host=cfg.LF_HOST, release=cfg.VERSION)
asas_llm.configure(
    prompts=asas_llm.LayeredPromptStore(
        LangfusePromptStore(client),                 # 1. the registry, where prompts are edited
        asas_llm.LocalPromptStore("app/prompts"),    # 3. the copy shipped in the image
    ),                                               # 2. (between them) the last copy fetched
    model_factory=openai_compatible(api_key=cfg.LLM_KEY, base_url=cfg.LLM_URL),
    default_model="gpt-5.1",
    tracer=LangfuseTracer(client),
)
app.add_middleware(TraceIdMiddleware)                # X-Correlation-Id in, X-Trace-Id out

# a call
result = await asas_llm.runner().ainvoke("parse-cv", {"cv_markdown": md}, output_model=ParsedCV)
result.value          # a ParsedCV
result.trace_id       # what the operator opens in Langfuse
result.prompt.source  # "langfuse" | "cache" | "local": which layer answered

# a host-owned agent or graph, traced the same way
with asas_llm.runner().trace("generate-jd-agent", input=payload) as (trace, config):
    raw = await agent.ainvoke(state, config=config)   # runner calls inside nest under this trace
    trace.set_output(raw["structured_response"])
```

Table-less, router-less variant of the Asas host contract, the shape
`asas-storage` uses: no `migrate`, no `seed`, no `build_routers`. One hook,
`configure`, and a lazily built process-wide `runner()`. It is the first
package of the **AI tier**: it owns no tables and calls a model instead.

## What each part is for

| Module | What it answers |
| --- | --- |
| `prompts` | Where does the prompt come from, and what happens when the registry is down? |
| `snapshot` | How does the local copy stay current? (`python -m asas_llm.snapshot`) |
| `context` | Which request is this work for, and which traces did it produce? |
| `tracing` | Open a trace, or join the enclosing one; the `Tracer` seam. |
| `middleware` | Bind the request from a header; hand the trace ids back in one. |
| `structured` | Strict schema in, repaired-and-validated object out. |
| `runners` | The LangChain runner and the two model factories. |
| `langfuse` | The Langfuse `PromptStore` and `Tracer`, plus the LangChain 1.x shim. |
| `errors` | Typed failures, each carrying its trace id. |

## Installing

```text
asas-llm[langfuse,langchain,openai] @ git+https://github.com/wlootah-a11y/asas.git@asas-llm/v0.1.0#subdirectory=packages/asas-llm
```

`import asas_llm` needs only pydantic and json-repair. The extras are imported
on use, and a module that needs one names it when it is missing:

| Extra | Pulls in | Needed for |
| --- | --- | --- |
| `langfuse` | `langfuse>=2.50,<3` | `asas_llm.langfuse` (registry, tracer, snapshot CLI) |
| `langchain` | `langchain-core>=0.3` | `asas_llm.runners` |
| `openai` | `langchain-openai>=0.3` | the `openai_compatible` / `azure_openai` factories |

## Six things this encodes that are easy to get wrong

1. **The registry is where prompts are edited, not what the process needs to
   exist.** `LayeredPromptStore` reads registry, then last-good copy, then
   local file, and raises only when all three have nothing. A failed refresh
   never evicts the cached copy. `Prompt.source` says which layer answered and
   the outage is logged once per window, not once per request.

2. **The trace id is minted before the backend is asked.** So it is on
   `LLMResult`, on every `LLMError`, and in the `X-Trace-Id` header whether or
   not Langfuse answered, and a host with no tracing service (`NullTracer`)
   gets the same shape. The source engine set the id into a context variable
   and then read it nowhere.

3. **Fan-out lands on the parent request.** `asyncio.gather` copies the
   context per task, but the list inside the scope is shared, so five parallel
   extraction calls record five ids on the request that will report them.

4. **A call inside an agent's tool joins the agent's trace.** Otherwise it is
   an orphan trace with no session, which is what the engine's runner had to
   special-case. `traced()` does it by default; `nest=False` opts out.

5. **Strict first, repair second, evidence third.** `response_format` makes
   the provider unable to emit a key not in the schema; `parse_structured`
   strips fences and repairs the JSON before validating; and when it still
   fails, `StructuredOutputError.raw` holds the text so nobody re-runs the
   call to see what came back.

6. **Failures raise.** The engine's runner caught everything and returned
   `None`; every caller then had to remember to check, and the ones that did
   not produced empty results with no error anywhere. Here a caller that wants
   "skip this area and continue" writes `except LLMError` and the log line has
   the trace id.

## Prompts

A `Prompt` is a name, a type (`text` or `chat`), a body, a version, labels and
config. Variables use `{{ name }}` everywhere, so a prompt snapshotted from
Langfuse and one written by hand are the same thing to the runner.

`config` is honoured by the runner: `model` and `temperature` in the prompt's
config win over the runner's defaults and lose to explicit call arguments. That
is what makes "change the model for one prompt" a registry edit.

### Stores

- `InMemoryPromptStore`: prompts declared in code (`store.text(...)`,
  `store.chat(...)`). Tests, and hosts without a registry.
- `LocalPromptStore(directory)`: `<name>.json` in the snapshot format, or
  `<name>.md` for a text prompt written by hand. Names are percent-encoded into
  file names. `label` and `version` are accepted and ignored: a fallback that
  refuses to serve because the label differs is not a fallback.
- `LayeredPromptStore(primary, fallback, cache_ttl=60, stale_ok=True)`: the
  three layers. `warm(names)` at boot returns `{name: source}` so the host can
  log what it is serving from where; `invalidate()` forces a refresh.
- `asas_llm.langfuse.LangfusePromptStore(client, fetch_timeout_seconds=5,
  max_retries=1)`: the registry. Bounded so a cold fetch cannot hang a request.
  Put it under a `LayeredPromptStore`; bare, it is the hard dependency again.

### Keeping the local copy current

```bash
LANGFUSE_PUBLIC_KEY=… LANGFUSE_SECRET_KEY=… LANGFUSE_HOST=… \
  python -m asas_llm.snapshot --dest app/prompts --label production
```

Writes one file per prompt, leaves unchanged files untouched, exits non-zero
if any prompt failed. Run it in the image build (so the fallback is the
version production was using when the image was cut) or as a scheduled job
that opens a PR with the diff. Either way "the fallback drifted" becomes
visible in the repository rather than in a replica's answers.

## Tracing

`bind_request(session_id)` starts a request scope; `TraceIdMiddleware` does it
from `X-Correlation-Id` (header names are parameters). Every `traced(...)`
inside the scope uses that session id and appends its trace id to the scope.
`trace_ids()` / `last_trace_id()` read them back; the middleware writes them
into `X-Trace-Id` on the way out and echoes the session id.

A host whose correlation id arrives in the body binds it in the route and puts
`trace_ids()` in the response model. The middleware still adds the header.

`Tracer` is a protocol with two implementations: `NullTracer` (records in
memory) and `asas_llm.langfuse.LangfuseTracer`. Langfuse 3.x is a different SDK
and gets its own adapter behind the same protocol when a host needs it.

Flushing: the Langfuse SDK batches in a background thread. Long-lived API
processes call `tracer.shutdown()` at exit; short-lived workers (Celery tasks)
set `flush_after_trace=True` or call `tracer.flush()` before returning, or the
last events are lost.

## Structured output

```python
from asas_llm import strict_json_schema, response_format, parse_structured

llm = llm.bind(response_format=response_format(ParsedCV))   # provider-side strict JSON
parsed = parse_structured(text, ParsedCV, trace_id=trace.id)  # fence-strip, repair, validate
```

`strict_json_schema` makes every property required, closes every object, drops
`None` defaults and unravels `$ref`s that carry siblings, which is the dialect
OpenAI-compatible providers enforce under `strict: true`. It is a port of the
walker the OpenAI SDK ships privately, so the package does not import `openai`
for a schema transform. Providers that reject `response_format` run with
`strict=False` and rely on repair plus validation.

## Adopting in the AI engine this was extracted from

What exists there today (`base/llm/`, `base/utils/pydantic.py`,
`base/definitions/enum/prompts.py`, `base/definitions/schema/llm/__init__.py`)
and where it goes:

| Engine today | Becomes | Note |
| --- | --- | --- |
| `LLMRunner.ainvoke` (`base/llm/runner.py`) | `asas_llm.runners.LLMRunner.ainvoke` | Same call shape; returns `LLMResult`, raises instead of `None`. The `LANGCHAIN` parser branch was `NotImplementedError` and is gone. |
| `LLMRunner.agent_ainvoke`, `graph_ainvoke` | host code + `runner.trace(name)` | `create_agent` and the LangGraph graph stay in the engine (they are product flow); the traced config and the nesting come from the package. |
| `LangfuseClient` singleton (`base/llm/store/langfuse.py`) | `LayeredPromptStore(LangfusePromptStore(client), LocalPromptStore(...))` | The hard dependency the roadmap names. `fetch_timeout_seconds` bounds what was an unbounded `to_thread` call. |
| `get_prompt` / `build_messages` (`base/llm/prompts/__init__.py`) | `to_chat_prompt_template(prompt, history_variable=, input_variable=)` | The engine's "Current user input: {input}" wording and `chat_history` placeholder are the `input_variable` / `history` arguments. |
| `to_strict_json_schema` (`base/utils/pydantic.py`) | `asas_llm.strict_json_schema` | Verbatim port minus the `openai` import. |
| `langfuse_compat.py` | `asas_llm.langfuse.install_langchain_compat()` | Applied lazily, only if the old paths fail to import. |
| `LangFuseCallbackHandler` (`base/llm/callbacks/`) | nothing | Unused in the engine; the trace handler comes from the trace. |
| `Prompts` enum, `LLMModel` enum (130 model names) | stay in the engine | Product vocabulary. The package takes prompt names and model names as strings; the enum's `create_openai_model` becomes one `openai_compatible(...)` / `azure_openai(...)` factory chosen from `LLM_PROVIDER`. |
| `EngineContextVariables.CORRELATION_ID` | `asas_llm.bind_request(request.correlation_id)` | Bound in `execute_executor`; `ExecuteExecutorResponse` gains a `trace_ids` field from `asas_llm.trace_ids()`. |
| `EngineContextVariables.LANGFUSE_TRACE_ID` | `asas_llm.last_trace_id()` | Was written on every call and never read. |
| `SuperAgent` and the tool collectors | stay in the engine | A product orchestration pattern over `agent_ainvoke`; it takes its traced config from `runner.trace`. |
| `EmbeddingClient` | stays, candidate for `asas_llm.embeddings` later | Different call shape; not needed by any current consumer of the runner. |
| `LLMRunner` as `SingletonMeta` | `asas_llm.configure` + `runner()` | Same process-wide access, but `set_runner()` lets tests inject a fake model. |

Order of adoption that keeps every step shippable: (1) snapshot the Langfuse
prompts into `base/data/prompts/` and configure the layered store, which
removes the outage failure mode before anything else changes; (2) switch
`ainvoke` callers to the package runner, replacing `if result is None` with
`except LLMError` where a skip is intended; (3) bind the request in the
executor route and add `trace_ids` to the response; (4) move `SuperAgent` to
`runner.trace`; (5) delete `base/llm/runner.py`, `store/`, `prompts/__init__.py`,
`callbacks/`, `langfuse_compat.py` and `utils/pydantic.py`.

## Developing

```bash
cd packages/asas-llm
pip install -e '.[dev]'
pytest -q
```

No database, no network, no model: the suite runs on LangChain's fake chat
model, an in-memory tracer, and a stub Langfuse client. One test starts a
fresh interpreter to prove `import asas_llm` loads no optional SDK.
