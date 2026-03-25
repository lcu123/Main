"""
Unit tests for QUOConfig.
"""

import os
import pytest
from quo_voip.config import QUOConfig


class TestQUOConfig:
    def test_defaults(self):
        config = QUOConfig(api_key="test")
        assert config.base_url == "https://api.openphone.com"
        assert config.timeout == 30
        assert config.max_retries == 3
        assert config.enable_cache is True

    def test_validate_missing_key(self):
        config = QUOConfig(api_key="")
        errors = config.validate()
        assert any("QUO_API_KEY" in e for e in errors)

    def test_validate_ok(self):
        config = QUOConfig(api_key="real-key", phone_number_id="PNtest001")
        assert config.validate() == []

    def test_ensure_valid_raises(self):
        config = QUOConfig(api_key="")
        with pytest.raises(ValueError, match="QUO_API_KEY"):
            config.ensure_valid()

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("QUO_API_KEY", "env-key")
        monkeypatch.setenv("OPENPHONE_PHONE_NUMBER_ID", "PN123abc")
        config = QUOConfig.from_env()
        assert config.api_key == "env-key"
        assert config.phone_number_id == "PN123abc"

    def test_repr_masks_key(self):
        config = QUOConfig(api_key="abcdefgh")
        assert "abcd****" in repr(config)
        assert "efgh" not in repr(config)

    def test_custom_model(self):
        config = QUOConfig(api_key="k", claude_model="claude-opus-4-6")
        assert config.claude_model == "claude-opus-4-6"
