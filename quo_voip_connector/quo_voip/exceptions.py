"""
Exception hierarchy for the QUO VOIP connector.
"""


class QUOError(Exception):
    """Base exception for all QUO VOIP connector errors."""


class QUOClientError(QUOError):
    """4xx errors raised by the client (bad request, etc.)."""


class QUOAuthError(QUOClientError):
    """Authentication / authorization failure (401, 403)."""


class QUONotFoundError(QUOClientError):
    """Resource not found (404)."""


class QUORateLimitError(QUOClientError):
    """Rate limit exceeded (429)."""


class QUOServerError(QUOError):
    """5xx errors from the QUO server."""


class QUOWebhookError(QUOError):
    """Errors related to webhook signature validation or processing."""


class QUOTranscriptionError(QUOError):
    """Errors specific to transcription retrieval or processing."""
