"""Asas MCP — remote MCP server core for FastAPI/Starlette hosts.

Extracted from Teamy (MCP epic TEAMY-357, design record 0014; extraction epic
TEAMY-466, design record 0017). Exposes a host to external AI clients (Claude
Code, ChatGPT, claude.ai, Cursor, …) over the Model Context Protocol:
streamable HTTP, stateless, JSON responses, mounted by the host as an exact
route (never a mount — MCP clients don't survive the trailing-slash redirect).

The package is protocol-only: it never imports host models, auth code, or
config. :func:`build_mcp_app` assembles the ASGI app + lifespan from injected
hooks — a tool lister (hand-written JSON Schemas pass through verbatim), a
thread-dispatched sync tool runner, optional prompt templates, and an optional
SDK ``TokenVerifier`` (None ⇒ open access, mirroring a host's enforce flag).
Tool annotations must stay honest (:class:`MCPToolDef`): clients build their
human-approval UX on them.
"""

from .server import MCPPromptArg, MCPPromptDef, MCPToolDef, build_mcp_app

__version__ = "0.11.1"

__all__ = ["MCPPromptArg", "MCPPromptDef", "MCPToolDef", "build_mcp_app", "__version__"]
