"""Connection settings: an explicit object the host builds from its own
configuration. The library reads none."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field

from .auth import ApiKeyAuth, Auth, BasicAuth, CompositeAuth, NoAuth, OAuthClientCredentials
from .errors import OracleConfigError


@dataclass(frozen=True)
class OracleSettings:
    """Where the instance is and how to sign in to it.

    ``base_url`` is the full REST root, for example
    ``https://acme.fa.ocs.oraclecloud.com/hcmRestApi/resources/11.13.18.05``.
    An EMPTY base URL is allowed and means the integration is off: the client
    reports ``configured == False`` and raises :class:`OracleNotConfiguredError`
    on its first call, so a host can boot without Oracle.

    **Credentials, any of three shapes, combinable** (:meth:`auth` builds them):

    * ``username`` + ``password``: Basic auth, for a direct connection to Fusion
      with an integration user. Sent only when a username is set.
    * ``gateway_api_key``: one header (``gateway_api_key_header``) for a
      deployment that reaches Fusion through an API gateway. A gateway that
      authenticates onward itself (to Oracle Integration Cloud, say) takes the
      key ALONE, with no Authorization header.
    * ``oauth_token_url`` + ``oauth_client_id`` + ``oauth_client_secret`` (and
      usually ``oauth_scope``): an OAuth 2.0 client-credentials token, for a
      direct connection to Oracle Integration Cloud or a gateway that wants a
      bearer token. Fetched once, reused, refreshed before it expires.

    A ``base_url`` needs at least one of them. Basic and OAuth both use the
    ``Authorization`` header, so they are refused together; a gateway key
    combines with either. ``extra_headers`` ride on every
    request (``REST-Framework-Version``, which changes how Fusion shapes some
    answers, or a header a gateway asks for).

    ``max_connections`` bounds the pool the client owns (the TLS handshake to a
    gateway is the dear part, so connections are kept warm), and
    ``connect_retries`` retries a connection that could not be OPENED: nothing
    reached Oracle, so it is safe even for a POST. Neither applies to an
    ``httpx.AsyncClient`` the host passes in.
    """

    base_url: str = ""
    username: str = ""
    password: str = ""
    timeout_seconds: float = 30.0
    gateway_api_key: str = ""
    gateway_api_key_header: str = "x-api-key"
    oauth_token_url: str = ""
    oauth_client_id: str = ""
    oauth_client_secret: str = ""
    oauth_scope: str = ""
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    max_connections: int = 20
    connect_retries: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", (self.base_url or "").strip().rstrip("/"))
        object.__setattr__(self, "extra_headers", dict(self.extra_headers or {}))
        oauth = (self.oauth_token_url, self.oauth_client_id, self.oauth_client_secret)
        if any(oauth) and not all(oauth):
            raise OracleConfigError(
                "oauth_token_url, oauth_client_id and oauth_client_secret are set together"
            )
        if self.oauth_token_url and not self.oauth_token_url.startswith("https://"):
            raise OracleConfigError("oauth_token_url must be https: it carries the client secret")
        if bool(self.username) != bool(self.password):
            raise OracleConfigError("username and password are set together or not at all")
        if self.username and self.oauth_token_url:
            raise OracleConfigError(
                "Basic and OAuth both use the Authorization header: configure one of them"
            )
        if self.base_url:
            if not self.base_url.startswith(("https://", "http://")):
                raise OracleConfigError("base_url must be an http(s) URL")
            if not (self.username or self.gateway_api_key or self.oauth_token_url):
                raise OracleConfigError(
                    "base_url needs credentials: a username and password, a gateway_api_key, "
                    "OAuth client credentials, or a combination"
                )
        if self.timeout_seconds <= 0:
            raise OracleConfigError("timeout_seconds must be positive")
        if self.gateway_api_key and not self.gateway_api_key_header.strip():
            raise OracleConfigError("gateway_api_key_header must name a header")
        if self.max_connections < 1:
            raise OracleConfigError("max_connections must be at least 1")
        if self.connect_retries < 0:
            raise OracleConfigError("connect_retries must be 0 or more")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def basic_auth(self) -> tuple[str, str] | None:
        """The Basic pair, or ``None`` when no user is configured."""
        return (self.username, self.password) if self.username else None

    def auth(self) -> Auth:
        """The credential these settings describe (see the class docstring)."""
        parts: list[Auth] = []
        if self.username:
            parts.append(BasicAuth(self.username, self.password))
        if self.gateway_api_key:
            parts.append(ApiKeyAuth(self.gateway_api_key, self.gateway_api_key_header))
        if self.oauth_token_url:
            parts.append(
                OAuthClientCredentials(
                    self.oauth_token_url,
                    self.oauth_client_id,
                    self.oauth_client_secret,
                    scope=self.oauth_scope,
                )
            )
        if not parts:
            return NoAuth()
        return parts[0] if len(parts) == 1 else CompositeAuth(parts)

    def describe_auth(self) -> str:
        """What is sent, never the values: for a status page or a check."""
        sent = []
        if self.username:
            sent.append("Basic")
        if self.gateway_api_key:
            sent.append(f"API key ({self.gateway_api_key_header})")
        if self.oauth_token_url:
            sent.append("OAuth client credentials")
        return " + ".join(sent) or "none"

    def __repr__(self) -> str:
        return f"OracleSettings(base_url={self.base_url!r}, auth={self.describe_auth()!r})"

    @classmethod
    def from_env(cls, prefix: str = "ORACLE_HCM_") -> OracleSettings:
        """Opt-in convenience for hosts configured by environment variables:
        ``{prefix}BASE_URL``, ``USERNAME``, ``PASSWORD``, ``GATEWAY_API_KEY``,
        ``GATEWAY_API_KEY_HEADER``, ``OAUTH_TOKEN_URL``, ``OAUTH_CLIENT_ID``,
        ``OAUTH_CLIENT_SECRET``, ``OAUTH_SCOPE``, ``TIMEOUT_SECONDS``,
        ``MAX_CONNECTIONS``, ``CONNECT_RETRIES``. Extra headers are not read
        from the environment; pass them in code."""

        def env(name: str, default: str = "") -> str:
            return os.environ.get(prefix + name, default)

        return cls(
            base_url=env("BASE_URL"),
            username=env("USERNAME"),
            password=env("PASSWORD"),
            timeout_seconds=float(env("TIMEOUT_SECONDS", "30")),
            gateway_api_key=env("GATEWAY_API_KEY"),
            gateway_api_key_header=env("GATEWAY_API_KEY_HEADER", "x-api-key"),
            oauth_token_url=env("OAUTH_TOKEN_URL"),
            oauth_client_id=env("OAUTH_CLIENT_ID"),
            oauth_client_secret=env("OAUTH_CLIENT_SECRET"),
            oauth_scope=env("OAUTH_SCOPE"),
            max_connections=int(env("MAX_CONNECTIONS", "20")),
            connect_retries=int(env("CONNECT_RETRIES", "2")),
        )
