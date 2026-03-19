"""
High-level transcription service built on top of QUOClient.

Provides:
  - Fetch single / list transcriptions
  - Full-text search across segments
  - Keyword & sentiment filtering
  - Bulk export (JSON, CSV, plain text)
  - Async transcription polling
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import datetime
from typing import Callable, Iterator, Optional

from .client import QUOClient
from .config import QUOConfig
from .exceptions import QUOTranscriptionError
from .models import (
    Call,
    CallListResult,
    Transcription,
    TranscriptionListResult,
    TranscriptionStatus,
)

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 50


class TranscriptionService:
    """
    Service layer for QUO VOIP transcription operations.

    Example
    -------
    >>> from quo_voip import TranscriptionService, QUOConfig
    >>> svc = TranscriptionService(QUOConfig(api_key="..."))
    >>> tx = svc.get("txn_abc123")
    >>> print(tx.render_transcript())
    """

    def __init__(self, config: Optional[QUOConfig] = None) -> None:
        self.config = config or QUOConfig.from_env()
        self.client = QUOClient(self.config)

    # ── Single resource ───────────────────────────────────────────────────────

    def get(self, transcription_id: str) -> Transcription:
        """Fetch a single transcription by ID."""
        data = self.client.get(f"/transcriptions/{transcription_id}")
        return Transcription.from_dict(data)

    def get_for_call(self, call_id: str) -> Transcription:
        """Fetch the transcription associated with a call."""
        data = self.client.get(f"/calls/{call_id}/transcription")
        return Transcription.from_dict(data)

    def get_call(self, call_id: str) -> Call:
        """Fetch a single call record."""
        data = self.client.get(f"/calls/{call_id}")
        return Call.from_dict(data)

    # ── Listing ───────────────────────────────────────────────────────────────

    def list(
        self,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        status: Optional[TranscriptionStatus] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        phone_number: Optional[str] = None,
        language: Optional[str] = None,
        call_direction: Optional[str] = None,
    ) -> TranscriptionListResult:
        """List transcriptions with optional filters."""
        params: dict = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status.value
        if from_date:
            params["from"] = from_date.isoformat()
        if to_date:
            params["to"] = to_date.isoformat()
        if phone_number:
            params["phone_number"] = phone_number
        if language:
            params["language"] = language
        if call_direction:
            params["direction"] = call_direction

        data = self.client.get("/transcriptions", params=params)
        return TranscriptionListResult.from_dict(data)

    def list_calls(
        self,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
        status: Optional[str] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        phone_number: Optional[str] = None,
        has_transcription: Optional[bool] = None,
    ) -> CallListResult:
        """List call records with optional filters."""
        params: dict = {"page": page, "page_size": page_size}
        if status:
            params["status"] = status
        if from_date:
            params["from"] = from_date.isoformat()
        if to_date:
            params["to"] = to_date.isoformat()
        if phone_number:
            params["phone_number"] = phone_number
        if has_transcription is not None:
            params["has_transcription"] = str(has_transcription).lower()

        data = self.client.get("/calls", params=params)
        return CallListResult.from_dict(data)

    def iter_all(
        self,
        page_size: int = DEFAULT_PAGE_SIZE,
        **filter_kwargs,
    ) -> Iterator[Transcription]:
        """
        Lazily iterate over ALL transcriptions matching the filters,
        automatically handling pagination.
        """
        page = 1
        while True:
            result = self.list(page=page, page_size=page_size, **filter_kwargs)
            yield from result.items
            if not result.has_more:
                break
            page += 1

    # ── Search ────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> TranscriptionListResult:
        """
        Full-text search across transcription segments.
        Returns transcriptions that contain the query string.
        """
        params: dict = {
            "q": query,
            "page": page,
            "page_size": page_size,
        }
        if from_date:
            params["from"] = from_date.isoformat()
        if to_date:
            params["to"] = to_date.isoformat()

        data = self.client.get("/transcriptions/search", params=params)
        return TranscriptionListResult.from_dict(data)

    def search_local(
        self,
        transcriptions: list[Transcription],
        query: str,
        case_sensitive: bool = False,
    ) -> list[tuple[Transcription, list[str]]]:
        """
        Client-side search across a list of Transcription objects.
        Returns list of (transcription, matching_segment_texts).
        """
        results = []
        needle = query if case_sensitive else query.lower()

        for tx in transcriptions:
            matches = []
            for seg in tx.segments:
                haystack = seg.text if case_sensitive else seg.text.lower()
                if needle in haystack:
                    matches.append(seg.text)
            if matches:
                results.append((tx, matches))

        return results

    def filter_by_sentiment(
        self,
        transcriptions: list[Transcription],
        sentiment: str,                        # "positive" | "neutral" | "negative"
        threshold: float = 0.5,                # fraction of segments that must match
    ) -> list[Transcription]:
        """Filter transcriptions where at least `threshold` of segments match the sentiment."""
        result = []
        for tx in transcriptions:
            if not tx.segments:
                continue
            matching = sum(
                1 for s in tx.segments if s.sentiment and s.sentiment.value == sentiment
            )
            if matching / len(tx.segments) >= threshold:
                result.append(tx)
        return result

    # ── Polling ───────────────────────────────────────────────────────────────

    def wait_for_transcription(
        self,
        call_id: str,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
        on_status_change: Optional[Callable[[TranscriptionStatus], None]] = None,
    ) -> Transcription:
        """
        Poll until the transcription for `call_id` is completed.
        Raises QUOTranscriptionError on timeout or failure.
        """
        deadline = time.time() + timeout
        last_status = None

        while time.time() < deadline:
            try:
                tx = self.get_for_call(call_id)
            except Exception as exc:
                logger.warning("Poll error: %s – retrying in %.1fs", exc, poll_interval)
                time.sleep(poll_interval)
                continue

            if tx.status != last_status:
                last_status = tx.status
                if on_status_change:
                    on_status_change(tx.status)

            if tx.status == TranscriptionStatus.COMPLETED:
                return tx
            if tx.status == TranscriptionStatus.FAILED:
                raise QUOTranscriptionError(f"Transcription failed for call {call_id}")

            logger.debug("Transcription status: %s – waiting %.1fs", tx.status.value, poll_interval)
            time.sleep(poll_interval)

        raise QUOTranscriptionError(
            f"Timed out waiting for transcription of call {call_id} after {timeout}s"
        )

    # ── Export ────────────────────────────────────────────────────────────────

    def export_json(
        self,
        transcriptions: list[Transcription] | Transcription,
        pretty: bool = True,
    ) -> str:
        """Serialize one or more transcriptions to JSON."""
        if isinstance(transcriptions, Transcription):
            data = transcriptions.to_dict()
        else:
            data = [t.to_dict() for t in transcriptions]
        return json.dumps(data, indent=2 if pretty else None, default=str)

    def export_csv(self, transcriptions: list[Transcription]) -> str:
        """
        Export all transcription segments to CSV.
        Columns: call_id, transcription_id, timestamp, speaker, text, confidence, sentiment
        """
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            ["call_id", "transcription_id", "timestamp", "speaker_id", "speaker_name",
             "text", "confidence", "sentiment"]
        )
        for tx in transcriptions:
            speaker_map = {sp.id: sp for sp in tx.speakers}
            for seg in tx.segments:
                speaker = speaker_map.get(seg.speaker_id)
                writer.writerow([
                    tx.call_id,
                    tx.id,
                    seg.formatted_timestamp(),
                    seg.speaker_id,
                    speaker.name or speaker.role if speaker else "",
                    seg.text,
                    f"{seg.confidence:.3f}",
                    seg.sentiment.value if seg.sentiment else "",
                ])
        return output.getvalue()

    def export_text(
        self,
        transcription: Transcription,
        include_timestamps: bool = True,
        include_speakers: bool = True,
        include_metadata: bool = True,
    ) -> str:
        """Export a single transcription as human-readable plain text."""
        lines = []

        if include_metadata:
            lines.append(f"Call ID:      {transcription.call_id}")
            lines.append(f"Transcript ID:{transcription.id}")
            lines.append(f"Language:     {transcription.language}")
            lines.append(f"Duration:     {transcription.duration_seconds or '?'}s")
            if transcription.summary:
                lines.append(f"\nSummary:\n{transcription.summary}")
            lines.append("\n" + "─" * 60)

        lines.append(transcription.render_transcript(
            include_timestamps=include_timestamps,
            include_speakers=include_speakers,
        ))

        return "\n".join(lines)
