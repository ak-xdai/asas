"""Which ROUTE a deployment takes to Fusion, and refusing a contradictory one.

A deployment reaches Fusion one of two ways, and they need different
credentials and different headers:

``direct``
    straight at the pod (``https://<pod>/hcmRestApi/resources/<version>``),
    authenticated with the Fusion account.

``gateway``
    through an API gateway in front of an integration tier
    (``https://<host>/gateway/<service>/<version>``), which needs the
    gateway's own key AND, on the gateways measured so far, the integration
    account as well: **the key alone answers 401**.

THE POINT OF THIS MODULE IS TO FAIL AT BOOT RATHER THAN AT THE FIRST READ.
An address and a route that contradict each other do not announce themselves:
a gateway address called with Fusion's credentials, or a direct address on a
deployment that is supposed to go through the gateway, answers 401 or 404 per
request, from a layer that looks like Oracle being down. `misroute` states the
contradiction in one line so a host can refuse to start, or light the reason
up on an admin page, instead of leaving somebody to read 401s.

The reasons are returned as a (code, message) pair: the CODE is for a host to
branch on and the message is English for a human. Neither names a secret, an
environment variable or a vault entry, because what those are called belongs
to the host and not to this package.
"""

from __future__ import annotations

from typing import Literal

import httpx

Route = Literal["direct", "gateway"]

#: The path segment an API gateway address carries. Fusion's own REST root is
#: ``/hcmRestApi/resources/<version>``, so the two are told apart by shape and
#: never by host name, which differs per client.
GATEWAY_MARKER = "/gateway/"


def is_gateway(base_url: str) -> bool:
    """Whether this address is an API gateway's rather than Fusion's own."""
    if not base_url:
        return False
    return GATEWAY_MARKER in httpx.URL(base_url).path


def misroute(
    route: Route,
    base_url: str,
    *,
    has_api_key: bool = True,
    has_account: bool = True,
) -> tuple[str, str]:
    """Why this address and these credentials contradict the route, or
    ``("", "")`` when they agree.

    An EMPTY ``base_url`` is not a contradiction: it means the integration is
    simply switched off, which is a different thing from being misconfigured
    and must not be reported as one.
    """
    if not base_url:
        return "", ""
    if route == "gateway":
        if not is_gateway(base_url):
            return (
                "base_url_is_direct",
                "the configured address points at Oracle Fusion directly; this "
                "deployment must go through the API gateway",
            )
        if not has_api_key:
            return "gateway_key_missing", "the API gateway's key is not set"
        if not has_account:
            # Measured: the gateway authenticates onward to the integration
            # tier, and answers 401 to the key alone.
            return (
                "gateway_account_missing",
                "the integration account the gateway authenticates onward with "
                "(username and password) is not set",
            )
        return "", ""
    if is_gateway(base_url):
        return (
            "base_url_is_gateway",
            "the configured address points at an API gateway; this deployment "
            "calls Oracle Fusion directly",
        )
    return "", ""
