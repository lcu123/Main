"""
QUO VOIP Connector
~~~~~~~~~~~~~~~~~~
A Python connector for the QUO VOIP system that enables pulling
transcription data and integrating with Claude AI.
"""

from .client import QUOClient
from .transcription import TranscriptionService
from .models import Call, Transcription, TranscriptionSegment, Speaker
from .config import QUOConfig

__all__ = [
    "QUOClient",
    "TranscriptionService",
    "Call",
    "Transcription",
    "TranscriptionSegment",
    "Speaker",
    "QUOConfig",
]

__version__ = "1.0.0"
