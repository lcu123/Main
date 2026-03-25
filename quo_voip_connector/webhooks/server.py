"""
Webhook listener for real-time QUO VOIP events.

QUO pushes events to your endpoint when:
  - A call starts / ends
  - A transcription completes
  - A voicemail is left

Run with:
    python -m webhooks.server
    # or
    quo-voip webhook-server --port 8080
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

from quo_voip.config import QUOConfig
from quo_voip.models import Call, Transcription
from quo_voip.transcription import TranscriptionService

logger = logging.getLogger(__name__)

# Event type constants
EVENT_CALL_STARTED = "call.started"
EVENT_CALL_ENDED = "call.ended"
EVENT_TRANSCRIPTION_COMPLETED = "transcription.completed"
EVENT_TRANSCRIPTION_FAILED = "transcription.failed"
EVENT_VOICEMAIL_RECEIVED = "voicemail.received"


class WebhookEvent:
    """Parsed QUO VOIP webhook event."""

    def __init__(self, raw: dict) -> None:
        self.event_type: str = raw.get("event", "unknown")
        self.event_id: str = raw.get("id", "")
        self.occurred_at: datetime = datetime.fromisoformat(
            raw.get("occurred_at", datetime.now(timezone.utc).isoformat())
        )
        self.payload: dict = raw.get("data", {})
        self._raw = raw

    @property
    def call_id(self) -> Optional[str]:
        return self.payload.get("call_id") or self.payload.get("id")

    @property
    def transcription_id(self) -> Optional[str]:
        return self.payload.get("transcription_id")

    def __repr__(self) -> str:
        return f"WebhookEvent(type={self.event_type!r}, id={self.event_id!r})"


class WebhookHandler:
    """
    Register callbacks for QUO VOIP webhook events.

    Example
    -------
    >>> handler = WebhookHandler(config)
    >>> @handler.on(EVENT_TRANSCRIPTION_COMPLETED)
    ... def on_transcript(event, transcription):
    ...     print(transcription.render_transcript())
    >>> handler.serve()
    """

    def __init__(self, config: Optional[QUOConfig] = None) -> None:
        self.config = config or QUOConfig.from_env()
        self._svc = TranscriptionService(self.config)
        self._callbacks: dict[str, list[Callable]] = {}

    # ── Decorator / registration ──────────────────────────────────────────────

    def on(self, event_type: str) -> Callable:
        """Decorator to register a callback for a specific event type."""
        def decorator(func: Callable) -> Callable:
            self._callbacks.setdefault(event_type, []).append(func)
            return func
        return decorator

    def register(self, event_type: str, callback: Callable) -> None:
        """Programmatically register a callback."""
        self._callbacks.setdefault(event_type, []).append(callback)

    # ── Event dispatching ─────────────────────────────────────────────────────

    def dispatch(self, event: WebhookEvent) -> None:
        """Dispatch a parsed event to all registered callbacks."""
        handlers = self._callbacks.get(event.event_type, [])
        if not handlers:
            logger.debug("No handlers for event: %s", event.event_type)
            return

        # Auto-hydrate rich objects where useful
        extra_args: tuple = ()
        if event.event_type == EVENT_TRANSCRIPTION_COMPLETED and event.transcription_id:
            try:
                tx = self._svc.get_transcript(event.transcription_id)
                extra_args = (tx,)
            except Exception as exc:
                logger.warning("Could not fetch transcription %s: %s", event.transcription_id, exc)
        elif event.event_type in (EVENT_CALL_STARTED, EVENT_CALL_ENDED) and event.call_id:
            try:
                call = self._svc.get_call(event.call_id)
                extra_args = (call,)
            except Exception as exc:
                logger.warning("Could not fetch call %s: %s", event.call_id, exc)

        for callback in handlers:
            try:
                callback(event, *extra_args)
            except Exception as exc:
                logger.error("Callback %s raised: %s", callback.__name__, exc, exc_info=True)

    # ── HTTP server ───────────────────────────────────────────────────────────

    def serve(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        blocking: bool = True,
    ) -> Optional[HTTPServer]:
        """Start the webhook HTTP server."""
        host = host or self.config.webhook_host
        port = port or self.config.webhook_port

        secret = self.config.webhook_secret
        dispatcher = self.dispatch

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                logger.info(fmt, *args)

            def do_POST(self):
                if self.path not in ("/webhook", "/webhook/"):
                    self.send_response(404)
                    self.end_headers()
                    return

                length = int(self.headers.get("Content-Length", 0))
                raw_body = self.rfile.read(length)

                # Signature verification
                if secret:
                    sig_header = self.headers.get("X-QUO-Signature", "")
                    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
                    if not hmac.compare_digest(sig_header, expected):
                        logger.warning("Webhook signature mismatch – rejecting request")
                        self.send_response(403)
                        self.end_headers()
                        return

                try:
                    payload = json.loads(raw_body)
                    event = WebhookEvent(payload)
                    logger.info("Received webhook event: %s", event)
                    # Dispatch in background thread to keep response fast
                    threading.Thread(
                        target=dispatcher, args=(event,), daemon=True
                    ).start()
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"ok": true}')
                except Exception as exc:
                    logger.error("Webhook parse error: %s", exc, exc_info=True)
                    self.send_response(400)
                    self.end_headers()

            def do_GET(self):
                # Health-check endpoint
                if self.path in ("/health", "/ping"):
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"status": "ok", "service": "quo-voip-webhook"}')
                else:
                    self.send_response(404)
                    self.end_headers()

        server = HTTPServer((host, port), _Handler)
        logger.info("QUO VOIP webhook server listening on http://%s:%d/webhook", host, port)

        if blocking:
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                logger.info("Webhook server stopped")
                server.shutdown()
        else:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server
