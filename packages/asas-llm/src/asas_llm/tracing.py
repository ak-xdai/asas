"""Every call traced, every trace grouped by the request it served.

A :class:`Tracer` opens a trace and hands back a :class:`TraceHandle`: the id
(minted here, before the backend is even asked, so it exists whether or not
the backend does), the session it belongs to, and whatever the backend needs
to attach LangChain callbacks to it. The package ships :class:`NullTracer`,
which records handles in memory and talks to nothing; :mod:`asas_llm.langfuse`
ships the Langfuse one.

:func:`traced` is the one entry point the runner and any host code use:

    with traced("parse-cv", tracer, input=payload) as trace:
        ... run the model with trace.langchain_handler() ...
        trace.set_output(result)

It does three things a call site otherwise forgets: joins the enclosing trace
when there is one (a call made from inside a tool body nests under the agent's
trace instead of starting an orphan), registers the id on the request scope so
the response can hand it back, and flushes when asked.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable

from . import context

log = logging.getLogger(__name__)


@dataclass
class TraceHandle:
    """One trace. ``owned`` is False when the handle was borrowed from an
    enclosing trace, in which case the borrower must not set its output."""

    id: str
    name: str | None
    session_id: str | None
    tracer: "Tracer"
    owned: bool = True
    native: Any = field(default=None, repr=False)

    def langchain_handler(self, *, update_parent: bool = True) -> Any | None:
        """A LangChain callback handler bound to this trace, or ``None`` when the
        backend has none (``NullTracer``) or the handle is borrowed (the
        enclosing run's callbacks already propagate)."""
        if not self.owned:
            return None
        return self.tracer.langchain_handler(self, update_parent=update_parent)

    def set_output(self, output: Any) -> None:
        if self.owned:
            self.tracer.update_trace(self, output=output)

    def set_metadata(self, **metadata: Any) -> None:
        if self.owned:
            self.tracer.update_trace(self, metadata=metadata)


@runtime_checkable
class Tracer(Protocol):
    def start_trace(
        self,
        *,
        id: str,
        name: str | None,
        session_id: str | None,
        input: Any = None,
        metadata: dict | None = None,
    ) -> TraceHandle: ...

    def langchain_handler(self, handle: TraceHandle, *, update_parent: bool) -> Any | None: ...

    def update_trace(self, handle: TraceHandle, *, output: Any = None, metadata: dict | None = None) -> None: ...

    def flush(self) -> None: ...

    def shutdown(self) -> None: ...


class NullTracer:
    """No backend. Ids are still minted and handed back, so a host without a
    tracing service (or a test) gets the same result shape and the same
    ``X-Trace-Id`` header. Recent handles and updates are kept for tests to
    inspect — BOUNDED, because this is the production default for hosts
    without Langfuse: an unbounded list retaining every call's full input and
    output is a linear memory leak in any long-lived service."""

    _KEEP = 256  # most recent; tests inspect the tail, production forgets

    def __init__(self) -> None:
        self.traces: list[TraceHandle] = []
        self.updates: list[tuple[str, dict]] = []
        self.flushes = 0

    def _trim(self, items: list) -> None:
        if len(items) > self._KEEP:
            del items[: len(items) - self._KEEP]

    def start_trace(self, *, id, name, session_id, input=None, metadata=None) -> TraceHandle:
        handle = TraceHandle(id=id, name=name, session_id=session_id, tracer=self, native={"input": input, "metadata": metadata})
        self.traces.append(handle)
        self._trim(self.traces)
        return handle

    def langchain_handler(self, handle: TraceHandle, *, update_parent: bool) -> Any | None:
        return None

    def update_trace(self, handle: TraceHandle, *, output=None, metadata=None) -> None:
        self.updates.append((handle.id, {"output": output, "metadata": metadata}))
        self._trim(self.updates)

    def flush(self) -> None:
        self.flushes += 1

    def shutdown(self) -> None:
        pass


@contextmanager
def traced(
    name: str | None,
    tracer: Tracer,
    *,
    input: Any = None,
    metadata: dict | None = None,
    session_id: str | None = None,
    nest: bool = True,
    flush: bool = False,
) -> Iterator[TraceHandle]:
    """Open a trace for the block, or join the enclosing one.

    ``session_id`` defaults to the bound request's session, so every trace a
    request produces groups under it without the caller passing anything.
    ``nest=False`` forces a fresh trace even inside an active one (an agent
    that wants its sub-calls as separate traces).
    """
    enclosing = context.active_trace_id() if nest else None
    if enclosing is not None:
        yield TraceHandle(id=enclosing, name=name, session_id=session_id or context.current_session_id(), tracer=tracer, owned=False)
        return

    trace_id = context.new_id()
    session = session_id or context.current_session_id()
    merged = dict(context.current_scope().metadata) if context.current_scope() else {}
    if metadata:
        merged.update(metadata)
    handle = tracer.start_trace(id=trace_id, name=name, session_id=session, input=input, metadata=merged or None)
    context.record_trace(handle.id)
    try:
        with context.activate_trace(handle.id):
            yield handle
    finally:
        if flush:
            try:
                tracer.flush()
            except Exception:  # noqa: BLE001 - a flush failure must not mask the call's result
                log.exception("trace flush failed for %s", handle.id)
