"""
Unit tests for the webhook server.
"""

import json
import hashlib
import hmac
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime

from webhooks.server import WebhookHandler, WebhookEvent, EVENT_TRANSCRIPTION_COMPLETED
from quo_voip.config import QUOConfig


def _make_event(event_type="transcription.completed", call_id="call_001", txn_id="txn_001"):
    return {
        "event": event_type,
        "id": "evt_001",
        "occurred_at": "2024-06-01T10:00:00",
        "data": {
            "call_id": call_id,
            "transcription_id": txn_id,
        },
    }


@pytest.fixture
def config():
    return QUOConfig(api_key="test-key", webhook_secret="my-secret")


@pytest.fixture
def handler(config):
    h = WebhookHandler(config)
    h._svc = MagicMock()
    return h


class TestWebhookEvent:
    def test_parse_event(self):
        raw = _make_event()
        evt = WebhookEvent(raw)
        assert evt.event_type == "transcription.completed"
        assert evt.call_id == "call_001"
        assert evt.transcription_id == "txn_001"

    def test_repr(self):
        evt = WebhookEvent(_make_event())
        assert "transcription.completed" in repr(evt)


class TestWebhookHandler:
    def test_on_decorator_registers(self, handler):
        called = []

        @handler.on(EVENT_TRANSCRIPTION_COMPLETED)
        def callback(event, transcription=None):
            called.append(event)

        assert EVENT_TRANSCRIPTION_COMPLETED in handler._callbacks
        assert len(handler._callbacks[EVENT_TRANSCRIPTION_COMPLETED]) == 1

    def test_register_programmatic(self, handler):
        cb = MagicMock()
        handler.register(EVENT_TRANSCRIPTION_COMPLETED, cb)
        assert cb in handler._callbacks[EVENT_TRANSCRIPTION_COMPLETED]

    def test_dispatch_calls_callback(self, handler):
        from quo_voip.models import Transcription
        mock_tx = MagicMock(spec=Transcription)
        handler._svc.get.return_value = mock_tx

        called_with = []

        @handler.on(EVENT_TRANSCRIPTION_COMPLETED)
        def cb(event, transcription=None):
            called_with.append((event, transcription))

        evt = WebhookEvent(_make_event())
        handler.dispatch(evt)

        assert len(called_with) == 1
        assert called_with[0][1] == mock_tx

    def test_dispatch_no_handler_no_error(self, handler):
        evt = WebhookEvent(_make_event(event_type="unknown.event"))
        # Should not raise
        handler.dispatch(evt)

    def test_callback_exception_does_not_propagate(self, handler):
        handler._svc.get.side_effect = Exception("API down")

        def bad_cb(event, transcription=None):
            raise RuntimeError("Oops")

        handler.register(EVENT_TRANSCRIPTION_COMPLETED, bad_cb)
        evt = WebhookEvent(_make_event())
        # dispatch should not raise
        handler.dispatch(evt)
