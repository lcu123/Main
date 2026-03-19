"""
Core HTTP client for the QUO VOIP API.

Handles:
  - Authentication (API key + optional HMAC signing)
  - Automatic retries with exponential back-off
  - Response parsing & error mapping
  - Optional in-memory caching
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import QUOConfig
from .exceptions import (
    QUOAuthError,
    QUONotFoundError,
    QUORateLimitError,
    QUOServerError,
    QUOClientError,
)

logger = logging.getLogger(__name__)

_CACHE: dict[str, tuple[Any, float]] = {}


class QUOClient:
    """
    Low-level HTTP client for the QUO VOIP REST API.

    Example
    -------
    >>> from quo_voip import QUOClient, QUOConfig
    >>> config = QUOConfig(api_key="your-key", account_id="acct_123")
    >>> client = QUOClient(config)
    >>> calls = client.get("/calls", params={"status": "completed"})
    """

    def __init__(self, config: Optional[QUOConfig] = None) -> None:
        self.config = config or QUOConfig.from_env()
        self.config.ensure_valid()

        self._session = self._build_session()

    # ── Public HTTP helpers ───────────────────────────────────────────────────

    def get(
        self,
        path: str,
        params: Optional[dict[str, Any]] = None,
        use_cache: bool = True,
    ) -> Any:
        """Perform a GET request, optionally serving from cache."""
        if self.config.enable_cache and use_cache:
            cache_key = self._cache_key("GET", path, params)
            cached = _CACHE.get(cache_key)
            if cached:
                value, expires_at = cached
                if time.time() < expires_at:
                    logger.debug("Cache hit: %s", cache_key)
                    return value

        response = self._request("GET", path, params=params)

        if self.config.enable_cache and use_cache:
            _CACHE[cache_key] = (response, time.time() + self.config.cache_ttl_seconds)

        return response

    def post(self, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        return self._request("POST", path, json=body)

    def put(self, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        return self._request("PUT", path, json=body)

    def patch(self, path: str, body: Optional[dict[str, Any]] = None) -> Any:
        return self._request("PATCH", path, json=body)

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _build_session(self) -> requests.Session:
        session = requests.Session()

        # Retry on transient errors
        retry = Retry(
            total=self.config.max_retries,
            backoff_factor=self.config.retry_backoff,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        session.headers.update(
            {
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-QUO-SDK": "python-quo-voip-connector/1.0.0",
            }
        )

        if self.config.account_id:
            session.headers["X-QUO-Account-ID"] = self.config.account_id

        return session

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[dict[str, Any]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> Any:
        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))

        # HMAC signature for write operations
        headers: dict[str, str] = {}
        if method in ("POST", "PUT", "PATCH", "DELETE") and self.config.api_secret:
            headers.update(self._sign_request(method, path, json))

        logger.debug("%s %s params=%s", method, url, params)

        try:
            resp = self._session.request(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
                timeout=self.config.timeout,
            )
        except requests.exceptions.ConnectionError as exc:
            raise QUOClientError(f"Connection error: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise QUOClientError(f"Request timed out after {self.config.timeout}s") from exc

        return self._handle_response(resp)

    def _handle_response(self, resp: requests.Response) -> Any:
        logger.debug("Response %s: %s", resp.status_code, resp.url)

        if resp.status_code == 401:
            raise QUOAuthError("Invalid or missing API credentials.")
        if resp.status_code == 403:
            raise QUOAuthError("Forbidden – insufficient permissions.")
        if resp.status_code == 404:
            raise QUONotFoundError(f"Resource not found: {resp.url}")
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After", "unknown")
            raise QUORateLimitError(f"Rate limit exceeded. Retry after: {retry_after}s")
        if resp.status_code >= 500:
            raise QUOServerError(f"QUO server error {resp.status_code}: {resp.text}")
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("message", resp.text)
            except Exception:
                detail = resp.text
            raise QUOClientError(f"Client error {resp.status_code}: {detail}")

        if resp.status_code == 204 or not resp.content:
            return None

        try:
            return resp.json()
        except ValueError:
            return resp.text

    def _sign_request(
        self,
        method: str,
        path: str,
        body: Optional[dict[str, Any]],
    ) -> dict[str, str]:
        """Generate HMAC-SHA256 request signature headers."""
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        payload = f"{timestamp}.{method.upper()}.{path}"
        if body:
            payload += "." + json.dumps(body, separators=(",", ":"), sort_keys=True)

        signature = hmac.new(
            self.config.api_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

        return {
            "X-QUO-Timestamp": timestamp,
            "X-QUO-Signature": signature,
        }

    @staticmethod
    def _cache_key(method: str, path: str, params: Optional[dict]) -> str:
        raw = f"{method}:{path}:{json.dumps(params or {}, sort_keys=True)}"
        return hashlib.md5(raw.encode()).hexdigest()

    def clear_cache(self) -> None:
        """Evict all cached responses."""
        _CACHE.clear()
        logger.debug("Cache cleared")

    def invalidate_cache_for(self, path: str, params: Optional[dict] = None) -> None:
        key = self._cache_key("GET", path, params)
        _CACHE.pop(key, None)
