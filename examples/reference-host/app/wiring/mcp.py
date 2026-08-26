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

import asas_mcp
from sqlmodel import Session, select

from ..db import engine
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
        annotations={"readOnlyHint": True, "idempotentHint": True},
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
        annotations={"readOnlyHint": True, "idempotentHint": True},
    ),
]


def _list_tools() -> list:
    return TOOLS


def _run_tool(token: Optional[str], name: str, arguments: dict) -> Any:
    """Dispatch.

    Note ``internal_note`` is absent from every projection below. The tool layer
    inherits the host's restrictions rather than restating them — but it must
    also not hand back a field the REST layer would have redacted, and the
    cheapest way to guarantee that is to never select it.
    """
    with Session(engine) as session:
        if name == "search_tickets":
            pattern = f"%{arguments['query']}%"
            rows = session.exec(
                select(Ticket).where(Ticket.title.ilike(pattern)).limit(20)
            ).all()
            return [
                {"id": t.id, "title": t.title, "status": t.status} for t in rows
            ]

        if name == "get_ticket":
            ticket = session.get(Ticket, arguments["ticket_id"])
            if ticket is None:
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
    )
