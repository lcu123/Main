"""Tokens, one-time codes and rate limits.

Nothing here is reversible on purpose: device tokens, office sessions and
login codes are stored only as SHA-256 hashes, so a leaked database cannot
sign anyone in.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict, deque

CODE_MINUTES = 15
CODE_MAX_TRIES = 5


def new_token() -> str:
    """256 bits, for device tokens and office sessions."""
    return secrets.token_urlsafe(32)


def new_watch_token() -> str:
    """The customer link: a random 128-bit token (32 hex chars), never an ID that counts up."""
    return secrets.token_hex(16)


def new_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def code_digest(subject: str, code: str) -> str:
    """Bound to its subject so a code for one tech can never match another's row."""
    return digest(f"{subject}:{code}")


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def csrf_token(secret: str, session_token: str) -> str:
    return hmac.new(secret.encode(), f"csrf:{session_token}".encode(), hashlib.sha256).hexdigest()


def normalize_phone(raw: str) -> str:
    """US numbers down to their last 10 digits; '' when it is not a phone number."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def normalize_contact(raw: str) -> tuple[str, str]:
    """Return ('email', addr) or ('phone', digits) or ('', '')."""
    raw = (raw or "").strip()
    if "@" in raw:
        return "email", raw.lower()
    phone = normalize_phone(raw)
    return ("phone", phone) if phone else ("", "")


def mask_email(addr: str) -> str:
    name, _, domain = addr.partition("@")
    if not domain:
        return "***"
    return f"{name[:1]}***@{domain}"


class RateLimiter:
    """In-memory sliding window per key. One process serves this app, so memory is enough;
    the login-code lockout that really matters is in the database (tries per code)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window: float) -> bool:
        return self.retry_after(key, limit, window) == 0

    def retry_after(self, key: str, limit: int, window: float) -> int:
        """0 when allowed (and the hit is counted), else seconds until the next try."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] >= window:
            hits.popleft()
        if len(hits) >= limit:
            return max(1, int(window - (now - hits[0])) + 1)
        hits.append(now)
        if len(self._hits) > 20000:  # a flood of unique keys must not grow memory forever
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= 3600]:
                del self._hits[k]
        return 0

    def reset(self) -> None:
        self._hits.clear()
