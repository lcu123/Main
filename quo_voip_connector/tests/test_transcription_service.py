"""
Unit tests for TranscriptionService — mocking the HTTP client.
"""

import pytest
import time
from datetime import datetime
from unittest.mock import MagicMock, patch, call

from quo_voip import QUOConfig, TranscriptionService
from quo_voip.models import (
    Call,
    CallListResult,
    CallRecordingListResult,
    CallSummary,
    Transcript,
    TranscriptionStatus,
)
from quo_voip.exceptions import QUOTranscriptionError

NOW_ISO = "2024-06-01T10:00:00"
NOW_ISO2 = "2024-06-01T10:30:00"


# ── Payload builders ──────────────────────────────────────────────────────────

def _call_payload(**overrides):
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


def _calls_list_payload(items=None, total=None, next_token=None):
    items = items or [_call_payload()]
    return {
        "data": items,
        "totalItems": total if total is not None else len(items),
        "nextPageToken": next_token,
    }


def _transcript_payload(**overrides):
    inner = {
        "callId": "ACtest001",
        "status": "completed",
        "createdAt": NOW_ISO,
        "duration": 90,
        "dialogue": [
            {"content": "Hello customer", "start": 0.0, "end": 2.0, "identifier": "+15559876543"}
        ],
    }
    inner.update(overrides)
    return {"data": inner}


def _summary_payload(**overrides):
    inner = {
        "callId": "ACtest001",
        "status": "completed",
        "summary": ["Customer inquired about pricing."],
        "nextSteps": ["Send pricing sheet."],
        "jobs": None,
    }
    inner.update(overrides)
    return {"data": inner}


def _recordings_payload(items=None):
    items = items or [
        {
            "id": "RCtest001",
            "status": "completed",
            "duration": 90,
            "type": "audio/mpeg",
            "url": "https://example.com/rec.mp3",
            "startTime": NOW_ISO,
        }
    ]
    return {"data": items}


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def config():
    return QUOConfig(api_key="test-key", phone_number_id="PNtest001")


@pytest.fixture
def svc(config):
    svc = TranscriptionService(config)
    svc.client = MagicMock()
    return svc


# ── list_calls ────────────────────────────────────────────────────────────────

class TestListCalls:
    def test_returns_call_list_result(self, svc):
        svc.client.get.return_value = _calls_list_payload()
        result = svc.list_calls(participants=["+15559876543"])
        assert isinstance(result, CallListResult)
        assert len(result.items) == 1
        assert isinstance(result.items[0], Call)

    def test_passes_correct_params(self, svc):
        svc.client.get.return_value = _calls_list_payload()
        svc.list_calls(
            participants=["+15559876543"],
            max_results=25,
        )
        svc.client.get.assert_called_once()
        _, kwargs = svc.client.get.call_args
        params = kwargs["params"]
        assert params["phoneNumberId"] == "PNtest001"
        assert params["participants"] == "+15559876543"
        assert params["maxResults"] == 25

    def test_date_filters_passed(self, svc):
        svc.client.get.return_value = _calls_list_payload()
        after = datetime.fromisoformat("2024-06-01T00:00:00")
        before = datetime.fromisoformat("2024-06-01T23:59:59")
        svc.list_calls(participants=["+15559876543"], created_after=after, created_before=before)
        _, kwargs = svc.client.get.call_args
        params = kwargs["params"]
        assert "createdAfter" in params
        assert "createdBefore" in params

    def test_raises_without_participants(self, svc):
        with pytest.raises(ValueError, match="participants"):
            svc.list_calls()

    def test_raises_without_phone_number_id(self, config):
        config.phone_number_id = None
        svc = TranscriptionService(config)
        svc.client = MagicMock()
        with pytest.raises(ValueError, match="phone_number_id"):
            svc.list_calls(participants=["+15559876543"])

    def test_total_items(self, svc):
        svc.client.get.return_value = _calls_list_payload(total=42)
        result = svc.list_calls(participants=["+15559876543"])
        assert result.total_items == 42

    def test_next_page_token(self, svc):
        svc.client.get.return_value = _calls_list_payload(next_token="tok_abc")
        result = svc.list_calls(participants=["+15559876543"])
        assert result.has_more is True
        assert result.next_page_token == "tok_abc"

    def test_page_token_passed(self, svc):
        svc.client.get.return_value = _calls_list_payload()
        svc.list_calls(participants=["+15559876543"], page_token="tok_xyz")
        _, kwargs = svc.client.get.call_args
        assert kwargs["params"]["pageToken"] == "tok_xyz"

    def test_max_results_clamped(self, svc):
        svc.client.get.return_value = _calls_list_payload()
        svc.list_calls(participants=["+15559876543"], max_results=200)
        _, kwargs = svc.client.get.call_args
        assert kwargs["params"]["maxResults"] == 100

    def test_max_results_min_one(self, svc):
        svc.client.get.return_value = _calls_list_payload()
        svc.list_calls(participants=["+15559876543"], max_results=0)
        _, kwargs = svc.client.get.call_args
        assert kwargs["params"]["maxResults"] == 1


# ── get_call ──────────────────────────────────────────────────────────────────

class TestGetCall:
    def test_returns_call(self, svc):
        svc.client.get.return_value = {"data": _call_payload()}
        c = svc.get_call("ACtest001")
        assert isinstance(c, Call)
        assert c.id == "ACtest001"

    def test_calls_correct_path(self, svc):
        svc.client.get.return_value = {"data": _call_payload()}
        svc.get_call("ACtest001")
        svc.client.get.assert_called_once_with("/v1/calls/ACtest001")


