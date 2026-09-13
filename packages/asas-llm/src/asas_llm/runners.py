"""The shared way to call a model, over LangChain.

(Named ``runners`` and not ``runner`` on purpose: the package exports a
``runner()`` function, and a submodule of the same name would shadow it the
moment it was imported, the exact trap ``tests/test_host_contract.py`` pins.)

Optional: ``pip install 'asas-llm[langchain]'``. The rest of the package
(prompt stores, tracing, structured parsing, the middleware) does not need it.

:class:`LLMRunner` puts the three pieces together for the common case, one
prompt in and one typed object out:

    result = await runner.ainvoke("parse-cv", {"cv_markdown": md}, output_model=Sections)
    result.value        # a Sections instance
    result.trace_id     # the id to show the user or write to the log
    result.prompt.version

What it does that the call site should not have to:

* loads the prompt from the layered store off the event loop (the SDK is sync);
* opens a trace named after the prompt, in the request's session, or joins
  the enclosing trace when called from inside an agent's tool;
* attaches the tracer's LangChain handler and links the generation to the
  prompt version it used;
* asks the provider for strict JSON when ``output_model`` is given, and
  repairs-then-validates the answer;
* raises a typed :class:`~asas_llm.errors.LLMError` carrying the trace id
  instead of returning ``None``.

For agents and graphs the host keeps its own ``create_agent`` / LangGraph code
and takes a traced config from :meth:`LLMRunner.trace`; calls made inside it
nest automatically.

The model is a **string** and comes from a host-supplied factory: which
provider, which credentials and which endpoint are host settings this library
never reads. :func:`openai_compatible` and :func:`azure_openai` build the two
factories the source engine needed (an OpenAI-compatible gateway and Azure
OpenAI).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Generic, Iterator, Mapping, Protocol, TypeVar

from . import context
from .errors import EmptyOutput, GenerationFailed, LLMError, StructuredOutputError
from .prompts import Prompt, PromptStore
from .structured import parse_structured, response_format
from .tracing import NullTracer, TraceHandle, Tracer, traced

log = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class ModelOptions:
    """What the runner asks the factory for, beyond the model name.
    ``temperature=None`` means "do not send one" (the GPT-5 family rejects any
    value but the default)."""

    temperature: float | None = None
    streaming: bool = False
    name: str | None = None


class ModelFactory(Protocol):
    def __call__(self, model: str, options: ModelOptions) -> Any: ...


@dataclass
class LLMResult(Generic[T]):
    """What a call returns: the typed value (or the text when no
    ``output_model`` was given), the raw text, and the identifiers an operator
    needs to find the call again."""

    value: T
    text: str
    trace_id: str
    model: str
    prompt: Prompt | None = None
    name: str | None = None


def _require_langchain():
    try:
        from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder  # noqa: F401
    except ImportError as exc:
        raise ModuleNotFoundError(
            "asas_llm.runners needs the 'langchain' optional extra: pip install 'asas-llm[langchain]'"
        ) from exc


_ROLE_MAP = {"system": "system", "user": "human", "human": "human", "assistant": "ai", "ai": "ai", "developer": "system"}


def to_chat_prompt_template(
    prompt: Prompt,
    *,
    history_variable: str | None = None,
    input_variable: str | None = None,
    partial: Mapping[str, Any] | None = None,
):
    """A LangChain ``ChatPromptTemplate`` from a :class:`Prompt`.

    ``{{var}}`` becomes ``{var}``; literal braces in the body are escaped so a
    JSON example inside a prompt survives. ``history_variable`` appends a
    ``MessagesPlaceholder`` after the system message(s); ``input_variable``
    appends a human message ``{input_variable}`` unless the prompt already
    references it. When the prompt came from Langfuse the template carries the
    ``langfuse_prompt`` metadata the Langfuse callback uses to link the
    generation to the prompt version.
    """
    _require_langchain()
    from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

    variables = set(prompt.variables())
    messages: list[Any] = []
    for role, body in prompt.messages:
        # LangChain speaks mustache natively (template_format below), so the
        # body passes through UNMODIFIED: no brace escaping (JSON examples
        # survive as-is), and dotted variables ({{user.name}}) keep working —
        # the previous f-string re-writer turned them into str.format
        # attribute lookups that raised KeyError on every call.
        messages.append((_ROLE_MAP.get(role, role), body))
    if history_variable:
        # After the leading system block, before the first non-system message.
        idx = next((i for i, (r, _) in enumerate(prompt.messages) if r not in ("system", "developer")), len(messages))
        messages.insert(idx, MessagesPlaceholder(variable_name=history_variable, optional=True))
    if input_variable and input_variable not in variables:
        messages.append(("human", "{{" + input_variable + "}}"))
    template = ChatPromptTemplate.from_messages(messages, template_format="mustache")
    if partial:
        template = template.partial(**{k: v for k, v in partial.items() if k in template.input_variables})
    if prompt.native is not None and not getattr(prompt.native, "is_fallback", False):
        template.metadata = {"langfuse_prompt": prompt.native}
    return template


def message_text(output: Any) -> str:
    """The text of whatever LangChain returned: a message, a string, a dict."""
    if output is None:
        return ""
    content = getattr(output, "content", None)
    if content is not None:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                part.get("text", "") if isinstance(part, dict) else str(part) for part in content
            )
        return str(content)
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        for key in ("text", "output", "content"):
            if isinstance(output.get(key), str):
                return output[key]
    return str(output)


class LLMRunner:
    """See the module docstring. Not a singleton: build one at boot with the
    host's factory, store and tracer, and inject it (``asas_llm.configure`` +
    ``asas_llm.runner()`` do that for hosts that want a process-wide one)."""

    def __init__(
        self,
        *,
        model_factory: ModelFactory,
        prompts: PromptStore,
        tracer: Tracer | None = None,
        default_model: str,
        default_label: str | None = None,
        flush_after_trace: bool = False,
    ) -> None:
        self.model_factory = model_factory
        self.prompts = prompts
        self.tracer: Tracer = tracer or NullTracer()
        self.default_model = default_model
        self.default_label = default_label
        self.flush_after_trace = flush_after_trace

    # -- prompts --------------------------------------------------------------

    def get_prompt(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        # A pinned version is an exact address: the default label must NOT
        # ride along (Langfuse rejects label+version together, which would
        # turn every pinned read into a registry "failure" and silently serve
        # whatever the fallback holds instead of the pinned version).
        effective_label = label if label is not None else (self.default_label if version is None else None)
        return self.prompts.get(name, label=effective_label, version=version)

    async def aget_prompt(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        return await asyncio.to_thread(self.get_prompt, name, label=label, version=version)

    # -- tracing for host-owned agents/graphs --------------------------------

    @contextmanager
    def trace(
        self,
        name: str,
        *,
        input: Any = None,
        metadata: dict | None = None,
        update_parent: bool = True,
        nest: bool = True,
    ) -> Iterator[tuple[TraceHandle, dict]]:
        """A trace plus a LangChain ``RunnableConfig`` dict for it::

            with runner.trace("generate-jd-agent", input=payload) as (t, config):
                raw = await agent.ainvoke(state, config=config)
                t.set_output(raw["structured_response"])

        Runner calls made inside the block join this trace.
        """
        with traced(name, self.tracer, input=input, metadata=metadata, nest=nest, flush=self.flush_after_trace) as handle:
            handler = handle.langchain_handler(update_parent=update_parent)
            config: dict[str, Any] = {"run_name": name, "metadata": dict(metadata or {})}
            if handler is not None:
                config["callbacks"] = [handler]
            yield handle, config

    # -- the call -------------------------------------------------------------

    async def ainvoke(
        self,
        prompt: str | Prompt,
        inputs: Mapping[str, Any] | None = None,
        *,
        output_model: type[T] | None = None,
        model: str | None = None,
        name: str | None = None,
        temperature: float | None = None,
        label: str | None = None,
        version: int | None = None,
        history: list[Any] | None = None,
        history_variable: str = "chat_history",
        input_variable: str | None = "input",
        metadata: dict | None = None,
        strict: bool = True,
    ) -> LLMResult[Any]:
        """One prompt, one model call, one typed result.

        ``prompt`` is a name (looked up in the store) or a :class:`Prompt`.
        ``inputs`` fills its variables. ``history`` is a list of prior LangChain
        messages inserted at ``history_variable``. ``input_variable`` names the
        input key appended as the human turn when the prompt does not already
        use it; pass ``None`` for prompts that are complete on their own.
        ``strict=False`` skips the provider-side JSON schema (for providers
        that reject ``response_format``) and relies on repair + validation only.
        """
        _require_langchain()
        inputs = dict(inputs or {})
        resolved = prompt if isinstance(prompt, Prompt) else await self.aget_prompt(prompt, label=label, version=version)
        trace_name = name or resolved.name
        model_name = model or str(resolved.config.get("model") or self.default_model)
        if temperature is None and "temperature" in resolved.config:
            temperature = resolved.config["temperature"]

        with traced(trace_name, self.tracer, input=inputs, metadata=metadata, flush=self.flush_after_trace) as trace:
            handler = trace.langchain_handler()
            callbacks = [handler] if handler is not None else None
            llm = self.model_factory(model_name, ModelOptions(temperature=temperature, streaming=False, name=trace_name))
            if output_model is not None and strict:
                llm = llm.bind(response_format=response_format(output_model))  # type: ignore[arg-type]

            template = to_chat_prompt_template(
                resolved,
                history_variable=history_variable if history is not None else None,
                input_variable=input_variable if input_variable in inputs else None,
            )
            if history is not None:
                inputs.setdefault(history_variable, history)
            config: dict[str, Any] = {"run_name": trace_name, "metadata": dict(metadata or {})}
            if callbacks:
                config["callbacks"] = callbacks

            try:
                raw = await (template | llm).ainvoke(inputs, config=config)
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001 - provider/network: typed and re-raised
                raise GenerationFailed(f"{type(exc).__name__}: {exc}", trace_id=trace.id, prompt=resolved.name) from exc

            text = message_text(raw)
            if not text.strip():
                raise EmptyOutput("model returned no text", trace_id=trace.id, prompt=resolved.name)

            value: Any = text
            if output_model is not None:
                try:
                    value = parse_structured(text, output_model, trace_id=trace.id, prompt=resolved.name)
                except StructuredOutputError:
                    log.warning("(%s) structured output did not validate; trace_id=%s", trace_name, trace.id)
                    raise
                trace.set_output(value.model_dump() if hasattr(value, "model_dump") else value)
            else:
                trace.set_output(text)

            return LLMResult(value=value, text=text, trace_id=trace.id, model=model_name, prompt=resolved, name=trace_name)


# --- factories ----------------------------------------------------------------


def _common(options: ModelOptions) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"streaming": options.streaming}
    if options.name:
        kwargs["name"] = options.name
    if options.temperature is not None:
        kwargs["temperature"] = options.temperature
    return kwargs


def openai_compatible(*, api_key: str, base_url: str | None = None, **extra: Any) -> ModelFactory:
    """OpenAI, or any gateway that speaks its API (the source engine's Core42
    endpoint). ``extra`` is passed to every ``ChatOpenAI``."""

    def factory(model: str, options: ModelOptions):
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, **_common(options), **extra)

    return factory


def azure_openai(*, api_key: str, endpoint: str, api_version: str, **extra: Any) -> ModelFactory:
    """Azure OpenAI, where the model name is the deployment name."""

    def factory(model: str, options: ModelOptions):
        from langchain_openai import AzureChatOpenAI

        return AzureChatOpenAI(
            azure_deployment=model,
            api_key=api_key,
            azure_endpoint=endpoint,
            api_version=api_version,
            **_common(options),
            **extra,
        )

    return factory


def static_model(instance: Any) -> ModelFactory:
    """A factory that always returns ``instance`` (tests: a fake chat model)."""

    def factory(model: str, options: ModelOptions):
        return instance

    return factory


__all__ = [
    "LLMResult",
    "LLMRunner",
    "ModelFactory",
    "ModelOptions",
    "azure_openai",
    "message_text",
    "openai_compatible",
    "static_model",
    "to_chat_prompt_template",
]
