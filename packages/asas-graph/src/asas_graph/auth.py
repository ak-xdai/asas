"""Where the bearer token comes from.

:class:`GraphClient` asks a :class:`TokenProvider` for a token before every
request and never caches one itself — caching is the provider's business, so a
host that already has a service-token source (the planned ``asas-upstream``
shared client, a sidecar, a test) plugs it in here and the Graph client is
unchanged.

The built-in provider is :class:`ClientCredentialTokenProvider`: MSAL's
confidential-client (app-only) flow. One MSAL application object lives for the
life of the provider so MSAL's in-memory token cache is honoured —
``acquire_token_silent`` serves the cached token until it nears expiry, and
only then does a network round-trip happen.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any, Protocol

from .errors import GraphAuthError
from .settings import GraphSettings


class TokenProvider(Protocol):
    """Anything that can produce a bearer token for Graph."""

    async def access_token(self) -> str: ...


class StaticTokenProvider:
    """A fixed token. For tests, and for hosts that obtain tokens elsewhere."""

    def __init__(self, token: str) -> None:
        if not token:
            raise GraphAuthError("StaticTokenProvider was given an empty token")
        self._token = token

    async def access_token(self) -> str:
        return self._token


class ClientCredentialTokenProvider:
    """MSAL client-credentials flow: sign in *as the application*.

    ``app_factory`` exists so tests can substitute a fake MSAL application
    without touching the network; hosts leave it alone.
    """

    def __init__(
        self,
        settings: GraphSettings,
        *,
        app_factory: Callable[[GraphSettings], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._app_factory = app_factory or _msal_app
        self._app: Any | None = None
        self._app_lock = threading.Lock()

    def _application(self) -> Any:
        # Guarded: access_token() runs in to_thread workers, so concurrent
        # first calls would each pay OIDC discovery + a client-credentials
        # round trip building N apps where one suffices.
        with self._app_lock:
            if self._app is None:
                self._app = self._app_factory(self._settings)
            return self._app

    def _acquire(self) -> dict[str, Any]:
        app = self._application()
        scopes = list(self._settings.scopes)
        result = app.acquire_token_silent(scopes, account=None)
        if not result:
            result = app.acquire_token_for_client(scopes=scopes)
        return result or {}

    async def access_token(self) -> str:
        # MSAL is synchronous and does its own HTTP; keep it off the event loop.
        try:
            result = await asyncio.to_thread(self._acquire)
        except Exception as exc:  # noqa: BLE001 - requests-level SSL/proxy/DNS
            # failures inside MSAL must honor the everything-derives-from-
            # GraphError contract instead of escaping as raw requests errors.
            raise GraphAuthError(
                f"token acquisition failed: {type(exc).__name__}: {exc}"
            ) from exc
        token = result.get("access_token")
        if not token:
            raise GraphAuthError(
                result.get("error_description") or result.get("error") or "no access_token returned",
                detail={k: result.get(k) for k in ("error", "correlation_id") if k in result},
            )
        return token


def _msal_app(settings: GraphSettings) -> Any:
    import msal  # imported here so a fake provider never needs the real library

    kwargs: dict[str, Any] = {}
    # The README's private-CA/proxy story must cover BOTH planes: the data
    # plane takes a custom httpx client, and without these the token plane
    # would still fail with a raw SSL error inside MSAL's own session.
    if settings.verify is not None:
        kwargs["verify"] = settings.verify
    if settings.proxies is not None:
        kwargs["proxies"] = settings.proxies
    return msal.ConfidentialClientApplication(
        client_id=settings.client_id,
        authority=settings.authority,
        client_credential=settings.client_secret,
        **kwargs,
    )
