"""
OpenPhone connector for Claude / MCP.
"""

from .client import QUOClient
from .config import QUOConfig
from .models import (
    Call,
    CallDirection,
    CallListResult,
    CallRecording,
    CallRecordingListResult,
    CallStatus,
    CallSummary,
    Transcript,
    TranscriptDialogueSegment,
    TranscriptionStatus,
    # legacy alias kept for any code that imports Transcription by name
    Transcription,
)
from .transcription import TranscriptionService

__all__ = [
    "QUOClient",
    "QUOConfig",
    "TranscriptionService",
    "Call",
    "CallDirection",
    "CallListResult",
    "CallRecording",
    "CallRecordingListResult",
    "CallStatus",
    "CallSummary",
    "Transcript",
    "TranscriptDialogueSegment",
    "TranscriptionStatus",
    "Transcription",
]

__version__ = "1.0.0"
