"""
Unit tests for TranscriptionService – mocking the HTTP client.
"""

import json
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from quo_voip import QUOConfig, TranscriptionService
from quo_voip.models import Transcription, TranscriptionStatus, Call, CallStatus

NOW_ISO = "2024-06-01T10:00:00"
NOW_ISO2 = "2024-06-01T10:30:00"


def _tx_payload(**overrides):
    base = {
        "id": "txn_001",
        "call_id": "call_001",
        "status": "completed",
        "language": "en",
        "segments": [
            {
                "id": "seg1",
                "speaker_id": "sp1",
                "text": "Hello customer",
                "start_time": 0.0,
                "end_time": 2.0,
                "confidence": 0.95,
            }
        ],
        "speakers": [
            {"id": "sp1", "name": "Agent", "phone_number": None, "role": "agent"}
        ],
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO2,
    }
    base.update(overrides)
    return base


def _list_payload(items=None):
    items = items or [_tx_payload()]
    return {
        "items": items,
        "total": len(items),
        "page": 1,
        "page_size": 20,
        "has_more": False,
    }


@pytest.fixture
def config():
    return QUOConfig(api_key="test-key", base_url="https://api.quo.test/v1")


@pytest.fixture
def svc(config):
    svc = TranscriptionService(config)
    svc.client = MagicMock()
    return svc


class TestGetTranscription:
    def test_get_returns_transcription(self, svc):
        svc.client.get.return_value = _tx_payload()
        tx = svc.get("txn_001")
        assert isinstance(tx, Transcription)
        assert tx.id == "txn_001"
        svc.client.get.assert_called_once_with("/transcriptions/txn_001")

    def test_get_for_call(self, svc):
        svc.client.get.return_value = _tx_payload()
        tx = svc.get_for_call("call_001")
        assert tx.call_id == "call_001"
        svc.client.get.assert_called_once_with("/calls/call_001/transcription")


class TestListTranscriptions:
    def test_list_basic(self, svc):
        svc.client.get.return_value = _list_payload()
        result = svc.list()
        assert result.total == 1
        assert len(result.items) == 1
        assert isinstance(result.items[0], Transcription)

    def test_list_with_filters(self, svc):
        svc.client.get.return_value = _list_payload()
        svc.list(
            status=TranscriptionStatus.COMPLETED,
            language="en",
            call_direction="inbound",
        )
        call_params = svc.client.get.call_args[1]["params"]
        assert call_params["status"] == "completed"
        assert call_params["language"] == "en"
        assert call_params["direction"] == "inbound"

    def test_iter_all_single_page(self, svc):
        svc.client.get.return_value = _list_payload()
        items = list(svc.iter_all())
        assert len(items) == 1

    def test_iter_all_multiple_pages(self, svc):
        page1 = {**_list_payload(), "has_more": True}
        page2 = _list_payload([_tx_payload(id="txn_002", call_id="call_002")])
        svc.client.get.side_effect = [page1, page2]
        items = list(svc.iter_all())
        assert len(items) == 2


class TestSearch:
    def test_search(self, svc):
        svc.client.get.return_value = _list_payload()
        result = svc.search("hello")
        svc.client.get.assert_called_once()
        call_params = svc.client.get.call_args[1]["params"]
        assert call_params["q"] == "hello"

    def test_search_local_match(self, svc):
        svc.client.get.return_value = _tx_payload()
        tx = Transcription.from_dict(_tx_payload())
        results = svc.search_local([tx], "hello")
        assert len(results) == 1
        assert results[0][0].id == "txn_001"

    def test_search_local_no_match(self, svc):
        tx = Transcription.from_dict(_tx_payload())
        results = svc.search_local([tx], "nonexistent_xyz")
        assert results == []

    def test_search_local_case_insensitive(self, svc):
        tx = Transcription.from_dict(_tx_payload())
        results = svc.search_local([tx], "HELLO")
        assert len(results) == 1


class TestExport:
    def test_export_json_single(self, svc):
        tx = Transcription.from_dict(_tx_payload())
        output = svc.export_json(tx)
        data = json.loads(output)
        assert data["id"] == "txn_001"

    def test_export_json_list(self, svc):
        tx = Transcription.from_dict(_tx_payload())
        output = svc.export_json([tx, tx])
        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) == 2

    def test_export_csv_headers(self, svc):
        tx = Transcription.from_dict(_tx_payload())
        csv_output = svc.export_csv([tx])
        assert "call_id" in csv_output
        assert "Hello customer" in csv_output

    def test_export_text(self, svc):
        tx = Transcription.from_dict(_tx_payload())
        text = svc.export_text(tx)
        assert "call_001" in text
        assert "Hello customer" in text

    def test_export_text_no_metadata(self, svc):
        tx = Transcription.from_dict(_tx_payload())
        text = svc.export_text(tx, include_metadata=False)
        assert "Call ID" not in text
        assert "Hello customer" in text


class TestFilterSentiment:
    def test_filter_positive(self, svc):
        from quo_voip.models import SentimentLabel
        payload = _tx_payload(segments=[
            {
                "id": "s1", "speaker_id": "sp1",
                "text": "Great!", "start_time": 0.0, "end_time": 1.0,
                "confidence": 1.0, "sentiment": "positive",
            }
        ])
        tx = Transcription.from_dict(payload)
        result = svc.filter_by_sentiment([tx], "positive")
        assert len(result) == 1

    def test_filter_below_threshold(self, svc):
        payload = _tx_payload(segments=[
            {"id": "s1", "speaker_id": "sp1", "text": "Bad!", "start_time": 0.0,
             "end_time": 1.0, "confidence": 1.0, "sentiment": "negative"},
            {"id": "s2", "speaker_id": "sp1", "text": "Ok.", "start_time": 1.0,
             "end_time": 2.0, "confidence": 1.0, "sentiment": "positive"},
        ])
        tx = Transcription.from_dict(payload)
        # Only 50% positive; threshold=0.8 → excluded
        result = svc.filter_by_sentiment([tx], "positive", threshold=0.8)
        assert result == []
