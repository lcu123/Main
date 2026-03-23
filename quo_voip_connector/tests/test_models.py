"""
Unit tests for QUO VOIP data models (OpenPhone schema).
"""

import pytest
from datetime import datetime, timezone
from quo_voip.models import (
    Call,
    CallDirection,
    CallListResult,
    CallRecording,
    CallRecordingListResult,
    CallStatus,
    CallSummary,
    RecordingStatus,
    Transcript,
    TranscriptDialogueSegment,
    TranscriptionStatus,
    Transcription,  # alias for Transcript
)

NOW_ISO = "2024-06-01T10:00:00"
NOW_ISO2 = "2024-06-01T10:30:00"


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_call(**overrides):
    base = {
        "id": "ACtest001",
        "phoneNumberId": "PNtest001",
        "direction": "incoming",
        "status": "completed",
        "createdAt": NOW_ISO,
        "participants": ["+15559876543"],
        "duration": 90,
    }
    base.update(overrides)
    return base


def make_segment(**overrides):
    base = {
        "content": "Hello there",
        "start": 0.0,
        "end": 3.0,
        "identifier": "+15559876543",
        "userId": None,
    }
    base.update(overrides)
    return base


def make_transcript(**overrides):
    base = {
        "data": {
            "callId": "ACtest001",
            "status": "completed",
            "createdAt": NOW_ISO,
            "duration": 90,
            "dialogue": [make_segment()],
        }
    }
    if overrides:
        base["data"].update(overrides)
    return base


def make_recording(**overrides):
    base = {
        "id": "RCtest001",
        "status": "completed",
        "duration": 90,
        "type": "audio/mpeg",
        "url": "https://example.com/recording.mp3",
        "startTime": NOW_ISO,
    }
    base.update(overrides)
    return base


# ── TranscriptDialogueSegment ─────────────────────────────────────────────────

class TestTranscriptDialogueSegment:
    def test_from_dict(self):
        seg = TranscriptDialogueSegment.from_dict(make_segment())
        assert seg.content == "Hello there"
        assert seg.start == 0.0
        assert seg.end == 3.0
        assert seg.identifier == "+15559876543"

    def test_formatted_timestamp_zero(self):
        seg = TranscriptDialogueSegment.from_dict(make_segment(start=0.0))
        assert seg.formatted_timestamp() == "00:00"

    def test_formatted_timestamp_non_zero(self):
        seg = TranscriptDialogueSegment.from_dict(make_segment(start=75.0))
        assert seg.formatted_timestamp() == "01:15"

    def test_roundtrip(self):
        data = make_segment()
        seg = TranscriptDialogueSegment.from_dict(data)
        d2 = seg.to_dict()
        assert d2["content"] == data["content"]
        assert d2["start"] == data["start"]
        assert d2["identifier"] == data["identifier"]

    def test_optional_fields_none(self):
        seg = TranscriptDialogueSegment.from_dict({"content": "Hi", "start": 0.0, "end": 1.0})
        assert seg.identifier is None
        assert seg.user_id is None


# ── Transcript ────────────────────────────────────────────────────────────────

class TestTranscript:
    def test_from_dict_basic(self):
        tx = Transcript.from_dict(make_transcript())
        assert tx.call_id == "ACtest001"
        assert tx.status == TranscriptionStatus.COMPLETED
        assert len(tx.dialogue) == 1

    def test_full_text(self):
        tx = Transcript.from_dict(make_transcript())
        assert tx.full_text == "Hello there"

    def test_full_text_multiple_segments(self):
        segs = [make_segment(content=f"Word {i}") for i in range(3)]
        tx = Transcript.from_dict(make_transcript(dialogue=segs))
        for i in range(3):
            assert f"Word {i}" in tx.full_text

    def test_full_text_none_when_no_dialogue(self):
        tx = Transcript.from_dict(make_transcript(dialogue=None))
        assert tx.full_text is None

    def test_render_transcript_with_timestamps_and_speakers(self):
        tx = Transcript.from_dict(make_transcript())
        rendered = tx.render_transcript(include_timestamps=True, include_speakers=True)
        assert "[00:00]" in rendered
        assert "+15559876543:" in rendered
        assert "Hello there" in rendered

    def test_render_transcript_no_timestamps_no_speakers(self):
        tx = Transcript.from_dict(make_transcript())
        rendered = tx.render_transcript(include_timestamps=False, include_speakers=False)
        assert "[" not in rendered
        assert rendered.strip() == "Hello there"

    def test_render_transcript_absent(self):
        tx = Transcript.from_dict(make_transcript(status="absent", dialogue=None))
        rendered = tx.render_transcript()
        assert "absent" in rendered

    def test_roundtrip_dict(self):
        data = make_transcript()
        tx = Transcript.from_dict(data)
        d2 = tx.to_dict()
        assert d2["callId"] == "ACtest001"
        assert d2["status"] == "completed"

    def test_transcription_alias(self):
        """Transcription is a backward-compat alias for Transcript."""
        tx = Transcription.from_dict(make_transcript())
        assert isinstance(tx, Transcript)


