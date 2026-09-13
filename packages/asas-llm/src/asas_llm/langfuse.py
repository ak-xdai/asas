"""Langfuse as the prompt registry and the trace backend.

Optional: ``pip install 'asas-llm[langfuse]'``. Nothing here is imported by
``import asas_llm``; a host that uses another registry never loads the SDK.

Two adapters:

* :class:`LangfusePromptStore` wraps ``Langfuse.get_prompt`` as a
  :class:`~asas_llm.prompts.PromptStore`. Put it **under** a
  :class:`~asas_llm.prompts.LayeredPromptStore`, never bare: bare, it is the
  hard runtime dependency the roadmap describes.
* :class:`LangfuseTracer` wraps ``Langfuse.trace`` as a
  :class:`~asas_llm.tracing.Tracer`, including the LangChain callback handler
  that links generations to the prompt version they used.

Also the LangChain 1.x compatibility shim: the Langfuse 2.x SDK imports
LangChain through pre-1.0 module paths that 1.x removed. The classes still
exist in ``langchain_core``; :func:`install_langchain_compat` aliases the old
paths and is run before ``langfuse.callback`` is first touched. It is a no-op
when the old paths import fine (LangChain 0.x) or when LangChain is absent.
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any

from .errors import PromptUnavailable
from .prompts import Prompt
from .tracing import TraceHandle

log = logging.getLogger(__name__)

_compat_done = False


def install_langchain_compat() -> bool:
    """Alias the LangChain module paths Langfuse 2.x expects. Returns True when
    the aliases were (or already are) in place, False when LangChain is missing."""
    global _compat_done
    if _compat_done:
        return True
    try:
        import langchain.callbacks.base  # noqa: F401  (LangChain 0.x: nothing to do)

        _compat_done = True
        return True
    except ImportError:
        pass
    try:
        import langchain_core.agents as agents
        import langchain_core.callbacks.base as callbacks_base
        import langchain_core.documents as documents
        import langchain_core.load.serializable as serializable
    except ImportError:
        return False

    def alias(name: str, **attrs: Any) -> None:
        module = types.ModuleType(name)
        for attr, value in attrs.items():
            setattr(module, attr, value)
        sys.modules.setdefault(name, module)

    if "langchain" not in sys.modules:
        alias("langchain")
    alias("langchain.callbacks", base=callbacks_base)
    sys.modules.setdefault("langchain.callbacks.base", callbacks_base)
    alias("langchain.schema")
    alias("langchain.schema.agent", AgentAction=agents.AgentAction, AgentFinish=agents.AgentFinish)
    alias("langchain.schema.document", Document=documents.Document)
    alias("langchain.load", serializable=serializable)
    sys.modules.setdefault("langchain.load.serializable", serializable)
    _compat_done = True
    return True


def _require_langfuse():
    try:
        from langfuse import Langfuse
    except ImportError as exc:  # pragma: no cover - exercised by the contract test
        raise ModuleNotFoundError(
            "asas_llm.langfuse needs the 'langfuse' optional extra: pip install 'asas-llm[langfuse]'"
        ) from exc
    return Langfuse


def make_client(
    *,
    public_key: str,
    secret_key: str,
    host: str | None = None,
    release: str | None = None,
    enabled: bool = True,
    **kwargs: Any,
):
    """A ``Langfuse`` client from explicit values. The library reads no
    settings; the host passes its own."""
    Langfuse = _require_langfuse()
    return Langfuse(public_key=public_key, secret_key=secret_key, host=host, release=release, enabled=enabled, **kwargs)


def prompt_from_client(prompt_client: Any, *, source: str = "langfuse") -> Prompt:
    """Convert a Langfuse ``TextPromptClient``/``ChatPromptClient`` to a
    :class:`Prompt`, keeping the client as ``native`` for generation linking."""
    body = prompt_client.prompt
    if isinstance(body, str):
        ptype, content = "text", body
    else:
        ptype, content = "chat", tuple((m["role"], m["content"]) for m in body)
    return Prompt(
        name=prompt_client.name,
        type=ptype,  # type: ignore[arg-type]
        content=content,
        version=prompt_client.version,
        labels=tuple(prompt_client.labels or ()),
        config=prompt_client.config or {},
        source=source,
        native=prompt_client,
    )


class LangfusePromptStore:
    """``PromptStore`` over the Langfuse prompt registry.

    ``cache_ttl_seconds`` is passed to the SDK's own prompt cache; ``0`` turns
    it off so the :class:`~asas_llm.prompts.LayeredPromptStore` above is the
    single cache and its TTL is the one that means something.
    ``fetch_timeout_seconds`` bounds how long a cold fetch may block a request.
    """

    source = "langfuse"

    def __init__(
        self,
        client: Any,
        *,
        cache_ttl_seconds: int = 0,
        fetch_timeout_seconds: int | None = 5,
        max_retries: int | None = 1,
    ) -> None:
        self.client = client
        self.cache_ttl_seconds = cache_ttl_seconds
        self.fetch_timeout_seconds = fetch_timeout_seconds
        self.max_retries = max_retries

    def get(self, name: str, *, label: str | None = None, version: int | None = None) -> Prompt:
        kwargs: dict[str, Any] = {"cache_ttl_seconds": self.cache_ttl_seconds}
        if label is not None:
            kwargs["label"] = label
        if self.fetch_timeout_seconds is not None:
            kwargs["fetch_timeout_seconds"] = self.fetch_timeout_seconds
        if self.max_retries is not None:
            kwargs["max_retries"] = self.max_retries
        try:
            prompt_client = self.client.get_prompt(name, version, **kwargs)
        except Exception as exc:  # noqa: BLE001 - network, auth, 404: all "unavailable" here
            raise PromptUnavailable(f"Langfuse could not serve prompt {name!r}: {exc}", prompt=name) from exc
        if getattr(prompt_client, "is_fallback", False):
            # The SDK only builds a fallback client when asked to; we never ask.
            raise PromptUnavailable(f"Langfuse returned a fallback for prompt {name!r}", prompt=name)
        return prompt_from_client(prompt_client, source=self.source)

    def names(self) -> list[str]:
        found: list[str] = []
        page = 1
        while True:
            response = self.client.api.prompts.list(page=page, limit=100)
            found.extend(meta.name for meta in response.data)
            meta = getattr(response, "meta", None)
            if meta is None or page >= getattr(meta, "total_pages", 1):
                break
            page += 1
        return sorted(set(found))


class LangfuseTracer:
    """``Tracer`` over ``Langfuse.trace``. Flushing is the SDK's background
    thread by default; call ``flush()`` at the end of a short-lived worker task
    and ``shutdown()`` at process exit or events are lost."""

    def __init__(self, client: Any) -> None:
        self.client = client

    def start_trace(self, *, id, name, session_id, input=None, metadata=None) -> TraceHandle:
        native = self.client.trace(id=id, name=name, session_id=session_id, input=input, metadata=metadata)
        return TraceHandle(id=id, name=name, session_id=session_id, tracer=self, native=native)

    def langchain_handler(self, handle: TraceHandle, *, update_parent: bool) -> Any | None:
        if handle.native is None:
            return None
        if not install_langchain_compat():
            return None
        return handle.native.get_langchain_handler(update_parent=update_parent)

    def update_trace(self, handle: TraceHandle, *, output=None, metadata=None) -> None:
        if handle.native is None:
            return
        kwargs: dict[str, Any] = {}
        if output is not None:
            kwargs["output"] = output
        if metadata is not None:
            kwargs["metadata"] = metadata
        if kwargs:
            handle.native.update(**kwargs)

    def flush(self) -> None:
        self.client.flush()

    def shutdown(self) -> None:
        self.client.shutdown()
