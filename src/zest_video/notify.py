"""Outbound messages: email (Resend), text (Quo) and push (APNs).

Each sender is a small class behind one method, so tests swap in a recorder
and a deployment with a piece unconfigured degrades instead of crashing:
no Quo key means codes go by email only, no APNs key means no push nudge
(the phone still nudges itself with local notifications).
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Config


class DeliveryError(Exception):
    pass


@dataclass
class Email:
    to: str
    subject: str
    html: str
    text: str
    reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    idempotency_key: str = ""
    tag: str = ""


class Mailer:
    """Base: a mailer that is switched off. `send` returns the provider's message id."""

    configured = False
    # True only for a mailer that really puts mail in an inbox. The log mailer is
    # "configured" (codes can be read from the log) but must never count as having
    # emailed a customer.
    delivers = False

    async def send(self, email: Email) -> str:
        raise DeliveryError("email is not set up on this server (set RESEND_API_KEY)")


class ResendMailer(Mailer):
    configured = True
    delivers = True

    def __init__(self, cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.AsyncClient(transport=transport, timeout=20.0)

    async def send(self, email: Email) -> str:
        body: dict[str, Any] = {
            "from": self.cfg.mail_from,
            "to": [email.to],
            "subject": email.subject,
            "html": email.html,
            "text": email.text,
        }
        if email.reply_to:
            body["reply_to"] = email.reply_to
        if email.headers:
            body["headers"] = email.headers
        if email.tag:
            body["tags"] = [{"name": "kind", "value": email.tag}]
        headers = {"Authorization": f"Bearer {self.cfg.resend_api_key}"}
        if email.idempotency_key:
            headers["Idempotency-Key"] = email.idempotency_key
        try:
            resp = await self._http.post("https://api.resend.com/emails", json=body, headers=headers)
        except httpx.TransportError as exc:
            raise DeliveryError(f"email provider unreachable: {exc}") from exc
        if resp.status_code >= 300:
            raise DeliveryError(f"email provider said {resp.status_code}: {resp.text[:300]}")
        return str(resp.json().get("id", ""))


class LogMailer(Mailer):
    """Prints the email's text to the server log instead of sending it. For first-run
    testing before a mail provider exists; the log is only readable by the owner."""

    configured = True

    async def send(self, email: Email) -> str:
        print(f"[mail:log] to={email.to} subject={email.subject!r}\n{email.text}\n", file=sys.stderr, flush=True)
        return f"log-{int(time.time() * 1000)}"


class MemoryMailer(Mailer):
    configured = True
    delivers = True

    def __init__(self) -> None:
        self.sent: list[Email] = []
        self.fail_with: str | None = None

    async def send(self, email: Email) -> str:
        if self.fail_with:
            raise DeliveryError(self.fail_with)
        self.sent.append(email)
        return f"mem-{len(self.sent)}"


def make_mailer(cfg: Config) -> Mailer:
    if cfg.mail_backend == "resend" and cfg.resend_api_key:
        return ResendMailer(cfg)
    if cfg.mail_backend == "log":
        return LogMailer()
    return Mailer()


class Texter:
    """Login codes by text through the office's Quo line. Off unless both vars are set."""

    def __init__(self, cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.AsyncClient(transport=transport, timeout=15.0)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.quo_api_key and self.cfg.quo_from)

    async def send(self, to_digits: str, content: str) -> None:
        if not self.configured:
            raise DeliveryError("texting is not set up on this server")
        try:
            resp = await self._http.post(
                "https://api.quo.com/v1/messages",
                json={"content": content, "from": self.cfg.quo_from, "to": [f"+1{to_digits}"]},
                headers={"Authorization": self.cfg.quo_api_key},
            )
        except httpx.TransportError as exc:
            raise DeliveryError(f"text provider unreachable: {exc}") from exc
        if resp.status_code >= 300:
            raise DeliveryError(f"text provider said {resp.status_code}: {resp.text[:300]}")


class MemoryTexter:
    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def send(self, to_digits: str, content: str) -> None:
        self.sent.append((to_digits, content))


class Pusher:
    """APNs alert pushes, token-authenticated (.p8 key). Returns False when the device
    token is dead so the caller can forget it."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._jwt: tuple[float, str] | None = None
        self._http: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return self.cfg.push_configured

    def _bearer(self) -> str:
        import jwt  # PyJWT; already a dependency of the MCP SDK

        now = time.time()
        if self._jwt and now - self._jwt[0] < 45 * 60:  # APNs wants a fresh one at least hourly
            return self._jwt[1]
        token = jwt.encode(
            {"iss": self.cfg.apns_team_id, "iat": int(now)},
            self.cfg.apns_key_p8,
            algorithm="ES256",
            headers={"kid": self.cfg.apns_key_id},
        )
        self._jwt = (now, token)
        return token

    async def send(self, device_token: str, *, title: str, body: str, sandbox: bool) -> bool:
        if not self.configured:
            raise DeliveryError("push is not set up on this server")
        if self._http is None:
            self._http = httpx.AsyncClient(http2=True, timeout=15.0)
        host = "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"
        try:
            resp = await self._http.post(
                f"https://{host}/3/device/{device_token}",
                json={"aps": {"alert": {"title": title, "body": body}, "sound": "default"}},
                headers={
                    "authorization": f"bearer {self._bearer()}",
                    "apns-topic": self.cfg.apns_bundle_id,
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                },
            )
        except httpx.TransportError as exc:
            raise DeliveryError(f"APNs unreachable: {exc}") from exc
        if resp.status_code == 200:
            return True
        if resp.status_code in (400, 410):  # BadDeviceToken / Unregistered
            return False
        raise DeliveryError(f"APNs said {resp.status_code}: {resp.text[:200]}")


class MemoryPusher:
    configured = True

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, device_token: str, *, title: str, body: str, sandbox: bool) -> bool:
        self.sent.append((device_token, title, body))
        return True