# ── Call ──────────────────────────────────────────────────────────────────────

class TestCall:
    def test_from_dict_basic(self):
        c = Call.from_dict(make_call())
        assert c.id == "ACtest001"
        assert c.phone_number_id == "PNtest001"
        assert c.direction == CallDirection.INCOMING
        assert c.status == CallStatus.COMPLETED
        assert c.duration == 90
        assert c.participants == ["+15559876543"]

    def test_direction_outgoing(self):
        c = Call.from_dict(make_call(direction="outgoing"))
        assert c.direction == CallDirection.OUTGOING

    def test_optional_fields_absent(self):
        c = Call.from_dict(make_call())
        assert c.answered_at is None
        assert c.completed_at is None
        assert c.user_id is None

    def test_optional_fields_present(self):
        c = Call.from_dict(make_call(answeredAt=NOW_ISO, completedAt=NOW_ISO2, userId="UStest"))
        assert c.answered_at is not None
        assert c.completed_at is not None
        assert c.user_id == "UStest"

    def test_roundtrip(self):
        data = make_call()
        c = Call.from_dict(data)
        d2 = c.to_dict()
        assert d2["id"] == data["id"]
        assert d2["phoneNumberId"] == data["phoneNumberId"]
        assert d2["status"] == "completed"
        assert d2["direction"] == "incoming"

    def test_all_statuses_parse(self):
        for s in ("queued", "initiated", "ringing", "in-progress", "completed",
                  "busy", "failed", "no-answer", "canceled", "missed",
                  "answered", "forwarded"):
            c = Call.from_dict(make_call(status=s))
            assert c.status.value == s


# ── CallListResult ────────────────────────────────────────────────────────────

class TestCallListResult:
    def test_from_dict_empty(self):
        result = CallListResult.from_dict({"data": [], "totalItems": 0})
        assert result.items == []
        assert result.total_items == 0
        assert result.has_more is False

    def test_from_dict_with_items(self):
        result = CallListResult.from_dict({
            "data": [make_call()],
            "totalItems": 1,
            "nextPageToken": None,
        })
        assert len(result.items) == 1
        assert isinstance(result.items[0], Call)

    def test_has_more_true(self):
        result = CallListResult.from_dict({
            "data": [make_call()],
            "totalItems": 2,
            "nextPageToken": "tok123",
        })
        assert result.has_more is True
        assert result.next_page_token == "tok123"


# ── CallRecording ─────────────────────────────────────────────────────────────

class TestCallRecording:
    def test_from_dict(self):
        r = CallRecording.from_dict(make_recording())
        assert r.id == "RCtest001"
        assert r.status == RecordingStatus.COMPLETED
        assert r.duration == 90
        assert r.url == "https://example.com/recording.mp3"

    def test_optional_url_none(self):
        r = CallRecording.from_dict(make_recording(url=None))
        assert r.url is None

    def test_roundtrip(self):
        data = make_recording()
        r = CallRecording.from_dict(data)
        d2 = r.to_dict()
        assert d2["id"] == data["id"]
        assert d2["status"] == "completed"

    def test_list_result(self):
        result = CallRecordingListResult.from_dict({"data": [make_recording()]})
        assert len(result.items) == 1
        assert isinstance(result.items[0], CallRecording)


# ── CallSummary ───────────────────────────────────────────────────────────────

class TestCallSummary:
    def test_from_dict_basic(self):
        data = {
            "data": {
                "callId": "ACtest001",
                "status": "completed",
                "summary": ["Customer asked about pricing."],
                "nextSteps": ["Send follow-up email."],
                "jobs": None,
            }
        }
        s = CallSummary.from_dict(data)
        assert s.call_id == "ACtest001"
        assert s.status == TranscriptionStatus.COMPLETED
        assert s.summary == ["Customer asked about pricing."]
        assert s.next_steps == ["Send follow-up email."]

    def test_from_dict_absent_status(self):
        data = {"data": {"callId": "ACtest001", "status": "absent"}}
        s = CallSummary.from_dict(data)
        assert s.status == TranscriptionStatus.ABSENT

    def test_to_dict_roundtrip(self):
        data = {
            "data": {
                "callId": "ACtest001",
                "status": "completed",
                "summary": ["Summary line."],
                "nextSteps": None,
                "jobs": None,
            }
        }
        s = CallSummary.from_dict(data)
        d2 = s.to_dict()
        assert d2["callId"] == "ACtest001"
        assert d2["status"] == "completed"
