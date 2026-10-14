"""How a request proves who it is. One small protocol, several shapes.

A Fusion deployment is reached in more than one way, and each wants different
credentials:

* **Direct to Fusion**: Basic auth with an integration user
  (:class:`BasicAuth`).
* **Through an API gateway that authenticates onward itself**: an API key in
  a header and nothing else (:class:`ApiKeyAuth`).
* **Direct to Oracle Integration Cloud, or a gateway that wants a token**: an
  OAuth 2.0 client-credentials token from the identity domain
  (:class:`OAuthClientCredentials`), fetched once and reused until shortly
  before it expires.

They compose (:class:`CompositeAuth`): a gateway key plus a token, say.
:meth:`OracleSettings.auth` builds the right one from the settings, and a host
with anything else passes its own :class:`Auth` to the client.

A credential never appears in a log line or an exception message.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Callable, Sequence
from typing import Protocol

import httpx

from .errors import OracleAuthError


class Auth(Protocol):
    """What the client asks of a credential."""

    async def headers(self, http: httpx.AsyncClient) -> dict[str, str]:
        """The headers that authenticate one request."""
        ...

    def invalidate(self) -> bool:
        """The upstream refused the credential (401). Return True when asking
        again may succeed (a token was dropped and will be fetched anew), so
        the client retries the request once; False for a fixed credential."""
        ...


class BasicAuth:
    """``Authorization: Basic ...`` for an integration user."""

    def __init__(self, username: str, password: str) -> None:
        raw = f"{username}:{password}".encode()
        self._value = "Basic " + base64.b64encode(raw).decode()

    async def headers(self, http: httpx.AsyncClient) -> dict[str, str]:
        return {"Authorization": self._value}

    def invalidate(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "BasicAuth(<redacted>)"


class ApiKeyAuth:
    """One header carrying an API key (a gateway's ``x-api-key``, say)."""

    def __init__(self, key: str, header: str = "x-api-key") -> None:
        if not header.strip():
            raise ValueError("the API key header must be named")
        self._header, self._key = header, key

    async def headers(self, http: httpx.AsyncClient) -> dict[str, str]:
        return {self._header: self._key}

    def invalidate(self) -> bool:
        return False

    def __repr__(self) -> str:
        return f"ApiKeyAuth(header={self._header!r}, key=<redacted>)"


class BearerToken:
    """``Authorization: Bearer ...`` from a token the host already holds, or
    from a callable the host owns (its own token cache, a managed identity).
    A callable is asked again after a 401."""

    def __init__(self, token: str | Callable[[], str]) -> None:
        self._token = token

    async def headers(self, http: httpx.AsyncClient) -> dict[str, str]:
        value = self._token() if callable(self._token) else self._token
        return {"Authorization": f"Bearer {value}"}

    def invalidate(self) -> bool:
        return callable(self._token)

    def __repr__(self) -> str:
        return "BearerToken(<redacted>)"


class OAuthClientCredentials:
    """An OAuth 2.0 client-credentials token, cached and refreshed.

    The token is fetched on first use, reused until ``refresh_margin_seconds``
    before it expires, and fetched again after a 401 (the client then retries
    the refused request once). Concurrent requests share one fetch. The client
    id and secret travel as HTTP Basic to the token endpoint, the form an
    Oracle identity domain expects (``credentials_in_body=True`` sends them as
    form fields instead, for providers that want that).

    ``token_url`` is the identity domain's token endpoint, for example
    ``https://idcs-<id>.identity.oraclecloud.com/oauth2/v1/token``; ``scope``
    is the integration's resource scope (an OIC instance names one, such as
    ``https://<host>:443urn:opc:resource:consumer::all``)."""

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        scope: str = "",
        refresh_margin_seconds: float = 60.0,
        credentials_in_body: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not (token_url and client_id and client_secret):
            raise ValueError("token_url, client_id and client_secret are all required")
        self._url = token_url
        self._client_id = client_id
        self._secret = client_secret
        self._scope = scope
        self._margin = refresh_margin_seconds
        self._in_body = credentials_in_body
        self._clock = clock
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock: asyncio.Lock | None = None
        self.fetches = 0

    def _fresh(self) -> bool:
        return self._token is not None and self._clock() < self._expires_at - self._margin

    async def _fetch(self, http: httpx.AsyncClient) -> None:
        form = {"grant_type": "client_credentials"}
        if self._scope:
            form["scope"] = self._scope
        headers = {"Accept": "application/json"}
        if self._in_body:
            form.update(client_id=self._client_id, client_secret=self._secret)
        else:
            headers.update(await BasicAuth(self._client_id, self._secret).headers(http))
        try:
            response = await http.post(self._url, data=form, headers=headers)
        except httpx.HTTPError as exc:
            raise OracleAuthError("The token endpoint is unreachable.", path="(token)") from exc
        if response.status_code >= 400:
            raise OracleAuthError(
                f"The token endpoint answered {response.status_code}.",
                status=response.status_code,
                path="(token)",
            )
        try:
            body = response.json()
            token = str(body["access_token"])
            lifetime = float(body.get("expires_in", 3600))
        except (ValueError, KeyError, TypeError) as exc:
            raise OracleAuthError("The token endpoint answered without a token.", path="(token)") from exc
        self.fetches += 1
        self._token, self._expires_at = token, self._clock() + lifetime

    async def headers(self, http: httpx.AsyncClient) -> dict[str, str]:
        if not self._fresh():
            if self._lock is None:
                self._lock = asyncio.Lock()
            async with self._lock:
                if not self._fresh():
                    await self._fetch(http)
        return {"Authorization": f"Bearer {self._token}"}

    def invalidate(self) -> bool:
        self._token, self._expires_at = None, 0.0
        return True

    def __repr__(self) -> str:
        return f"OAuthClientCredentials(token_url={self._url!r}, client_id={self._client_id!r}, secret=<redacted>)"


class CompositeAuth:
    """Several credentials on one request (a gateway key AND a token)."""

    def __init__(self, parts: Sequence[Auth]) -> None:
        self._parts = list(parts)

    async def headers(self, http: httpx.AsyncClient) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self._parts:
            out.update(await part.headers(http))
        return out

    def invalidate(self) -> bool:
        return any([part.invalidate() for part in self._parts])

    def __repr__(self) -> str:
        return f"CompositeAuth({self._parts!r})"


class NoAuth:
    """No credential at all (a host that authenticates in its own transport)."""

    async def headers(self, http: httpx.AsyncClient) -> dict[str, str]:
        return {}

    def invalidate(self) -> bool:
        return False
