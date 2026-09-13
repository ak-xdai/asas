"""Binding the request scope from a header, and handing trace ids back in one.

Pure ASGI, so it needs only Starlette's scope/receive/send shapes (FastAPI
hosts have it; nothing is imported from Starlette). Add it **outermost** so the
scope exists before any dependency or route body runs::

    app.add_middleware(TraceIdMiddleware)   # X-Correlation-Id in, X-Trace-Id out

On every HTTP request it binds :func:`asas_llm.context.bind_request` with the
value of ``session_header`` (a fresh id when absent), and on the response it
adds ``trace_header`` with every trace id the request produced, comma
separated, oldest first, plus an echo of the session id. A caller that got a
wrong answer can quote the header and the operator opens the exact trace.

Hosts whose correlation id arrives in the body (the source engine's
``correlation_id`` field) call ``bind_request(body.correlation_id)`` in the
route instead and read :func:`asas_llm.context.trace_ids` for the response
field; the middleware still adds the header.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from . import context

Scope = dict[str, Any]
Receive = Callable[[], Awaitable[dict[str, Any]]]
Send = Callable[[dict[str, Any]], Awaitable[None]]


class TraceIdMiddleware:
    def __init__(
        self,
        app: Callable[[Scope, Receive, Send], Awaitable[None]],
        *,
        session_header: str = "X-Correlation-Id",
        trace_header: str = "X-Trace-Id",
        echo_session: bool = True,
    ) -> None:
        self.app = app
        self.session_header = session_header
        self.trace_header = trace_header
        self.echo_session = echo_session

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = _header(scope, self.session_header)
        request = context.bind_request(incoming)

        async def send_with_ids(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                ids = context.trace_ids()
                if ids:
                    headers.append((self.trace_header.lower().encode("latin-1"), ",".join(ids).encode("latin-1")))
                if self.echo_session:
                    headers.append((self.session_header.lower().encode("latin-1"), request.session_id.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_ids)
        finally:
            context.clear_request()


def _header(scope: Scope, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for key, value in scope.get("headers") or ():
        if key.lower() == wanted:
            text = value.decode("latin-1").strip()
            return text or None
    return None
