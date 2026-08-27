"""MCP server assembly: low-level SDK server + stateless streamable HTTP + bearer auth.

Why the low-level ``Server`` and not ``FastMCP``: FastMCP derives tool input
schemas from Python function signatures, but our tools already carry
hand-written JSON Schemas (e.g. Teamy's chat ToolSpec registry) that must be exposed
verbatim — the low-level ``list_tools``/``call_tool`` handlers give us that
control. The transport/auth plumbing below mirrors what
``FastMCP.streamable_http_app`` assembles internally.

Stateless on purpose: no ``Mcp-Session-Id``, every POST is self-contained —
scale-out safe behind Render, and forward-compatible with the 2026-07-28 spec
revision that drops protocol state. ``json_response=True`` keeps responses
plain JSON (no SSE frames) — simpler for clients and tests alike.

Auth is optional by construction (``token_verifier=None`` ⇒ open access) so the
wiring can mirror ``AUTH_ENFORCE`` semantics — the same flag that opens the
rest of the API opens this surface.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, List, Optional

import json

import anyio
import mcp.types as types
from mcp.shared.exceptions import McpError
from pydantic import AnyHttpUrl
from mcp.server.auth.middleware.auth_context import (
    AuthContextMiddleware,
    get_access_token,
)
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import TokenVerifier
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.middleware.authentication import AuthenticationMiddleware


@dataclass
class MCPToolDef:
    """One tool as exposed over MCP. ``input_schema`` is a JSON Schema dict,
    passed through verbatim. Annotations must be honest per tool (TEAMY-371):
    clients build their human-approval UX on them — a write tool mislabeled
    read-only would skip the user's approval prompt. Defaults describe a read
    tool; write tools set ``read_only=False`` plus the hints that apply."""

    name: str
    description: str
    input_schema: dict
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True


@dataclass
class MCPPromptArg:
    name: str
    description: str
    required: bool = True


@dataclass
class MCPPromptDef:
    """One prompt template (TEAMY-359): ``render(arguments) -> str`` produces
    the user-role message text. Prompts are recipes over the read tools — they
    carry no data themselves, so they need no auth context to render."""

    name: str
    description: str
    arguments: List["MCPPromptArg"]
    render: Callable[[dict], str]


# The runner executes one tool call synchronously (DB session + permission
# context inside). A ``str`` return becomes plain text content; a ``dict``
# return becomes ``structuredContent`` *and* its JSON as text — the dual shape
# ChatGPT's search/fetch compatibility contract requires (TEAMY-360). ``token``
# is the raw bearer credential of the request, or None when auth is off.
ToolRunner = Callable[[Optional[str], str, dict], Any]
ToolLister = Callable[[], List[MCPToolDef]]


def build_mcp_app(
    *,
    name: str,
    instructions: str,
    list_tools: ToolLister,
    run_tool: ToolRunner,
    prompts: Optional[List[MCPPromptDef]] = None,
    token_verifier: Optional[TokenVerifier] = None,
    resource_metadata_url: Optional[str] = None,
):
    """Build the ``/mcp`` ASGI app and its lifespan.

    Returns ``(asgi_app, lifespan)`` — the caller mounts the app and runs the
    lifespan inside its own (the session manager's task group must be entered
    before the first request and exited on shutdown).
    """
    server = Server(name, instructions=instructions)

    @server.list_tools()
    async def _list_tools() -> List[types.Tool]:
        return [
            types.Tool(
                name=d.name,
                description=d.description,
                inputSchema=d.input_schema,
                annotations=types.ToolAnnotations(
                    readOnlyHint=d.read_only,
                    destructiveHint=d.destructive,
                    idempotentHint=d.idempotent,
                ),
            )
            for d in list_tools()
        ]

    @server.call_tool()
    async def _call_tool(tool_name: str, arguments: dict):
        # Read the request's credential off the auth contextvar here, in async
        # context — the worker thread below has no access to it.
        access = get_access_token()
        token = access.token if access is not None else None
        # Tools are sync (SQLModel session work); keep the event loop free.
        result = await anyio.to_thread.run_sync(run_tool, token, tool_name, arguments)
        if isinstance(result, dict):
            # SDK normalizes: structuredContent + the JSON serialized as text.
            return result
        if not isinstance(result, str):
            # ToolRunner is typed Any: a void write tool returning None (or a
            # list/number) must not surface a pydantic validation dump for
            # TextContent as the tool's isError text — normalize instead.
            result = "" if result is None else json.dumps(result, default=str)
        return [types.TextContent(type="text", text=result)]

    prompt_defs = {p.name: p for p in (prompts or [])}
    if prompt_defs:

        @server.list_prompts()
        async def _list_prompts() -> List[types.Prompt]:
            return [
                types.Prompt(
                    name=p.name,
                    description=p.description,
                    arguments=[
                        types.PromptArgument(
                            name=a.name, description=a.description, required=a.required
                        )
                        for a in p.arguments
                    ],
                )
                for p in prompt_defs.values()
            ]

        @server.get_prompt()
        async def _get_prompt(
            prompt_name: str, arguments: Optional[dict]
        ) -> types.GetPromptResult:
            prompt = prompt_defs.get(prompt_name)
            if prompt is None:
                # A bare ValueError lands in the SDK's generic handler as
                # JSON-RPC error code 0 — not a valid code at all; the spec
                # wants Invalid params (-32602) for an unknown prompt name.
                raise McpError(
                    types.ErrorData(
                        code=types.INVALID_PARAMS,
                        message=f"Unknown prompt '{prompt_name}'",
                    )
                )
            missing = [
                a.name for a in prompt.arguments if a.required and a.name not in (arguments or {})
            ]
            if missing:
                # prompts/list advertises these as required — accepting the
                # call anyway silently renders a different prompt.
                raise McpError(
                    types.ErrorData(
                        code=types.INVALID_PARAMS,
                        message=f"Missing required arguments: {', '.join(missing)}",
                    )
                )
            return types.GetPromptResult(
                description=prompt.description,
                messages=[
                    types.PromptMessage(
                        role="user",
                        content=types.TextContent(
                            type="text", text=prompt.render(arguments or {})
                        ),
                    )
                ],
            )

    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
    )

    class _Handler:
        """Class-based ASGI endpoint on purpose: Starlette's ``Route`` treats a
        plain function endpoint as a request-response handler; an instance is
        dispatched as a raw ASGI app (what the session manager needs)."""

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await manager.handle_request(scope, receive, send)

    asgi_app: Any = _Handler()
    if token_verifier is None:
        # Open access is a legitimate choice (a socket nobody else can reach,
        # a gateway doing auth in front) but it is never a safe *default* to
        # arrive at by omission: what gets mounted is a remote query API over
        # the host's data with no login. Say so at build time — a host that
        # meant it loses nothing by seeing the line in its boot log.
        logging.getLogger(__name__).warning(
            "asas-mcp: %r mounted with NO authentication (token_verifier=None). "
            "Anyone who can reach this endpoint can call every registered tool. "
            "Pass token_verifier= unless something in front of it authenticates.",
            name,
        )
    if token_verifier is not None:
        # Compose the SDK's auth stack by hand (outermost last): bearer
        # verification → auth contextvar → the 401 gate whose challenge
        # advertises the RFC 9728 protected-resource metadata (how
        # OAuth-capable clients find our AS).
        asgi_app = RequireAuthMiddleware(
            asgi_app,
            required_scopes=[],
            resource_metadata_url=(
                AnyHttpUrl(resource_metadata_url) if resource_metadata_url else None
            ),
        )
        asgi_app = AuthContextMiddleware(asgi_app)
        asgi_app = AuthenticationMiddleware(
            asgi_app, backend=BearerAuthBackend(token_verifier)
        )

    @asynccontextmanager
    async def lifespan() -> AsyncIterator[None]:
        async with manager.run():
            yield

    # A single composed ASGI app, NOT a Starlette sub-application: the caller
    # registers it as an exact Route("/mcp") — mounting would 307-redirect the
    # bare path to /mcp/, and redirect handling is where MCP clients break
    # (POST bodies and Authorization headers don't reliably survive).
    return asgi_app, lifespan
