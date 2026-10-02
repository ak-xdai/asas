"""The error hierarchy. None of these carries an HTTP status for the HOST's
callers: how an Oracle failure is reported onward is host policy.

What they never carry either is Oracle's response BODY. It holds tenant detail,
and an attachment listing holds signed download URLs, so the text is logged at
debug level by the client and kept off the exception entirely. The status, the
verb and the path are enough to act on.
"""

from __future__ import annotations


class OracleError(Exception):
    """Base class for everything this package raises."""


class OracleConfigError(OracleError):
    """The host wired the settings wrong (raised at construction)."""


class OracleQueryError(OracleError, ValueError):
    """A value cannot be written into Oracle's ``q`` grammar safely.

    The grammar has no escape form: a single quote ends the literal and a
    ``;`` starts another clause, so a value holding either would change what
    the query means (an input that reads more rows than the caller asked for).
    Raised BEFORE any request, so nothing is sent."""


class OracleNotConfiguredError(OracleError):
    """No base URL, so the integration is off for this deployment.

    Raised on the first call rather than at construction, because "Oracle is
    not configured here" is a legitimate state for a host to boot in: a status
    page can ask ``client.configured`` and answer instantly."""


class OracleUpstreamError(OracleError):
    """Oracle refused, timed out, or answered something that is not JSON.

    ``status`` is Oracle's HTTP status, or ``None`` when no response arrived
    (a transport failure or a malformed body)."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str = "",
        path: str = "",
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path
        #: What the upstream asked a caller to wait (``Retry-After`` on a 429 or
        #: a 503), or ``None`` when it said nothing.
        self.retry_after_seconds = retry_after_seconds

    @property
    def is_transient(self) -> bool:
        """Worth retrying later: no answer at all, a throttle, or a 5xx."""
        return self.status is None or self.status == 429 or self.status >= 500


class OracleAuthError(OracleUpstreamError):
    """Oracle or the gateway refused the credentials (401) or the permission
    (403), or a token could not be obtained.

    Its own class because it is a CONFIGURATION fault, not an outage: retrying
    will not fix it, it never trips the circuit breaker, and it says to look at
    the key, the account or the role rather than at the network."""

    @property
    def is_transient(self) -> bool:
        return False


class OracleNotFoundError(OracleUpstreamError):
    """Oracle answered 404: a real answer about a real record, not a fault.
    A retry will not bring the record back."""


class OracleAlreadyExistsError(OracleUpstreamError):
    """Oracle refused a create because a key the CALLER minted is taken.

    Its own class because it is the one refusal that can mean success: when the
    caller chooses the key (a record number it mints itself) and retries a create,
    this answer says the first attempt landed. The caller reads the record back
    by that key. On a first attempt it means a genuine collision."""


class OracleUnavailableError(OracleUpstreamError):
    """The client's circuit breaker is open: this process saw Oracle fail
    several times in a row and is not asking again until the cooldown ends.

    Raised at once, before any request, so a caller learns of the outage in
    microseconds rather than after a timeout. A subclass of the upstream error,
    so a fail-soft caller already handles it; ``retry_after_seconds`` says when
    the breaker will let one probe through. Transient by definition."""

    def __init__(self, message: str, *, retry_after_seconds: int, method: str = "", path: str = "") -> None:
        super().__init__(
            message, status=None, method=method, path=path, retry_after_seconds=retry_after_seconds
        )
