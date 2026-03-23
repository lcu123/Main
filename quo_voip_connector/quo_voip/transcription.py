"""
Service layer wrapping the OpenPhone REST API.

OpenPhone endpoints used:
  GET /v1/calls                     list calls
  GET /v1/calls/{callId}            single call
  GET /v1/call-transcripts/{id}     transcript for a call
  GET /v1/call-summaries/{callId}   AI summary for a call
  GET /v1/call-recordings/{callId}  recordings for a call
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Callable, Optional

from .client import QUOClient
from .config import QUOConfig
from .exceptions import QUOTranscriptionError
from .models import (
    Call,
    CallListResult,
    CallRecordingListResult,
    CallSummary,
    Transcript,
    TranscriptionStatus,
)

logger = logging.getLogger(__name__)


class TranscriptionService:
    """
    High-level service for OpenPhone call and transcript operations.

    Example
    -------
    >>> svc = TranscriptionService(QUOConfig(api_key="...", phone_number_id="PN..."))
    >>> calls = svc.list_calls(max_results=20)
    >>> tx = svc.get_transcript(calls.items[0].id)
    >>> print(tx.render_transcript())
    """

    def __init__(self, config: Optional[QUOConfig] = None) -> None:
        self.config = config or QUOConfig.from_env()
        self.client = QUOClient(self.config)

    # ── Calls ─────────────────────────────────────────────────────────────────

    def get_call(self, call_id: str) -> Call:
        """Fetch a single call by its ID (AC...)."""
        data = self.client.get(f"/v1/calls/{call_id}")
        return Call.from_dict(data.get("data", data))

    def list_calls(
        self,
        phone_number_id: Optional[str] = None,
        participants: Optional[list[str]] = None,
        user_id: Optional[str] = None,
        created_after: Optional[datetime] = None,
        created_before: Optional[datetime] = None,
        max_results: int = 10,
        page_token: Optional[str] = None,
    ) -> CallListResult:
        """
        List calls. ``phone_number_id`` and ``participants`` are both required
        by the OpenPhone API — if not supplied, the values from config are used.

        Parameters
        ----------
        phone_number_id:
            OpenPhone phone number ID (PN...). Falls back to config.phone_number_id.
        participants:
            List containing exactly one E.164 phone number to filter by.
        max_results:
            Number of results to return (1–100, default 10).
        """
        pn_id = phone_number_id or self.config.phone_number_id
        if not pn_id:
            raise ValueError(
                "phone_number_id is required. Set OPENPHONE_PHONE_NUMBER_ID "
                "or pass phone_number_id explicitly."
            )
        if not participants:
            raise ValueError(
                "participants is required (list with one E.164 phone number)."
            )

        params: dict = {
            "phoneNumberId": pn_id,
            "participants": participants[0],
            "maxResults": max(1, min(max_results, 100)),
        }
        if user_id:
            params["userId"] = user_id
        if created_after:
            params["createdAfter"] = created_after.isoformat()
        if created_before:
            params["createdBefore"] = created_before.isoformat()
        if page_token:
            params["pageToken"] = page_token

        data = self.client.get("/v1/calls", params=params)
        return CallListResult.from_dict(data)

    # ── Transcripts ───────────────────────────────────────────────────────────

    def get_transcript(self, call_id: str) -> Transcript:
        """
        Fetch the transcript for a call by its call ID (AC...).
        Requires Business or Scale plan.
        """
        data = self.client.get(f"/v1/call-transcripts/{call_id}")
        return Transcript.from_dict(data)

    # ── Summaries ─────────────────────────────────────────────────────────────

    def get_summary(self, call_id: str) -> CallSummary:
        """
        Fetch the AI-generated summary for a call.
        Requires Business or Scale plan.
        """
        data = self.client.get(f"/v1/call-summaries/{call_id}")
        return CallSummary.from_dict(data)

    # ── Recordings ────────────────────────────────────────────────────────────

    def get_recordings(self, call_id: str) -> CallRecordingListResult:
        """Fetch all recordings attached to a call."""
        data = self.client.get(f"/v1/call-recordings/{call_id}")
        return CallRecordingListResult.from_dict(data)

    # ── Polling ───────────────────────────────────────────────────────────────

    def wait_for_transcript(
        self,
        call_id: str,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
        on_status_change: Optional[Callable[[TranscriptionStatus], None]] = None,
    ) -> Transcript:
        """
        Poll until the transcript for ``call_id`` reaches status 'completed'.
        Raises QUOTranscriptionError on timeout or failure.
        """
        deadline = time.time() + timeout
        last_status = None

        while time.time() < deadline:
            try:
                tx = self.get_transcript(call_id)
            except Exception as exc:
                logger.warning("Poll error: %s — retrying in %.1fs", exc, poll_interval)
                time.sleep(poll_interval)
                continue

            if tx.status != last_status:
                last_status = tx.status
                if on_status_change:
                    on_status_change(tx.status)

            if tx.status == TranscriptionStatus.COMPLETED:
                return tx
            if tx.status == TranscriptionStatus.FAILED:
                raise QUOTranscriptionError(f"Transcript failed for call {call_id}")

            logger.debug("Transcript status: %s — waiting %.1fs", tx.status.value, poll_interval)
            time.sleep(poll_interval)

        raise QUOTranscriptionError(
            f"Timed out waiting for transcript of call {call_id} after {timeout}s"
        )

    # ── Convenience export ────────────────────────────────────────────────────

    def export_text(
        self,
        transcript: Transcript,
        include_timestamps: bool = True,
        include_speakers: bool = True,
        include_metadata: bool = True,
    ) -> str:
        """Return a human-readable plain-text export of a transcript."""
        lines = []
        if include_metadata:
            lines.append(f"Call ID:  {transcript.call_id}")
            lines.append(f"Status:   {transcript.status.value}")
            lines.append(f"Duration: {transcript.duration}s")
            lines.append("─" * 60)
        lines.append(transcript.render_transcript(
            include_timestamps=include_timestamps,
            include_speakers=include_speakers,
        ))
        return "\n".join(lines)
