"""
MCP tool definitions and handler for the OpenPhone connector.

All tools map directly to OpenPhone Public API v1 endpoints:
  /v1/calls                   → openphone_list_calls
  /v1/calls/{id}              → openphone_get_call
  /v1/call-transcripts/{id}   → openphone_get_transcript
  /v1/call-summaries/{id}     → openphone_get_summary
  /v1/call-recordings/{id}    → openphone_get_recordings
  (polling helper)            → openphone_wait_for_transcript
"""

from __future__ import annotations

import json
import logging
from typing import Any

from quo_voip import QUOConfig
from quo_voip.transcription import TranscriptionService

logger = logging.getLogger(__name__)


# ── Tool schemas (JSON Schema) ─────────────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "openphone_list_calls",
        "description": (
            "List calls from OpenPhone for a given phone number and participant. "
            "Both phoneNumberId (PN...) and participants (E.164 phone number) are required "
            "by the OpenPhone API. Returns call metadata including direction, status, duration, "
            "and timestamps."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "phone_number_id": {
                    "type": "string",
                    "description": "OpenPhone phone number ID (PN...). Falls back to OPENPHONE_PHONE_NUMBER_ID env var.",
                },
                "participants": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 1,
                    "description": "List with exactly one E.164 phone number to filter by (e.g. [\"+15555550100\"]).",
                },
                "user_id": {
                    "type": "string",
                    "description": "Filter by OpenPhone user ID (US...).",
                },
                "created_after": {
                    "type": "string",
                    "description": "ISO 8601 datetime — return calls created after this time.",
                },
                "created_before": {
                    "type": "string",
                    "description": "ISO 8601 datetime — return calls created before this time.",
                },
                "max_results": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 100,
                    "description": "Number of calls to return (max 100).",
                },
                "page_token": {
                    "type": "string",
                    "description": "Pagination token from a previous response.",
                },
            },
            "required": ["participants"],
        },
    },
    {
        "name": "openphone_get_call",
        "description": "Fetch a single call record from OpenPhone by its call ID (AC...).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "OpenPhone call ID (AC...).",
                }
            },
            "required": ["call_id"],
        },
    },
    {
        "name": "openphone_get_transcript",
        "description": (
            "Fetch the full call transcript from OpenPhone for a given call ID. "
            "Returns time-stamped dialogue segments with speaker phone numbers. "
            "Requires Business or Scale plan. Status will be 'absent' if no transcript exists."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "OpenPhone call ID (AC...).",
                },
                "include_timestamps": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include [MM:SS] timestamps in rendered output.",
                },
                "include_speakers": {
                    "type": "boolean",
                    "default": True,
                    "description": "Include speaker phone number / user ID labels.",
                },
            },
            "required": ["call_id"],
        },
    },
    {
        "name": "openphone_get_summary",
        "description": (
            "Fetch the AI-generated call summary from OpenPhone for a given call ID. "
            "Returns a summary list, next steps, and any custom AI job results. "
            "Requires Business or Scale plan."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "OpenPhone call ID (AC...).",
                }
            },
            "required": ["call_id"],
        },
    },
    {
        "name": "openphone_get_recordings",
        "description": "Fetch all call recordings attached to a call from OpenPhone.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": "OpenPhone call ID (AC...).",
                }
            },
            "required": ["call_id"],
        },
    },
    {
        "name": "openphone_wait_for_transcript",
        "description": (
            "Poll OpenPhone until the transcript for a call is ready (status = completed). "
            "Useful immediately after a call ends. Returns the completed transcript."
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
    """Dispatches MCP tool calls to the OpenPhone service layer."""

    def __init__(self, config: QUOConfig) -> None:
        self.svc = TranscriptionService(config)

    def handle(self, tool_name: str, args: dict[str, Any]) -> str:
        try:
            return self._dispatch(tool_name, args)
        except Exception as exc:
            logger.error("Tool %s error: %s", tool_name, exc, exc_info=True)
            return json.dumps({"error": str(exc)})

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "openphone_list_calls":
            from datetime import datetime
            result = self.svc.list_calls(
                phone_number_id=args.get("phone_number_id"),
                participants=args.get("participants", []),
                user_id=args.get("user_id"),
                created_after=datetime.fromisoformat(args["created_after"]) if args.get("created_after") else None,
                created_before=datetime.fromisoformat(args["created_before"]) if args.get("created_before") else None,
                max_results=args.get("max_results", 10),
                page_token=args.get("page_token"),
            )
            return json.dumps({
                "totalItems": result.total_items,
                "nextPageToken": result.next_page_token,
                "items": [c.to_dict() for c in result.items],
            }, default=str)

        if name == "openphone_get_call":
            call = self.svc.get_call(args["call_id"])
            return json.dumps(call.to_dict(), default=str)

        if name == "openphone_get_transcript":
            tx = self.svc.get_transcript(args["call_id"])
            rendered = tx.render_transcript(
                include_timestamps=args.get("include_timestamps", True),
                include_speakers=args.get("include_speakers", True),
            )
            return json.dumps({**tx.to_dict(), "rendered": rendered}, default=str)

        if name == "openphone_get_summary":
            summary = self.svc.get_summary(args["call_id"])
            return json.dumps(summary.to_dict(), default=str)

        if name == "openphone_get_recordings":
            result = self.svc.get_recordings(args["call_id"])
            return json.dumps({"items": [r.to_dict() for r in result.items]}, default=str)

        if name == "openphone_wait_for_transcript":
            tx = self.svc.wait_for_transcript(
                call_id=args["call_id"],
                poll_interval=args.get("poll_interval_seconds", 5.0),
                timeout=args.get("timeout_seconds", 120.0),
            )
            return json.dumps(tx.to_dict(), default=str)

        return json.dumps({"error": f"Unknown tool: {name}"})
