"""
Data models for the OpenPhone API responses.

Mirrors the shapes defined in the OpenPhone Public API v1 spec:
  GET /v1/calls           → Call, CallListResult
  GET /v1/calls/{id}      → Call
  GET /v1/call-transcripts/{id} → Transcript, TranscriptDialogueSegment
  GET /v1/call-summaries/{id}   → CallSummary
  GET /v1/call-recordings/{id}  → CallRecording
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional, Union


# ── Enums ─────────────────────────────────────────────────────────────────────

class CallDirection(str, Enum):
    INCOMING = "incoming"
    OUTGOING = "outgoing"


class CallStatus(str, Enum):
    QUEUED     = "queued"
    INITIATED  = "initiated"
    RINGING    = "ringing"
    IN_PROGRESS = "in-progress"
    COMPLETED  = "completed"
    BUSY       = "busy"
    FAILED     = "failed"
    NO_ANSWER  = "no-answer"
    CANCELED   = "canceled"
    MISSED     = "missed"
    ANSWERED   = "answered"
    FORWARDED  = "forwarded"
    ABANDONED  = "abandoned"


class TranscriptionStatus(str, Enum):
    ABSENT      = "absent"
    IN_PROGRESS = "in-progress"
    COMPLETED   = "completed"
    FAILED      = "failed"


class RecordingStatus(str, Enum):
    ABSENT      = "absent"
    COMPLETED   = "completed"
    DELETED     = "deleted"
    FAILED      = "failed"
    IN_PROGRESS = "in-progress"
    PAUSED      = "paused"
    PROCESSING  = "processing"
    STOPPED     = "stopped"
    STOPPING    = "stopping"


# ── Call ──────────────────────────────────────────────────────────────────────

@dataclass
class Call:
    """A single call record from GET /v1/calls or GET /v1/calls/{callId}."""
    id: str                                  # AC...
    phone_number_id: str                     # PN...
    direction: CallDirection
    status: CallStatus
    created_at: datetime
    participants: list[str]                  # E.164 phone numbers
    user_id: Optional[str] = None            # US...
    answered_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    duration: Optional[int] = None           # seconds
    answered_by: Optional[str] = None        # US...
    initiated_by: Optional[str] = None       # US...
    call_route: Optional[str] = None         # "phone-number" | "phone-menu"
    forwarded_from: Optional[str] = None
    forwarded_to: Optional[str] = None
    ai_handled: Optional[str] = None         # "ai-agent"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Call":
        def _dt(val: Optional[str]) -> Optional[datetime]:
            return datetime.fromisoformat(val) if val else None

        return cls(
            id=data["id"],
            phone_number_id=data["phoneNumberId"],
            direction=CallDirection(data["direction"]),
            status=CallStatus(data["status"]),
            created_at=datetime.fromisoformat(data["createdAt"]),
            participants=data.get("participants", []),
            user_id=data.get("userId"),
            answered_at=_dt(data.get("answeredAt")),
            completed_at=_dt(data.get("completedAt")),
            updated_at=_dt(data.get("updatedAt")),
            duration=data.get("duration"),
            answered_by=data.get("answeredBy"),
            initiated_by=data.get("initiatedBy"),
            call_route=data.get("callRoute"),
            forwarded_from=data.get("forwardedFrom"),
            forwarded_to=data.get("forwardedTo"),
            ai_handled=data.get("aiHandled"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phoneNumberId": self.phone_number_id,
            "userId": self.user_id,
            "direction": self.direction.value,
            "status": self.status.value,
            "createdAt": self.created_at.isoformat(),
            "answeredAt": self.answered_at.isoformat() if self.answered_at else None,
            "completedAt": self.completed_at.isoformat() if self.completed_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "duration": self.duration,
            "participants": self.participants,
            "answeredBy": self.answered_by,
            "initiatedBy": self.initiated_by,
            "callRoute": self.call_route,
            "forwardedFrom": self.forwarded_from,
            "forwardedTo": self.forwarded_to,
            "aiHandled": self.ai_handled,
        }


@dataclass
class CallListResult:
    """Paginated result from GET /v1/calls."""
    items: list[Call]
    total_items: int
    next_page_token: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallListResult":
        return cls(
            items=[Call.from_dict(c) for c in data.get("data", [])],
            total_items=data.get("totalItems", 0),
            next_page_token=data.get("nextPageToken"),
        )

    @property
    def has_more(self) -> bool:
        return self.next_page_token is not None


# ── Transcript ────────────────────────────────────────────────────────────────

@dataclass
class TranscriptDialogueSegment:
    """One turn of dialogue from a call transcript."""
    content: str
    start: float    # seconds from call start
    end: float
    identifier: Optional[str] = None   # E.164 phone number of speaker
    user_id: Optional[str] = None      # US... if speaker is an OpenPhone user

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptDialogueSegment":
        return cls(
            content=data["content"],
            start=float(data["start"]),
            end=float(data["end"]),
            identifier=data.get("identifier"),
            user_id=data.get("userId"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "start": self.start,
            "end": self.end,
            "identifier": self.identifier,
            "userId": self.user_id,
        }

    def formatted_timestamp(self) -> str:
        minutes = int(self.start // 60)
        seconds = int(self.start % 60)
        return f"{minutes:02d}:{seconds:02d}"


@dataclass
class Transcript:
    """Call transcript from GET /v1/call-transcripts/{id}."""
    call_id: str
    status: TranscriptionStatus
    created_at: datetime
    duration: int                                        # seconds
    dialogue: Optional[list[TranscriptDialogueSegment]]  # None when status != completed

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcript":
        # Response is wrapped: {"data": {...}}
        inner = data.get("data", data)
        dialogue_raw = inner.get("dialogue")
        return cls(
            call_id=inner["callId"],
            status=TranscriptionStatus(inner.get("status", "completed")),
            created_at=datetime.fromisoformat(inner["createdAt"]),
            duration=inner.get("duration", 0),
            dialogue=[TranscriptDialogueSegment.from_dict(s) for s in dialogue_raw]
            if dialogue_raw else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "callId": self.call_id,
            "status": self.status.value,
            "createdAt": self.created_at.isoformat(),
            "duration": self.duration,
            "dialogue": [s.to_dict() for s in self.dialogue] if self.dialogue else None,
        }

    def render_transcript(
        self,
        include_timestamps: bool = True,
        include_speakers: bool = True,
    ) -> str:
        """Return a human-readable transcript string."""
        if not self.dialogue:
            return f"[Transcript status: {self.status.value}]"
        lines = []
        for seg in self.dialogue:
            parts = []
            if include_timestamps:
                parts.append(f"[{seg.formatted_timestamp()}]")
            if include_speakers:
                label = seg.identifier or seg.user_id or "Unknown"
                parts.append(f"{label}:")
            parts.append(seg.content)
            lines.append(" ".join(parts))
        return "\n".join(lines)

    @property
    def full_text(self) -> Optional[str]:
        if not self.dialogue:
            return None
        return " ".join(s.content for s in self.dialogue)


# Alias kept so any code referencing Transcription still works
Transcription = Transcript


# ── Call Summary ──────────────────────────────────────────────────────────────

@dataclass
class SummaryJobResult:
    name: str
    value: Union[str, int, float, bool]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryJobResult":
        return cls(name=data["name"], value=data["value"])


@dataclass
class SummaryJob:
    icon: str
    name: str
    results: list[SummaryJobResult]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SummaryJob":
        results = [SummaryJobResult.from_dict(r) for r in data.get("result", {}).get("data", [])]
        return cls(icon=data.get("icon", ""), name=data["name"], results=results)


@dataclass
class CallSummary:
    """AI-generated call summary from GET /v1/call-summaries/{callId}."""
    call_id: str
    status: TranscriptionStatus
    summary: Optional[list[str]]
    next_steps: Optional[list[str]]
    jobs: Optional[list[SummaryJob]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallSummary":
        inner = data.get("data", data)
        return cls(
            call_id=inner["callId"],
            status=TranscriptionStatus(inner.get("status", "completed")),
            summary=inner.get("summary"),
            next_steps=inner.get("nextSteps"),
            jobs=[SummaryJob.from_dict(j) for j in inner["jobs"]] if inner.get("jobs") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "callId": self.call_id,
            "status": self.status.value,
            "summary": self.summary,
            "nextSteps": self.next_steps,
            "jobs": [{"icon": j.icon, "name": j.name,
                      "results": [{"name": r.name, "value": r.value} for r in j.results]}
                     for j in self.jobs] if self.jobs else None,
        }


# ── Call Recording ─────────────────────────────────────────────────────────────

@dataclass
class CallRecording:
    """A recording entry from GET /v1/call-recordings/{callId}."""
    id: str
    status: RecordingStatus
    duration: Optional[int]      # seconds
    type: Optional[str]          # "audio/mpeg"
    url: Optional[str]
    start_time: Optional[datetime]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallRecording":
        return cls(
            id=data["id"],
            status=RecordingStatus(data.get("status", "completed")),
            duration=data.get("duration"),
            type=data.get("type"),
            url=data.get("url"),
            start_time=datetime.fromisoformat(data["startTime"]) if data.get("startTime") else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "duration": self.duration,
            "type": self.type,
            "url": self.url,
            "startTime": self.start_time.isoformat() if self.start_time else None,
        }


@dataclass
class CallRecordingListResult:
    items: list[CallRecording]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallRecordingListResult":
        return cls(items=[CallRecording.from_dict(r) for r in data.get("data", [])])
