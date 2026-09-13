"""What a host must tell the library about its Microsoft 365 tenant.

The library reads no configuration of its own: the host builds a
:class:`GraphSettings` from *its* settings object and hands it over. The one
convenience is :meth:`GraphSettings.from_env`, for hosts whose configuration
is plain environment variables — it is opt-in and it names every variable it
read (or failed to find), so a misconfigured deployment fails at boot with the
variable name in the message rather than at the first Graph call with a 401.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import GraphConfigError

DEFAULT_AUTHORITY_HOST = "https://login.microsoftonline.com"
DEFAULT_BASE_URL = "https://graph.microsoft.com/v1.0"
DEFAULT_SCOPES: tuple[str, ...] = ("https://graph.microsoft.com/.default",)


@dataclass(frozen=True)
class GraphSettings:
    """App-only (client-credentials) access to one tenant.

    ``authority_host`` and ``base_url`` are both settable because they move
    together for national clouds (Azure Government, China): a tenant on one
    of those signs in at a different authority *and* calls a different Graph
    endpoint. The defaults are the global cloud.
    """

    tenant_id: str
    client_id: str
    client_secret: str = field(repr=False)
    #: TLS/proxy knobs for the TOKEN plane (MSAL's own HTTP). The data plane
    #: takes a custom httpx client via GraphClient(http=...); these keep the
    #: private-CA / corporate-proxy story true for both planes.
    verify: Any | None = None      # bool or CA-bundle path, per requests
    proxies: dict[str, str] | None = None
    authority_host: str = DEFAULT_AUTHORITY_HOST
    base_url: str = DEFAULT_BASE_URL
    scopes: tuple[str, ...] = field(default=DEFAULT_SCOPES)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        missing = [
            name
            for name in ("tenant_id", "client_id", "client_secret")
            if not getattr(self, name)
        ]
        if missing:
            raise GraphConfigError(
                f"GraphSettings is missing {', '.join(missing)}; the Graph client "
                f"cannot sign in without them"
            )
        if not self.scopes:
            raise GraphConfigError("GraphSettings.scopes must name at least one scope")
        if self.timeout_seconds <= 0:
            raise GraphConfigError("GraphSettings.timeout_seconds must be > 0")
        # Normalise so path joins never produce a double slash or lose one.
        object.__setattr__(self, "authority_host", self.authority_host.rstrip("/"))
        object.__setattr__(self, "base_url", self.base_url.rstrip("/"))
        object.__setattr__(self, "scopes", tuple(self.scopes))

    @property
    def authority(self) -> str:
        """The MSAL authority URL: ``<authority_host>/<tenant_id>``."""
        return f"{self.authority_host}/{self.tenant_id}"

    @classmethod
    def from_env(
        cls, prefix: str = "GRAPH_", env: Mapping[str, str] | None = None
    ) -> GraphSettings:
        """Build from environment variables.

        Required: ``<prefix>TENANT_ID``, ``<prefix>CLIENT_ID``,
        ``<prefix>CLIENT_SECRET``. Optional: ``<prefix>AUTHORITY_HOST``,
        ``<prefix>BASE_URL``, ``<prefix>SCOPES`` (space-separated),
        ``<prefix>TIMEOUT_SECONDS``. Missing required variables are reported
        together, by name.
        """
        source = os.environ if env is None else env
        required = ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET")
        missing = [f"{prefix}{name}" for name in required if not source.get(f"{prefix}{name}")]
        if missing:
            raise GraphConfigError(
                f"missing environment variable(s) for Microsoft Graph: {', '.join(missing)}"
            )
        kwargs: dict[str, object] = {
            "tenant_id": source[f"{prefix}TENANT_ID"],
            "client_id": source[f"{prefix}CLIENT_ID"],
            "client_secret": source[f"{prefix}CLIENT_SECRET"],
        }
        if source.get(f"{prefix}AUTHORITY_HOST"):
            kwargs["authority_host"] = source[f"{prefix}AUTHORITY_HOST"]
        if source.get(f"{prefix}BASE_URL"):
            kwargs["base_url"] = source[f"{prefix}BASE_URL"]
        if source.get(f"{prefix}SCOPES"):
            kwargs["scopes"] = tuple(source[f"{prefix}SCOPES"].split())
        if source.get(f"{prefix}TIMEOUT_SECONDS"):
            try:
                kwargs["timeout_seconds"] = float(source[f"{prefix}TIMEOUT_SECONDS"])
            except ValueError as exc:
                raise GraphConfigError(
                    f"{prefix}TIMEOUT_SECONDS must be a number, got "
                    f"{source[f'{prefix}TIMEOUT_SECONDS']!r}"
                ) from exc
        return cls(**kwargs)  # type: ignore[arg-type]
