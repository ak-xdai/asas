"""Asas LLM — the shared way to call a language model.

Three things every AI product rebuilds, done once:

* **Prompts as data.** A prompt lives in a registry (Langfuse) where it is
  edited without a deploy, and every read goes **registry, then last-good
  copy, then a local file shipped with the image**
  (:class:`~asas_llm.prompts.LayeredPromptStore`). A replica that starts while
  the registry is unreachable still answers; the registry is where prompts are
  *edited*, not what the process *needs to exist*. :mod:`asas_llm.snapshot`
  keeps the local copy current.
* **Every call traced, grouped per request, and the id handed back.** The host
  binds one session per inbound request (:func:`bind_request`, or the
  :class:`~asas_llm.middleware.TraceIdMiddleware` from a header); every trace
  joins that session; :func:`trace_ids` reads them back for a response header,
  a body field or a log line. The id exists before the tracing backend is
  asked, so it is on every result and every error whether or not the backend
  answered.
* **Structured output that does not come back broken.** Ask the provider for
  strict JSON matching the pydantic model, repair what comes back before
  parsing it, and when it still does not validate raise with the raw text
  attached (:mod:`asas_llm.structured`).

Host contract shape: **table-less, router-less** — the variant
``asas-storage`` and ``asas-ratelimit`` use. No ``migrate``, no ``seed``, no
``build_routers``. One hook, :func:`configure`, and a process-wide
:func:`runner` built lazily from it, mirroring ``asas_storage.configure`` /
``asas_storage.storage()``.

Optional extras, each imported only when used: ``[langfuse]`` for the registry
and tracer, ``[langchain]`` for the runner, ``[openai]`` for the two model
factories. ``import asas_llm`` alone needs pydantic and json-repair.

Extracted from an AI engine's working implementation (the runner, the strict
schema walker, the Langfuse prompt builder and the LangChain-1.x shim). The
two pieces the source did not have and this adds are the fallback layers and
the hand-back register.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .context import (
    RequestScope,
    active_trace_id,
    bind_request,
    clear_request,
    current_scope,
    current_session_id,
    last_trace_id,
    new_id,
    record_trace,
    request_scope,
    trace_ids,
)
from .errors import (
    EmptyOutput,
    GenerationFailed,
    LLMError,
    NotConfigured,
    PromptFormatError,
    PromptUnavailable,
    StructuredOutputError,
)
from .prompts import (
    InMemoryPromptStore,
    LayeredPromptStore,
    LocalPromptStore,
    Prompt,
    PromptStore,
)
from .structured import parse_structured, response_format, strict_json_schema, strip_code_fence
from .tracing import NullTracer, TraceHandle, Tracer, traced

if TYPE_CHECKING:  # pragma: no cover
    from .runners import LLMRunner, ModelFactory

__version__ = "0.1.0"

__all__ = [
    "EmptyOutput",
    "GenerationFailed",
    "InMemoryPromptStore",
    "LLMError",
    "LayeredPromptStore",
    "LocalPromptStore",
    "NotConfigured",
    "NullTracer",
    "Prompt",
    "PromptFormatError",
    "PromptStore",
    "PromptUnavailable",
    "RequestScope",
    "StructuredOutputError",
    "TraceHandle",
    "Tracer",
    "active_trace_id",
    "bind_request",
    "clear_request",
    "configure",
    "current_scope",
    "current_session_id",
    "last_trace_id",
    "new_id",
    "parse_structured",
    "record_trace",
    "request_scope",
    "response_format",
    "runner",
    "set_runner",
    "strict_json_schema",
    "strip_code_fence",
    "trace_ids",
    "traced",
    "__version__",
]

_settings: dict[str, Any] | None = None
_runner: "LLMRunner | None" = None


def configure(
    *,
    prompts: PromptStore,
    model_factory: "ModelFactory",
    default_model: str,
    tracer: Tracer | None = None,
    default_label: str | None = None,
    flush_after_trace: bool = False,
) -> None:
    """Install the host's pieces; :func:`runner` builds the ``LLMRunner`` from
    them on first use. Reconfiguring drops any built runner. The library reads
    no settings: the host builds the factory from its own configuration."""
    global _settings, _runner
    _settings = {
        "prompts": prompts,
        "model_factory": model_factory,
        "default_model": default_model,
        "tracer": tracer,
        "default_label": default_label,
        "flush_after_trace": flush_after_trace,
    }
    _runner = None


def runner() -> "LLMRunner":
    """The process-wide runner (lazy; see :func:`set_runner` for tests)."""
    global _runner
    if _runner is None:
        if _settings is None:
            raise NotConfigured(
                "asas_llm is not configured: call asas_llm.configure(...) at startup, "
                "or set_runner(instance) directly"
            )
        from .runners import LLMRunner

        _runner = LLMRunner(**_settings)
    return _runner


def set_runner(instance: "LLMRunner | None") -> None:
    """Override the runner (tests point this at one with a fake model);
    ``None`` resets to lazy re-selection from :func:`configure`."""
    global _runner
    _runner = instance
