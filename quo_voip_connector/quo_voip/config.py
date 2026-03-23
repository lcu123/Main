"""
Configuration management for the QUO VOIP connector.
Supports env vars, .env files, and programmatic config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


@dataclass
class QUOConfig:
    """
    Central configuration for the QUO VOIP connector.

    Priority order (highest to lowest):
      1. Values passed directly to the constructor
      2. Environment variables
      3. Defaults
    """

    # ── QUO VOIP API ──────────────────────────────────────────────────────────
    api_key: str = field(default_factory=lambda: os.environ.get("QUO_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: os.environ.get("QUO_API_SECRET", ""))
    base_url: str = field(
        default_factory=lambda: os.environ.get("QUO_BASE_URL", "https://api.openphone.com")
    )
    # OpenPhone phone number ID (PN...) used as a default filter for call listing
    phone_number_id: Optional[str] = field(
        default_factory=lambda: os.environ.get("OPENPHONE_PHONE_NUMBER_ID")
    )

    # ── HTTP behaviour ────────────────────────────────────────────────────────
    timeout: int = field(
        default_factory=lambda: int(os.environ.get("QUO_TIMEOUT", "30"))
    )
    max_retries: int = field(
        default_factory=lambda: int(os.environ.get("QUO_MAX_RETRIES", "3"))
    )
    retry_backoff: float = field(
        default_factory=lambda: float(os.environ.get("QUO_RETRY_BACKOFF", "1.5"))
    )

    # ── Claude / Anthropic ────────────────────────────────────────────────────
    anthropic_api_key: str = field(
        default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", "")
    )
    claude_model: str = field(
        default_factory=lambda: os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
    )

    # ── Webhook server ────────────────────────────────────────────────────────
    webhook_host: str = field(
        default_factory=lambda: os.environ.get("QUO_WEBHOOK_HOST", "0.0.0.0")
    )
    webhook_port: int = field(
        default_factory=lambda: int(os.environ.get("QUO_WEBHOOK_PORT", "8080"))
    )
    webhook_secret: Optional[str] = field(
        default_factory=lambda: os.environ.get("QUO_WEBHOOK_SECRET")
    )

    # ── Data / cache ──────────────────────────────────────────────────────────
    cache_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("QUO_CACHE_DIR", str(Path.home() / ".quo_voip" / "cache"))
        )
    )
    cache_ttl_seconds: int = field(
        default_factory=lambda: int(os.environ.get("QUO_CACHE_TTL", "300"))
    )
    enable_cache: bool = field(
        default_factory=lambda: os.environ.get("QUO_ENABLE_CACHE", "true").lower() == "true"
    )

    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = OK)."""
        errors: list[str] = []
        if not self.api_key:
            errors.append("QUO_API_KEY is required")
        if not self.base_url:
            errors.append("QUO_BASE_URL is required")
        return errors

    def ensure_valid(self) -> None:
        errors = self.validate()
        if errors:
            raise ValueError("QUO Config errors:\n  " + "\n  ".join(errors))

    @classmethod
    def from_env(cls) -> "QUOConfig":
        """Create a config from environment variables (convenience factory)."""
        return cls()

    def __repr__(self) -> str:
        masked_key = f"{self.api_key[:4]}****" if self.api_key else "(not set)"
        return (
            f"QUOConfig(base_url={self.base_url!r}, "
            f"api_key={masked_key}, "
            f"phone_number_id={self.phone_number_id!r})"
        )
