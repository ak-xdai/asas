"""Which request the current work belongs to, and which traces it produced.

Two things live here, both ContextVars so they follow ``asyncio`` tasks and
threads started with a copied context:

* the **request scope**: one session id per inbound request (an API call, a
  Celery task, a meeting), used to group every trace that request produces so
  they can be read together afterwards. The host binds it once, from a header
  or a body field, and never passes it down by hand.
* the **hand-back register**: the ids of every trace started inside the scope,
  in order. A response header, a response body field, or a log line reads them
  back with :func:`trace_ids` / :func:`last_trace_id`. This is the part the
  source engine set into a context variable and then never read anywhere.

The list inside the scope is shared, not copied, when a task is spawned: a
fan-out (``asyncio.gather`` over five extraction calls) records all five ids on
the parent's scope, which is what the response has to report.

Also here: the **active trace**, so a call made from inside a tool body nests
under the agent's trace instead of starting a new one.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class RequestScope:
    session_id: str
    trace_ids: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


_scope: ContextVar[RequestScope | None] = ContextVar("asas_llm_request", default=None)
_active_trace: ContextVar[str | None] = ContextVar("asas_llm_active_trace", default=None)


def new_id() -> str:
    """A fresh id: 32 hex characters, accepted by Langfuse as a trace id."""
    return uuid.uuid4().hex


def bind_request(session_id: str | None = None, **metadata) -> RequestScope:
    """Start a request scope. A missing ``session_id`` is generated, so a caller
    that has no correlation id still gets its traces grouped. Returns the scope;
    the session id is ``scope.session_id``. Rebinding replaces the scope, so a
    long-lived worker thread that reuses a context does not leak the previous
    request's trace ids into the next one."""
    scope = RequestScope(session_id=session_id or new_id(), metadata=dict(metadata))
    _scope.set(scope)
    return scope


def clear_request() -> None:
    _scope.set(None)
    _active_trace.set(None)


def current_scope() -> RequestScope | None:
    return _scope.get()


def current_session_id() -> str | None:
    scope = _scope.get()
    return scope.session_id if scope else None


@contextmanager
def request_scope(session_id: str | None = None, **metadata) -> Iterator[RequestScope]:
    """``bind_request`` for a block; the previous scope is restored on exit
    rather than cleared, so a nested scope cannot wipe its parent's."""
    token = _scope.set(RequestScope(session_id=session_id or new_id(), metadata=dict(metadata)))
    try:
        yield _scope.get()  # type: ignore[misc]
    finally:
        _scope.reset(token)


def record_trace(trace_id: str) -> None:
    """Register a trace id on the current scope. Outside any scope this is a
    no-op: nothing is lost, the id is still on the result and the exception,
    there is just no request to hand it back to."""
    scope = _scope.get()
    if scope is not None and trace_id not in scope.trace_ids:
        scope.trace_ids.append(trace_id)


def trace_ids() -> tuple[str, ...]:
    """Every trace started in the current request scope, oldest first."""
    scope = _scope.get()
    return tuple(scope.trace_ids) if scope else ()


def last_trace_id() -> str | None:
    ids = trace_ids()
    return ids[-1] if ids else None


def active_trace_id() -> str | None:
    """The trace the current code is running inside, if any (see
    :func:`activate_trace`)."""
    return _active_trace.get()


@contextmanager
def activate_trace(trace_id: str) -> Iterator[None]:
    """Mark ``trace_id`` as the enclosing trace for the block. A runner call
    made inside the block joins it instead of opening its own."""
    token = _active_trace.set(trace_id)
    try:
        yield
    finally:
        _active_trace.reset(token)
