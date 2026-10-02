"""Connection settings: an explicit object the host builds from its own
configuration. The library reads none."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import OracleConfigError


@dataclass(frozen=True)
class OracleSettings:
    """Where the instance is and how to sign in to it.

    ``base_url`` is the full REST root, for example
    ``https://acme.fa.ocs.oraclecloud.com/hcmRestApi/resources/11.13.18.05``.
    An EMPTY base URL is allowed and means the integration is off: the client
    reports ``configured == False`` and raises :class:`OracleNotConfiguredError`
    on its first call, so a host can boot without Oracle.

    ``gateway_api_key`` is for a deployment that reaches Fusion through an API
    gateway rather than directly: when set, every request also carries it in
    ``gateway_api_key_header``; unset sends no header at all.

    **Basic auth travels only when a username is set.** Direct to Fusion the
    service account is required. Through a gateway that authenticates to the
    integration layer itself (an OAuth client behind the gateway, as with Abu
    Dhabi's AD Connect in front of Oracle Integration Cloud), the consumer sends
    the API key ALONE, and an empty pair must send no Authorization header
    rather than ``Basic Og==``. So ``base_url`` needs a username and password,
    OR a gateway key, or both.

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
    max_connections: int = 20
    connect_retries: int = 2

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", (self.base_url or "").strip().rstrip("/"))
        if self.base_url:
            if not self.base_url.startswith(("https://", "http://")):
                raise OracleConfigError("base_url must be an http(s) URL")
            if bool(self.username) != bool(self.password):
                raise OracleConfigError("username and password are set together or not at all")
            if not (self.username or self.gateway_api_key):
                raise OracleConfigError(
                    "base_url needs a username and password, a gateway_api_key, or both"
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
        """The Basic pair to send, or ``None`` (gateway key alone)."""
        return (self.username, self.password) if self.username else None

    @classmethod
    def from_env(cls, prefix: str = "ORACLE_HCM_") -> OracleSettings:
        """Opt-in convenience for hosts configured by environment variables:
        ``{prefix}BASE_URL``, ``USERNAME``, ``PASSWORD``, and the optional
        ``TIMEOUT_SECONDS``, ``GATEWAY_API_KEY``, ``GATEWAY_API_KEY_HEADER``,
        ``MAX_CONNECTIONS``, ``CONNECT_RETRIES``."""

        def env(name: str, default: str = "") -> str:
            return os.environ.get(prefix + name, default)

        return cls(
            base_url=env("BASE_URL"),
            username=env("USERNAME"),
            password=env("PASSWORD"),
            timeout_seconds=float(env("TIMEOUT_SECONDS", "30")),
            gateway_api_key=env("GATEWAY_API_KEY"),
            gateway_api_key_header=env("GATEWAY_API_KEY_HEADER", "x-api-key"),
            max_connections=int(env("MAX_CONNECTIONS", "20")),
            connect_retries=int(env("CONNECT_RETRIES", "2")),
        )