# ── get_transcript ────────────────────────────────────────────────────────────

class TestGetTranscript:
    def test_returns_transcript(self, svc):
        svc.client.get.return_value = _transcript_payload()
        tx = svc.get_transcript("ACtest001")
        assert isinstance(tx, Transcript)
        assert tx.call_id == "ACtest001"
        assert tx.status == TranscriptionStatus.COMPLETED

    def test_calls_correct_path(self, svc):
        svc.client.get.return_value = _transcript_payload()
        svc.get_transcript("ACtest001")
        svc.client.get.assert_called_once_with("/v1/call-transcripts/ACtest001")

    def test_dialogue_parsed(self, svc):
        svc.client.get.return_value = _transcript_payload()
        tx = svc.get_transcript("ACtest001")
        assert len(tx.dialogue) == 1
        assert tx.dialogue[0].content == "Hello customer"

    def test_absent_transcript(self, svc):
        svc.client.get.return_value = _transcript_payload(status="absent", dialogue=None)
        tx = svc.get_transcript("ACtest001")
        assert tx.status == TranscriptionStatus.ABSENT
        assert tx.dialogue is None


# ── get_summary ───────────────────────────────────────────────────────────────

class TestGetSummary:
    def test_returns_call_summary(self, svc):
        svc.client.get.return_value = _summary_payload()
        s = svc.get_summary("ACtest001")
        assert isinstance(s, CallSummary)
        assert s.call_id == "ACtest001"

    def test_calls_correct_path(self, svc):
        svc.client.get.return_value = _summary_payload()
        svc.get_summary("ACtest001")
        svc.client.get.assert_called_once_with("/v1/call-summaries/ACtest001")

    def test_summary_and_next_steps(self, svc):
        svc.client.get.return_value = _summary_payload()
        s = svc.get_summary("ACtest001")
        assert s.summary == ["Customer inquired about pricing."]
        assert s.next_steps == ["Send pricing sheet."]


# ── get_recordings ────────────────────────────────────────────────────────────

class TestGetRecordings:
    def test_returns_recording_list(self, svc):
        svc.client.get.return_value = _recordings_payload()
        result = svc.get_recordings("ACtest001")
        assert isinstance(result, CallRecordingListResult)
        assert len(result.items) == 1

    def test_calls_correct_path(self, svc):
        svc.client.get.return_value = _recordings_payload()
        svc.get_recordings("ACtest001")
        svc.client.get.assert_called_once_with("/v1/call-recordings/ACtest001")

    def test_empty_recordings(self, svc):
        svc.client.get.return_value = {"data": []}
        result = svc.get_recordings("ACtest001")
        assert result.items == []


# ── wait_for_transcript ───────────────────────────────────────────────────────

class TestWaitForTranscript:
    def test_returns_immediately_when_completed(self, svc):
        svc.client.get.return_value = _transcript_payload()
        tx = svc.wait_for_transcript("ACtest001", poll_interval=0.01, timeout=5.0)
        assert tx.status == TranscriptionStatus.COMPLETED

    def test_polls_until_completed(self, svc):
        in_progress = _transcript_payload(status="in-progress", dialogue=None)
        completed = _transcript_payload()
        svc.client.get.side_effect = [in_progress, completed]
        with patch("time.sleep"):
            tx = svc.wait_for_transcript("ACtest001", poll_interval=0.01, timeout=5.0)
        assert tx.status == TranscriptionStatus.COMPLETED
        assert svc.client.get.call_count == 2

    def test_raises_on_failed_status(self, svc):
        svc.client.get.return_value = _transcript_payload(status="failed", dialogue=None)
        with patch("time.sleep"), pytest.raises(QUOTranscriptionError, match="failed"):
            svc.wait_for_transcript("ACtest001", poll_interval=0.01, timeout=5.0)

    def test_raises_on_timeout(self, svc):
        svc.client.get.return_value = _transcript_payload(status="in-progress", dialogue=None)
        with patch("time.sleep"), patch("time.time", side_effect=[0, 0, 10, 10]):
            with pytest.raises(QUOTranscriptionError, match="Timed out"):
                svc.wait_for_transcript("ACtest001", poll_interval=0.01, timeout=5.0)

    def test_on_status_change_callback(self, svc):
        in_progress = _transcript_payload(status="in-progress", dialogue=None)
        completed = _transcript_payload()
        svc.client.get.side_effect = [in_progress, completed]
        statuses = []
        with patch("time.sleep"):
            svc.wait_for_transcript(
                "ACtest001",
                poll_interval=0.01,
                timeout=5.0,
                on_status_change=lambda s: statuses.append(s),
            )
        assert TranscriptionStatus.IN_PROGRESS in statuses
        assert TranscriptionStatus.COMPLETED in statuses


# ── export_text ───────────────────────────────────────────────────────────────

class TestExportText:
    def test_export_with_metadata(self, svc):
        svc.client.get.return_value = _transcript_payload()
        tx = svc.get_transcript("ACtest001")
        text = svc.export_text(tx)
        assert "ACtest001" in text
        assert "Hello customer" in text
        assert "completed" in text

    def test_export_without_metadata(self, svc):
        svc.client.get.return_value = _transcript_payload()
        tx = svc.get_transcript("ACtest001")
        text = svc.export_text(tx, include_metadata=False)
        assert "Call ID" not in text
        assert "Hello customer" in text
