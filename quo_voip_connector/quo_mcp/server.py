"""
MCP (Model Context Protocol) server that exposes QUO VOIP tools to Claude.

Run with:
    python -m mcp.server

Or via the CLI:
    quo-voip mcp-server

Claude will be able to call these tools during a conversation to fetch
live transcription data from your QUO VOIP account.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Optional

# MCP SDK – install with: pip install mcp
try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp import types as mcp_types
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from quo_voip import QUOConfig
from quo_voip.mcp_tools import TOOLS, QUOMCPHandler

logger = logging.getLogger(__name__)


# ── MCP Server bootstrap ──────────────────────────────────────────────────────

def run_mcp_server(config: Optional[QUOConfig] = None) -> None:
    """Start the MCP stdio server."""
    if not MCP_AVAILABLE:
        print(
            "ERROR: The 'mcp' package is not installed.\n"
            "Install it with: pip install mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    config = config or QUOConfig.from_env()
    handler = QUOMCPHandler(config)
    app = Server("quo-voip-connector")

    @app.list_tools()
    async def list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOLS
        ]

    @app.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[mcp_types.TextContent]:
        result = handler.handle(name, arguments)
        return [mcp_types.TextContent(type="text", text=result)]

    import asyncio

    async def main():
        async with stdio_server() as streams:
            await app.run(streams[0], streams[1], app.create_initialization_options())

    asyncio.run(main())


# Allow: python -m quo_mcp.server
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    run_mcp_server()
