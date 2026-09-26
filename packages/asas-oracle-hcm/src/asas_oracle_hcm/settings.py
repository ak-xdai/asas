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
    ``gateway_api_key_header``. The Basic credentials still travel (a gateway
    typically forwards them), so the key is an addition, never a replacement.
    Unset sends no header at all.
    """

    base_url: str = ""
    username: str = ""
    password: str = ""
    timeout_seconds: float = 30.0
    gateway_api_key: str = ""
    gateway_api_key_header: str = "x-api-key"

    def __post_init__(self) -> None:
        object.__setattr__(self, "base_url", (self.base_url or "").strip().rstrip("/"))
        if self.base_url:
            if not self.base_url.startswith(("https://", "http://")):
                raise OracleConfigError("base_url must be an http(s) URL")
            if not (self.username and self.password):
                raise OracleConfigError(
                    "username and password are required when base_url is set"
                )
        if self.timeout_seconds <= 0:
            raise OracleConfigError("timeout_seconds must be positive")
        if self.gateway_api_key and not self.gateway_api_key_header.strip():
            raise OracleConfigError("gateway_api_key_header must name a header")

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @classmethod
    def from_env(cls, prefix: str = "ORACLE_HCM_") -> OracleSettings:
        """Opt-in convenience for hosts configured by environment variables:
        ``{prefix}BASE_URL``, ``USERNAME``, ``PASSWORD``, and the optional
        ``TIMEOUT_SECONDS``, ``GATEWAY_API_KEY``, ``GATEWAY_API_KEY_HEADER``."""

        def env(name: str, default: str = "") -> str:
            return os.environ.get(prefix + name, default)

        return cls(
            base_url=env("BASE_URL"),
            username=env("USERNAME"),
            password=env("PASSWORD"),
            timeout_seconds=float(env("TIMEOUT_SECONDS", "30")),
            gateway_api_key=env("GATEWAY_API_KEY"),
            gateway_api_key_header=env("GATEWAY_API_KEY_HEADER", "x-api-key"),
        )
