"""
Unit tests for QUO VOIP data models.
"""

import pytest
from datetime import datetime, timezone
from quo_voip.models import (
    Call, CallStatus,
    Transcription, TranscriptionStatus, TranscriptionSegment,
    Speaker, SentimentLabel,
)

NOW_ISO = "2024-06-01T10:00:00"
NOW_ISO2 = "2024-06-01T10:30:00"


def make_speaker(id="sp1", name="Alice", role="agent"):
    return {
        "id": id,
        "name": name,
        "phone_number": "+15551234567",
        "role": role,
    }


def make_segment(id="seg1", speaker_id="sp1", text="Hello there", start=0.0, end=3.0, confidence=0.95):
    return {
        "id": id,
        "speaker_id": speaker_id,
        "text": text,
        "start_time": start,
        "end_time": end,
        "confidence": confidence,
        "sentiment": "positive",
        "keywords": ["hello"],
    }


def make_transcription(**overrides):
    base = {
        "id": "txn_001",
        "call_id": "call_001",
        "status": "completed",
        "language": "en",
        "segments": [make_segment()],
        "speakers": [make_speaker()],
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO2,
    }
    base.update(overrides)
    return base


def make_call(**overrides):
    base = {
        "id": "call_001",
        "status": "completed",
        "direction": "inbound",
        "from_number": "+15559876543",
        "to_number": "+15551234567",
        "started_at": NOW_ISO,
        "ended_at": NOW_ISO2,
        "duration_seconds": 1800.0,
        "recording_url": None,
        "transcription_id": "txn_001",
    }
    base.update(overrides)
    return base


# ── Speaker ───────────────────────────────────────────────────────────────────

class TestSpeaker:
    def test_from_dict(self):
        sp = Speaker.from_dict(make_speaker())
        assert sp.id == "sp1"
        assert sp.name == "Alice"
        assert sp.role == "agent"

    def test_roundtrip(self):
        data = make_speaker()
        assert Speaker.from_dict(data).to_dict() == data


# ── TranscriptionSegment ──────────────────────────────────────────────────────

class TestTranscriptionSegment:
    def test_from_dict(self):
        seg = TranscriptionSegment.from_dict(make_segment())
        assert seg.text == "Hello there"
        assert seg.confidence == 0.95
        assert seg.sentiment == SentimentLabel.POSITIVE

    def test_formatted_timestamp(self):
        seg = TranscriptionSegment.from_dict(make_segment(start=75.0))
        assert seg.formatted_timestamp() == "01:15"

    def test_timestamp_zero(self):
        seg = TranscriptionSegment.from_dict(make_segment(start=0.0))
        assert seg.formatted_timestamp() == "00:00"

    def test_roundtrip(self):
        data = make_segment()
        seg = TranscriptionSegment.from_dict(data)
        d2 = seg.to_dict()
        assert d2["text"] == data["text"]
        assert d2["sentiment"] == "positive"


# ── Transcription ─────────────────────────────────────────────────────────────

class TestTranscription:
    def test_from_dict_basic(self):
        tx = Transcription.from_dict(make_transcription())
        assert tx.id == "txn_001"
        assert tx.call_id == "call_001"
        assert tx.status == TranscriptionStatus.COMPLETED
        assert len(tx.segments) == 1
        assert len(tx.speakers) == 1

    def test_auto_full_text(self):
        tx = Transcription.from_dict(make_transcription())
        assert tx.full_text == "Hello there"

    def test_auto_word_count(self):
        tx = Transcription.from_dict(make_transcription())
        assert tx.word_count == 2

    def test_average_confidence(self):
        data = make_transcription(segments=[
            make_segment(id="s1", confidence=0.8),
            make_segment(id="s2", confidence=0.9),
        ])
        tx = Transcription.from_dict(data)
        assert abs(tx.average_confidence - 0.85) < 0.001

    def test_render_transcript_with_timestamps(self):
        tx = Transcription.from_dict(make_transcription())
        rendered = tx.render_transcript(include_timestamps=True, include_speakers=True)
        assert "[00:00]" in rendered
        assert "Alice:" in rendered
        assert "Hello there" in rendered

    def test_render_transcript_no_timestamps(self):
        tx = Transcription.from_dict(make_transcription())
        rendered = tx.render_transcript(include_timestamps=False, include_speakers=False)
        assert "[" not in rendered
        assert rendered.strip() == "Hello there"

    def test_roundtrip_dict(self):
        data = make_transcription()
        tx = Transcription.from_dict(data)
        d2 = tx.to_dict()
        assert d2["id"] == data["id"]
        assert d2["call_id"] == data["call_id"]

    def test_multiple_segments(self):
        segs = [
            make_segment(id=f"s{i}", text=f"Word {i}", start=float(i), end=float(i+1))
            for i in range(5)
        ]
        tx = Transcription.from_dict(make_transcription(segments=segs))
        assert len(tx.segments) == 5
        assert "Word 0" in tx.full_text


# ── Call ──────────────────────────────────────────────────────────────────────

class TestCall:
    def test_from_dict(self):
        c = Call.from_dict(make_call())
        assert c.id == "call_001"
        assert c.status == CallStatus.COMPLETED
        assert c.direction == "inbound"
        assert c.duration_seconds == 1800.0

    def test_optional_fields(self):
        data = make_call(recording_url=None, transcription_id=None, ended_at=None)
        c = Call.from_dict(data)
        assert c.recording_url is None
        assert c.ended_at is None

    def test_roundtrip(self):
        data = make_call()
        c = Call.from_dict(data)
        d2 = c.to_dict()
        assert d2["id"] == data["id"]
        assert d2["status"] == "completed"
