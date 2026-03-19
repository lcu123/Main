"""
Data models for QUO VOIP API responses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class CallStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    MISSED = "missed"
    VOICEMAIL = "voicemail"


class TranscriptionStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class SentimentLabel(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


@dataclass
class Speaker:
    id: str
    name: Optional[str]
    phone_number: Optional[str]
    role: str  # "agent" | "customer" | "unknown"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Speaker":
        return cls(
            id=data["id"],
            name=data.get("name"),
            phone_number=data.get("phone_number"),
            role=data.get("role", "unknown"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "phone_number": self.phone_number,
            "role": self.role,
        }


@dataclass
class TranscriptionSegment:
    id: str
    speaker_id: str
    text: str
    start_time: float          # seconds from call start
    end_time: float
    confidence: float          # 0.0 – 1.0
    sentiment: Optional[SentimentLabel] = None
    keywords: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptionSegment":
        sentiment_raw = data.get("sentiment")
        return cls(
            id=data["id"],
            speaker_id=data["speaker_id"],
            text=data["text"],
            start_time=float(data["start_time"]),
            end_time=float(data["end_time"]),
            confidence=float(data.get("confidence", 1.0)),
            sentiment=SentimentLabel(sentiment_raw) if sentiment_raw else None,
            keywords=data.get("keywords", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "speaker_id": self.speaker_id,
            "text": self.text,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "confidence": self.confidence,
            "sentiment": self.sentiment.value if self.sentiment else None,
            "keywords": self.keywords,
        }

    def formatted_timestamp(self) -> str:
        minutes = int(self.start_time // 60)
        seconds = int(self.start_time % 60)
        return f"{minutes:02d}:{seconds:02d}"


@dataclass
class Transcription:
    id: str
    call_id: str
    status: TranscriptionStatus
    language: str
    segments: list[TranscriptionSegment]
    speakers: list[Speaker]
    created_at: datetime
    updated_at: datetime
    summary: Optional[str] = None
    full_text: Optional[str] = None
    duration_seconds: Optional[float] = None
    word_count: Optional[int] = None
    average_confidence: Optional[float] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Transcription":
        segments = [TranscriptionSegment.from_dict(s) for s in data.get("segments", [])]
        speakers = [Speaker.from_dict(sp) for sp in data.get("speakers", [])]

        # Compute derived fields if not provided
        full_text = data.get("full_text")
        if not full_text and segments:
            full_text = " ".join(s.text for s in segments)

        avg_conf = data.get("average_confidence")
        if avg_conf is None and segments:
            avg_conf = sum(s.confidence for s in segments) / len(segments)

        return cls(
            id=data["id"],
            call_id=data["call_id"],
            status=TranscriptionStatus(data.get("status", "completed")),
            language=data.get("language", "en"),
            segments=segments,
            speakers=speakers,
            created_at=datetime.fromisoformat(data["created_at"]),
            updated_at=datetime.fromisoformat(data["updated_at"]),
            summary=data.get("summary"),
            full_text=full_text,
            duration_seconds=data.get("duration_seconds"),
            word_count=data.get("word_count") or (len(full_text.split()) if full_text else None),
            average_confidence=avg_conf,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "call_id": self.call_id,
            "status": self.status.value,
            "language": self.language,
            "segments": [s.to_dict() for s in self.segments],
            "speakers": [sp.to_dict() for sp in self.speakers],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "summary": self.summary,
            "full_text": self.full_text,
            "duration_seconds": self.duration_seconds,
            "word_count": self.word_count,
            "average_confidence": self.average_confidence,
        }

    def render_transcript(self, include_timestamps: bool = True, include_speakers: bool = True) -> str:
        """Return a human-readable transcript string."""
        speaker_map = {sp.id: sp for sp in self.speakers}
        lines = []
        for seg in self.segments:
            parts = []
            if include_timestamps:
                parts.append(f"[{seg.formatted_timestamp()}]")
            if include_speakers:
                speaker = speaker_map.get(seg.speaker_id)
                label = speaker.name or speaker.role.title() if speaker else seg.speaker_id
                parts.append(f"{label}:")
            parts.append(seg.text)
            lines.append(" ".join(parts))
        return "\n".join(lines)


@dataclass
class Call:
    id: str
    status: CallStatus
    direction: str              # "inbound" | "outbound"
    from_number: str
    to_number: str
    started_at: datetime
    ended_at: Optional[datetime]
    duration_seconds: Optional[float]
    recording_url: Optional[str]
    transcription_id: Optional[str]
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Call":
        return cls(
            id=data["id"],
            status=CallStatus(data.get("status", "completed")),
            direction=data.get("direction", "inbound"),
            from_number=data["from_number"],
            to_number=data["to_number"],
            started_at=datetime.fromisoformat(data["started_at"]),
            ended_at=datetime.fromisoformat(data["ended_at"]) if data.get("ended_at") else None,
            duration_seconds=data.get("duration_seconds"),
            recording_url=data.get("recording_url"),
            transcription_id=data.get("transcription_id"),
            tags=data.get("tags", []),
            metadata=data.get("metadata", {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status.value,
            "direction": self.direction,
            "from_number": self.from_number,
            "to_number": self.to_number,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "duration_seconds": self.duration_seconds,
            "recording_url": self.recording_url,
            "transcription_id": self.transcription_id,
            "tags": self.tags,
            "metadata": self.metadata,
        }


@dataclass
class TranscriptionListResult:
    items: list[Transcription]
    total: int
    page: int
    page_size: int
    has_more: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptionListResult":
        return cls(
            items=[Transcription.from_dict(t) for t in data.get("items", [])],
            total=data.get("total", 0),
            page=data.get("page", 1),
            page_size=data.get("page_size", 20),
            has_more=data.get("has_more", False),
        )


@dataclass
class CallListResult:
    items: list[Call]
    total: int
    page: int
    page_size: int
    has_more: bool

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CallListResult":
        return cls(
            items=[Call.from_dict(c) for c in data.get("items", [])],
            total=data.get("total", 0),
            page=data.get("page", 1),
            page_size=data.get("page_size", 20),
            has_more=data.get("has_more", False),
        )
