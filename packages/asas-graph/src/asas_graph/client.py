"""The HTTP layer: one authenticated ``httpx`` client against the Graph base URL.

Two things this gets right that first implementations do not:

1. **Trust store.** Python's default TLS context trusts OpenSSL's default CA
   bundle, which is *empty* on some machines (macOS dev boxes, minimal
   containers) and fails with "unable to get local issuer certificate".
   :func:`default_ssl_context` pins verification to ``certifi``'s bundle, so
   the same code works everywhere. Hosts that must use a corporate/private CA
   pass their own ``httpx.AsyncClient`` and the library uses it untouched.
2. **Empty bodies.** ``sendMail`` answers 202, ``PATCH``/``DELETE`` answer 204,
   none with a body. Every verb here returns ``None`` for an empty success
   rather than choking on JSON that is not there.

The client is a plain object: build one per settings, share it across the
process, close it on shutdown (``async with`` or :meth:`GraphClient.aclose`).
No singleton — a process that talks to two tenants makes two.
"""

from __future__ import annotations

import logging
import ssl
from functools import lru_cache
from typing import Any

import certifi
import httpx

from .auth import ClientCredentialTokenProvider, TokenProvider
from .errors import GraphError, GraphRequestError, GraphTransportError
from .settings import GraphSettings

log = logging.getLogger(__name__)

# Status codes whose success carries no body by definition.
_NO_CONTENT = frozenset({202, 204})


@lru_cache(maxsize=1)
def default_ssl_context() -> ssl.SSLContext:
    """A TLS context that verifies against certifi's CA bundle (built once)."""
    return ssl.create_default_context(cafile=certifi.where())


class GraphClient:
    """Authenticated verbs against Microsoft Graph, app-only.

    ``token_provider`` defaults to :class:`ClientCredentialTokenProvider` on
    the given settings. ``http`` lets the host inject its own
    ``httpx.AsyncClient`` (custom CA, proxies, a shared pool, a test
    transport); a client the host passes in is the host's to close.
    """

    def __init__(
        self,
        settings: GraphSettings,
        *,
        token_provider: TokenProvider | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens: TokenProvider = token_provider or ClientCredentialTokenProvider(settings)
        self._http = http
        self._owns_http = http is None

    @property
    def settings(self) -> GraphSettings:
        return self._settings

    # -- lifecycle -------------------------------------------------------------

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._settings.timeout_seconds,
                verify=default_ssl_context(),
            )
        return self._http

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this object created it.

        The closed client stays assigned: a straggler request after shutdown
        must fail loudly (httpx raises on a closed client) — setting the slot
        to ``None`` here would silently resurrect a fresh, never-closed pool
        on the next call and leak it for the process lifetime."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()

    async def __aenter__(self) -> GraphClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- verbs -----------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send one request. ``path`` is relative to ``settings.base_url``
        (``/users/{id}/events``). Returns the decoded JSON body, or ``None``
        when the success carried no body. Raises :class:`GraphRequestError`
        on any 4xx/5xx, :class:`GraphAuthError` if no token could be had."""
        token = await self._tokens.access_token()
        url = f"{self._settings.base_url}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        try:
            response = await self._client().request(
                method, url, json=json, params=params, headers=headers
            )
        except httpx.HTTPError as exc:
            # "Everything the library raises derives from GraphError" is the
            # documented contract — transport failures (timeouts, DNS, TLS,
            # pool exhaustion) must honor it too, and they are transient by
            # nature: hosts key retry loops on is_transient.
            raise GraphTransportError(path, method=method, cause=exc) from exc
        if response.status_code >= 400:
            raise _request_error(method, path, response)
        if response.status_code in _NO_CONTENT or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise GraphError(
                f"Graph returned a non-JSON body for {method} {path}",
                detail=response.text,
            ) from exc

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, payload: Any) -> Any:
        """POST; returns the body, or ``None`` for a 202/204 (e.g. ``sendMail``)."""
        return await self.request("POST", path, json=payload)

    async def patch(self, path: str, payload: Any) -> Any:
        """PATCH; Graph usually answers 204, so this usually returns ``None``."""
        return await self.request("PATCH", path, json=payload)

    async def delete(self, path: str) -> None:
        await self.request("DELETE", path)


def _request_error(method: str, path: str, response: httpx.Response) -> GraphRequestError:
    try:
        detail: Any = response.json() if response.content else None
    except ValueError:
        detail = response.text
    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
    log.warning("Graph %s %s failed (%s): %s", method, path, response.status_code, detail)
    return GraphRequestError(
        status=response.status_code,
        path=path,
        detail=detail,
        method=method,
        retry_after=retry_after,
    )


def _retry_after_seconds(raw: str | None) -> float | None:
    """``Retry-After`` on 429/503: Graph sends delta-seconds, but the header's
    other RFC 9110 form is an HTTP-date (front doors and proxies use it), and
    a host backoff loop keyed on this value must not read a legal header as
    "no delay". Non-finite values are rejected — a trusting host must never
    sleep forever on ``inf``."""
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        from email.utils import parsedate_to_datetime
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        from datetime import datetime, timezone
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    import math
    if not math.isfinite(value):
        return None
    return max(0.0, value)
