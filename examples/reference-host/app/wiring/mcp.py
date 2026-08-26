"""asas-mcp.

Contract row: **Routers** (``build_mcp_app`` — an ASGI app, not an APIRouter).

Optional tier: without ``MCP_TOKEN`` there is nobody to authenticate, so the
endpoint is **absent** rather than open. An unauthenticated MCP endpoint is a
remote API over your database with no login.

The rule the tool surface follows, and the reason it is worth stating: **an MCP
tool is a thin allowlist over capability the host already has.** Permission
checks, redaction and filtering happen in the shared layer underneath, never
reimplemented at the protocol boundary. A tool that reaches around the host's
own service layer to query the database directly is how an MCP surface ends up
with different permissions from the REST API it is supposed to mirror.

Annotations must stay honest. The human gate for a write is the *client's*
approval prompt, so ``readOnlyHint`` on something that writes removes the only
confirmation step there is.
"""

from __future__ import annotations

from typing import Any, Optional

import asas_access
import asas_mcp
from mcp.server.auth.provider import AccessToken
from sqlmodel import Session, select

from ..config import settings
from ..models import Ticket

TOOLS = [
    asas_mcp.MCPToolDef(
        name="search_tickets",
        description="Find helpdesk tickets by a substring of their title.",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Text to look for."},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        # Flat kwargs, not an `annotations` dict — the package builds the
        # MCP annotations from these. Defaults already describe a read tool;
        # stated explicitly because an inaccurate hint here removes a client's
        # human-approval prompt, which is the only gate a write tool has.
        read_only=True,
        idempotent=True,
    ),
    asas_mcp.MCPToolDef(
        name="get_ticket",
        description="Read one helpdesk ticket by id.",
        input_schema={
            "type": "object",
            "properties": {"ticket_id": {"type": "integer"}},
            "required": ["ticket_id"],
            "additionalProperties": False,
        },
        # Flat kwargs, not an `annotations` dict — the package builds the
        # MCP annotations from these. Defaults already describe a read tool;
        # stated explicitly because an inaccurate hint here removes a client's
        # human-approval prompt, which is the only gate a write tool has.
        read_only=True,
        idempotent=True,
    ),
]


def _list_tools() -> list:
    return TOOLS


class _StaticTokenVerifier:
    """Bearer verification for the MCP endpoint.

    Without a verifier ``build_mcp_app`` mounts with **no authentication
    middleware at all** — the endpoint is then a remote query API over the
    database with no login. That is why the optional tier gates on `MCP_TOKEN`
    existing, and why the token has to actually be checked rather than merely
    present in the config.

    As crude as `fake_auth.py`, and for the same reason: a real host verifies
    against its own identity provider here.
    """

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        if not settings.mcp_token or token != settings.mcp_token:
            return None
        return AccessToken(
            token=token, client_id="asas-reference-helpdesk", scopes=["read"]
        )


def _caller(session: Session):
    """The subject an MCP request acts as.

    A single shared token names no person, so the caller is resolved as
    **anonymous** — holding no principals, and therefore reaching no classified
    ticket. That is the safe reading of an ambiguous identity, and it is the
    honest one: a host that wants per-user MCP access needs per-user tokens,
    not a shared secret plus an assumption.
    """
    return None


def _visible(session: Session, tickets) -> list:
    """Apply need-to-know before anything leaves the process.

    The tool layer is a thin allowlist over capability the host already has —
    which means it inherits the host's *checks*, not merely its data access. A
    tool that queries the table directly and skips `mac_allows` gives the MCP
    surface different permissions from the REST API it mirrors, which is the
    exact failure the thin-allowlist rule exists to prevent.
    """
    user = _caller(session)
    return [t for t in tickets if asas_access.mac_allows(session, user, "ticket", t)]


def _run_tool(token: Optional[str], name: str, arguments: dict) -> Any:
    """Dispatch.

    Note ``internal_note`` is absent from every projection below. The tool layer
    inherits the host's restrictions rather than restating them — but it must
    also not hand back a field the REST layer would have redacted, and the
    cheapest way to guarantee that is to never select it.
    """
    # Resolved per call rather than captured at import: the engine is a
    # module-level singleton the host may rebuild (tests do), and a stale
    # reference here would silently query a different database.
    from ..db import engine

    with Session(engine) as session:
        if name == "search_tickets":
            pattern = f"%{arguments['query']}%"
            rows = session.exec(
                select(Ticket).where(Ticket.title.ilike(pattern)).limit(20)
            ).all()
            return [
                {"id": t.id, "title": t.title, "status": t.status}
                for t in _visible(session, rows)
            ]

        if name == "get_ticket":
            ticket = session.get(Ticket, arguments["ticket_id"])
            # Same 404-not-403 reasoning as the REST route: an inaccessible
            # ticket is reported as absent, because confirming it exists is
            # itself the disclosure.
            if ticket is None or not _visible(session, [ticket]):
                return {"error": "not found"}
            return {
                "id": ticket.id,
                "title": ticket.title,
                "body": ticket.body,
                "status": ticket.status,
                "priority": ticket.priority_code,
            }

    return {"error": f"unknown tool {name!r}"}


def build_app():
    """The ASGI app to mount. Called only when the optional tier is on."""
    return asas_mcp.build_mcp_app(
        name="asas-reference-helpdesk",
        instructions=(
            "A demonstration helpdesk. Tools are read-only; there is no write "
            "surface in the reference host."
        ),
        list_tools=_list_tools,
        run_tool=_run_tool,
        token_verifier=_StaticTokenVerifier(),
    )
