"""
Remote MCP server for QUO VOIP Connector.

Wraps the MCP tools in an HTTP + SSE server so Claude on ANY laptop
can connect to a single hosted instance — no local installation needed.

Start locally:
    uvicorn server.remote_mcp:app --host 0.0.0.0 --port 8000

Via Docker / Render:
    docker compose up
    # or just push to Render — it reads the Dockerfile automatically

Environment variables required:
    QUO_API_KEY        — your QUO VOIP API key
    QUO_SERVER_TOKEN   — a secret token you choose; every laptop needs this

Claude Code config on each laptop  (~/.claude.json  or  .mcp.json):
    {
      "mcpServers": {
        "quo-voip": {
          "type": "sse",
          "url": "https://your-app.onrender.com/sse",
          "headers": { "Authorization": "Bearer YOUR_QUO_SERVER_TOKEN" }
        }
      }
    }
"""

from __future__ import annotations

import logging
import os
import secrets
import sys
from typing import Any

# ── FastAPI / Starlette ───────────────────────────────────────────────────────
try:
    from fastapi import FastAPI, HTTPException, Query, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
except ImportError as exc:
    sys.exit(
        "FastAPI not found. Install server dependencies:\n"
        "  pip install fastapi uvicorn[standard]\n"
        f"  ({exc})"
    )

# ── MCP SDK ───────────────────────────────────────────────────────────────────
try:
    from mcp import types as mcp_types
    from mcp.server import Server
    from mcp.server.sse import SseServerTransport
except ImportError as exc:
    sys.exit(
        "MCP SDK not found. Install with:\n"
        "  pip install 'mcp>=1.0.0'\n"
        f"  ({exc})"
    )

# ── QUO VOIP ──────────────────────────────────────────────────────────────────
from quo_voip import QUOConfig
from quo_voip.mcp_tools import TOOLS, QUOMCPHandler

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

# Server auth token — set QUO_SERVER_TOKEN in your Render env vars.
# If not set we generate one on startup (lost on redeploy, fine for testing).
_SERVER_TOKEN: str = os.environ.get("QUO_SERVER_TOKEN", "")
if not _SERVER_TOKEN:
    _SERVER_TOKEN = secrets.token_urlsafe(32)
    logger.warning(
        "QUO_SERVER_TOKEN is not set — using a temporary token for this session.\n"
        "  Token: %s\n"
        "  Set QUO_SERVER_TOKEN in your Render environment variables to make it permanent.",
        _SERVER_TOKEN,
    )

# Render injects PORT; fall back to 8000 for local dev.
PORT: int = int(os.environ.get("PORT", 8000))

# ─────────────────────────────────────────────────────────────────────────────
# MCP server instance
# ─────────────────────────────────────────────────────────────────────────────

_quo_config = QUOConfig.from_env()
_handler = QUOMCPHandler(_quo_config)

mcp_server = Server("quo-voip-connector")


@mcp_server.list_tools()
async def _list_tools() -> list[mcp_types.Tool]:
    return [
        mcp_types.Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in TOOLS
    ]


@mcp_server.call_tool()
async def _call_tool(
    name: str, arguments: dict[str, Any]
) -> list[mcp_types.TextContent]:
    result = _handler.handle(name, arguments)
    return [mcp_types.TextContent(type="text", text=result)]


# ─────────────────────────────────────────────────────────────────────────────
# SSE transport
# ─────────────────────────────────────────────────────────────────────────────

sse = SseServerTransport("/messages/")


def _require_bearer(request: Request) -> None:
    """Raise 401/403 if the request carries an invalid Bearer token."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authorization: Bearer <token> required")
    token = auth[len("Bearer "):]
    if not secrets.compare_digest(token.encode(), _SERVER_TOKEN.encode()):
        raise HTTPException(status_code=403, detail="Invalid token")


async def _handle_sse(request: Request) -> None:
    """
    Claude connects here first.  The server sends back an event-stream
    containing the session URL that Claude will POST tool calls to.
    """
    _require_bearer(request)
    logger.info("SSE connection from %s", request.client)
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp_server.run(
            streams[0],
            streams[1],
            mcp_server.create_initialization_options(),
        )


# ─────────────────────────────────────────────────────────────────────────────
# FastAPI application
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="QUO VOIP MCP Server",
    description=(
        "Remote MCP server for QUO VOIP. "
        "Connect Claude on any laptop by pointing it at /sse with a Bearer token."
    ),
    version="1.0.0",
    docs_url=None,   # disable Swagger UI in production
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    """Render and uptime monitors hit this endpoint."""
    return JSONResponse({"status": "ok", "service": "quo-voip-mcp"})


@app.get("/call-volume")
async def call_volume(
    request: Request,
    date: str = Query(default=None, description="Date in YYYY-MM-DD format (default: today)"),
) -> JSONResponse:
    """
    Return the call volume for a given date.

    Usage:
        curl https://<host>/call-volume?date=2026-03-23 \\
             -H "Authorization: Bearer <QUO_SERVER_TOKEN>"
    """
    _require_bearer(request)
    from datetime import date as _date, datetime
    target = date or str(_date.today())
    try:
        participant = os.environ.get("OPENPHONE_PARTICIPANT", "")
        if not participant:
            raise HTTPException(status_code=500, detail="OPENPHONE_PARTICIPANT env var is required")
        start = f"{target}T00:00:00"
        end   = f"{target}T23:59:59"
        result = _handler.svc.list_calls(
            participants=[participant],
            created_after=datetime.fromisoformat(start),
            created_before=datetime.fromisoformat(end),
            max_results=1,
        )
        return JSONResponse({"date": target, "call_volume": result.total_items})
    except Exception as exc:
        logger.error("call_volume error: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/sse")
async def sse_endpoint(request: Request) -> None:
    """
    MCP over SSE.  Claude connects here; auth via Bearer token.

    Claude Code config:
        {
          "mcpServers": {
            "quo-voip": {
              "type": "sse",
              "url": "https://<your-app>.onrender.com/sse",
              "headers": { "Authorization": "Bearer <QUO_SERVER_TOKEN>" }
            }
          }
        }
    """
    await _handle_sse(request)


# Mount the SSE POST message handler (no extra auth needed — session ID is
# generated during the authenticated SSE handshake above).
app.mount(
    "/messages",
    Starlette(routes=[Mount("/", app=sse.handle_post_message)]),
)
