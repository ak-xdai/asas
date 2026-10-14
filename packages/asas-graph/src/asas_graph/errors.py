"""Typed errors. Everything the library raises derives from :class:`GraphError`.

Three layers, so a host can catch at the altitude it cares about:

- :class:`GraphConfigError` — the host wired the library wrong. Raised at
  construction time, never mid-request.
- :class:`GraphAuthError` / :class:`GraphRequestError` — the transport: a
  token could not be acquired, or Graph answered a request with 4xx/5xx.
  ``GraphRequestError`` carries the parsed Graph error envelope (``code``,
  ``message``, ``request_id``) and ``retry_after`` when Graph throttled.
- :class:`MeetingError` and its three subclasses — the Teams-meeting service
  failed at one specific operation. The transport error is chained as
  ``__cause__`` so nothing is lost by catching the higher one.

The library never maps these to HTTP status codes; how a *host* reports a
Graph failure to *its* callers is host policy.
"""

from __future__ import annotations

from typing import Any

# Status codes on which a retry has a reasonable chance of succeeding.
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})


class GraphError(Exception):
    """Base class for every error this package raises."""

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.detail = detail


class GraphConfigError(GraphError):
    """The host handed the library settings it cannot work with."""


class GraphAuthError(GraphError):
    """An access token could not be acquired for the application."""

    def __init__(self, reason: str, detail: Any = None) -> None:
        self.reason = reason
        super().__init__(f"Failed to acquire Microsoft Graph token: {reason}", detail=detail)


class GraphRequestError(GraphError):
    """Graph answered a request with a non-success status.

    ``detail`` is the decoded response body (or the raw text when it was not
    JSON). ``code``/``message``/``request_id`` are pulled out of Graph's
    standard ``{"error": {"code", "message", "innerError": {"request-id"}}}``
    envelope when present — ``request_id`` is what Microsoft support asks for.
    """

    def __init__(
        self,
        status: int,
        path: str,
        detail: Any = None,
        *,
        method: str = "",
        retry_after: float | None = None,
    ) -> None:
        self.status = status
        self.path = path
        self.method = method
        self.retry_after = retry_after
        envelope = detail.get("error") if isinstance(detail, dict) else None
        envelope = envelope if isinstance(envelope, dict) else {}
        self.code: str | None = envelope.get("code")
        self.message: str | None = envelope.get("message")
        inner = envelope.get("innerError")
        self.request_id: str | None = (
            inner.get("request-id") if isinstance(inner, dict) else None
        )
        summary = f"Graph returned {status} for {method + ' ' if method else ''}{path}"
        if self.code:
            summary += f" ({self.code})"
        super().__init__(summary, detail=detail)

    @property
    def is_throttled(self) -> bool:
        return self.status == 429

    @property
    def is_not_found(self) -> bool:
        return self.status == 404

    @property
    def is_transient(self) -> bool:
        """Worth retrying (after ``retry_after`` when set)."""
        return self.status in _TRANSIENT_STATUSES


class GraphTransportError(GraphRequestError):
    """The request never got a Graph answer: timeout, DNS, TLS, pool
    exhaustion. Status is 0 (there was no response), and it is transient by
    definition — the retry loop a host keys on ``is_transient`` applies."""

    def __init__(self, path: str, *, method: str = "", cause: Exception | None = None) -> None:
        super().__init__(0, path, detail=None, method=method)
        self.cause = cause
        reason = f"{type(cause).__name__}: {cause}" if cause is not None else "transport failure"
        self.args = (f"no response for {method + ' ' if method else ''}{path} ({reason})",)

    @property
    def is_transient(self) -> bool:
        return True


class MeetingError(GraphError):
    """A Teams-meeting operation failed. See the three subclasses."""

    operation = "change"

    def __init__(self, reason: str, detail: Any = None) -> None:
        self.reason = reason
        super().__init__(f"Failed to {self.operation} online meeting: {reason}", detail=detail)


class MeetingCreationError(MeetingError):
    operation = "create"


class MeetingUpdateError(MeetingError):
    operation = "update"


class MeetingCancelError(MeetingError):
    operation = "cancel"
