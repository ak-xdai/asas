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
    ) -> None:
        super().__init__(message)
        self.status = status
        self.method = method
        self.path = path

    @property
    def is_transient(self) -> bool:
        """Worth retrying later: no answer at all, a throttle, or a 5xx."""
        return self.status is None or self.status == 429 or self.status >= 500


class OracleNotFoundError(OracleUpstreamError):
    """Oracle answered 404: a real answer about a real record, not a fault.
    A retry will not bring the record back."""


class OracleAlreadyExistsError(OracleUpstreamError):
    """Oracle refused a create because a key the CALLER minted is taken.

    Its own class because it is the one refusal that can mean success: when the
    caller chooses the key (a requisition number, say) and retries a create,
    this answer says the first attempt landed. The caller reads the record back
    by that key. On a first attempt it means a genuine collision."""
