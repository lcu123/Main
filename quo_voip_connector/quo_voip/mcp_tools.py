"""
Tool definitions and handler for the QUO VOIP MCP integration.

Shared between the stdio server (mcp/server.py) and the
remote SSE server (server/remote_mcp.py) so both stay in sync
with the same tool schemas and dispatch logic.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from quo_voip import QUOClient, QUOConfig, TranscriptionService
from quo_voip.models import TranscriptionStatus

logger = logging.getLogger(__name__)


# ── Tool schemas (JSON Schema) ─────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "quo_get_transcription",
        "description": (
            "Fetch a single call transcription from QUO VOIP by its transcription ID. "
            "Returns full text, per-speaker segments, timestamps, confidence scores, and sentiment."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcription_id": {
                    "type": "string",
                    "description": "The QUO VOIP transcription ID (e.g. txn_abc123)",
                }
            },
            "required": ["transcription_id"],
        },
    },
    {
        "name": "quo_get_call_transcription",
        "description": "Fetch the transcription for a specific call by its Call ID from QUO VOIP.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "The QUO VOIP call ID (e.g. call_xyz456)",
                }
            },
            "required": ["call_id"],
        },
    },
    {
        "name": "quo_list_transcriptions",
        "description": (
            "List recent call transcriptions from QUO VOIP with optional filters. "
            "Supports filtering by date range, phone number, language, and call direction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "description": "Page number (default: 1)", "default": 1},
                "page_size": {
                    "type": "integer",
                    "description": "Results per page (default: 20, max: 100)",
                    "default": 20,
                },
                "status": {
                    "type": "string",
                    "enum": ["pending", "processing", "completed", "failed"],
                    "description": "Filter by transcription status",
                },
                "from_date": {
                    "type": "string",
                    "description": "ISO 8601 start date filter (e.g. 2024-01-01T00:00:00)",
                },
                "to_date": {"type": "string", "description": "ISO 8601 end date filter"},
                "phone_number": {
                    "type": "string",
                    "description": "Filter by phone number (caller or callee)",
                },
                "language": {
                    "type": "string",
                    "description": "Filter by transcript language code (e.g. en, es, fr)",
                },
                "call_direction": {
                    "type": "string",
                    "enum": ["inbound", "outbound"],
                    "description": "Filter by call direction",
                },
            },
        },
    },
    {
        "name": "quo_search_transcriptions",
        "description": (
            "Search QUO VOIP transcriptions for a text query. "
            "Returns transcriptions containing the query across all segments."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query text"},
                "from_date": {"type": "string", "description": "ISO 8601 start date filter"},
                "to_date": {"type": "string", "description": "ISO 8601 end date filter"},
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "quo_list_calls",
        "description": (
            "List call records from QUO VOIP with optional filters. "
            "Returns call metadata including duration, direction, phone numbers, and status."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "integer", "default": 1},
                "page_size": {"type": "integer", "default": 20},
                "status": {
                    "type": "string",
                    "enum": ["active", "completed", "failed", "missed", "voicemail"],
                },
                "from_date": {"type": "string"},
                "to_date": {"type": "string"},
                "phone_number": {"type": "string"},
                "has_transcription": {
                    "type": "boolean",
                    "description": "Only return calls that have a transcription",
                },
            },
        },
    },
    {
        "name": "quo_export_transcript_text",
        "description": (
            "Fetch a transcription and return it as formatted plain text, "
            "with optional timestamps and speaker labels."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "transcription_id": {"type": "string"},
                "include_timestamps": {"type": "boolean", "default": True},
                "include_speakers": {"type": "boolean", "default": True},
                "include_metadata": {"type": "boolean", "default": True},
            },
            "required": ["transcription_id"],
        },
    },
    {
        "name": "quo_wait_for_transcription",
        "description": (
            "Poll QUO VOIP until a call's transcription is ready. "
            "Useful immediately after a call ends. Returns the completed transcription."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "call_id": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 120},
                "poll_interval_seconds": {"type": "number", "default": 5},
            },
            "required": ["call_id"],
        },
    },
]


# ── Tool dispatcher ────────────────────────────────────────────────────────────

class QUOMCPHandler:
    """Dispatches MCP tool calls to the QUO VOIP service layer."""

    def __init__(self, config: QUOConfig) -> None:
        self.svc = TranscriptionService(config)

    def handle(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call and return a JSON string result."""
        try:
            return self._dispatch(tool_name, args)
        except Exception as exc:
            logger.error("Tool %s error: %s", tool_name, exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:  # noqa: PLR0911
        if name == "quo_get_transcription":
            tx = self.svc.get(args["transcription_id"])
            return json.dumps(tx.to_dict(), default=str)

        if name == "quo_get_call_transcription":
            tx = self.svc.get_for_call(args["call_id"])
            return json.dumps(tx.to_dict(), default=str)

        if name == "quo_list_transcriptions":
            result = self.svc.list(
                page=args.get("page", 1),
                page_size=args.get("page_size", 20),
                status=TranscriptionStatus(args["status"]) if args.get("status") else None,
                from_date=datetime.fromisoformat(args["from_date"]) if args.get("from_date") else None,
                to_date=datetime.fromisoformat(args["to_date"]) if args.get("to_date") else None,
                phone_number=args.get("phone_number"),
                language=args.get("language"),
                call_direction=args.get("call_direction"),
            )
            return json.dumps(
                {
                    "total": result.total,
                    "page": result.page,
                    "page_size": result.page_size,
                    "has_more": result.has_more,
                    "items": [t.to_dict() for t in result.items],
                },
                default=str,
            )

        if name == "quo_search_transcriptions":
            result = self.svc.search(
                query=args["query"],
                from_date=datetime.fromisoformat(args["from_date"]) if args.get("from_date") else None,
                to_date=datetime.fromisoformat(args["to_date"]) if args.get("to_date") else None,
                page=args.get("page", 1),
                page_size=args.get("page_size", 20),
            )
            return json.dumps(
                {
                    "total": result.total,
                    "has_more": result.has_more,
                    "items": [t.to_dict() for t in result.items],
                },
                default=str,
            )

        if name == "quo_list_calls":
            result = self.svc.list_calls(
                page=args.get("page", 1),
                page_size=args.get("page_size", 20),
                status=args.get("status"),
                from_date=datetime.fromisoformat(args["from_date"]) if args.get("from_date") else None,
                to_date=datetime.fromisoformat(args["to_date"]) if args.get("to_date") else None,
                phone_number=args.get("phone_number"),
                has_transcription=args.get("has_transcription"),
            )
            return json.dumps(
                {
                    "total": result.total,
                    "has_more": result.has_more,
                    "items": [c.to_dict() for c in result.items],
                },
                default=str,
            )

        if name == "quo_export_transcript_text":
            tx = self.svc.get(args["transcription_id"])
            text = self.svc.export_text(
                tx,
                include_timestamps=args.get("include_timestamps", True),
                include_speakers=args.get("include_speakers", True),
                include_metadata=args.get("include_metadata", True),
            )
            return json.dumps({"text": text})

        if name == "quo_wait_for_transcription":
            tx = self.svc.wait_for_transcription(
                call_id=args["call_id"],
                poll_interval=args.get("poll_interval_seconds", 5.0),
                timeout=args.get("timeout_seconds", 120.0),
            )
            return json.dumps(tx.to_dict(), default=str)

        return json.dumps({"error": f"Unknown tool: {name}"})
