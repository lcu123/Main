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

from .config import Config, hush


class DeliveryError(Exception):
    """A message that did not go, or may not have. `status` is what the service answered
    with, when it answered at all: a number is safe to put in a log line, where the
    service's own words are not (they may repeat the request)."""

    def __init__(self, *args: Any, status: int | None = None):
        super().__init__(*args)
        self.status = status


class AlreadyTaken(DeliveryError):
    """The mail service refused an email because it already holds one under the same key,
    with other words in it. That proves the key was seen before. It does not prove an
    email was delivered, so by itself it counts as a send that failed. Only a caller that
    knows the rest of the story may read more into it (`videos._deliver`)."""


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
        if resp.status_code == 409 and email.idempotency_key and _already_has_it(resp):
            # "You already gave me an email under this key, and it was not word for word
            # this one." The mail service keeps what it took the first time and takes
            # nothing new. It does not say which id it gave the first one.
            raise AlreadyTaken("the mail service already has an email under this key, with other words in it")
        if resp.status_code >= 300:
            raise DeliveryError(f"email provider said {resp.status_code}: {resp.text[:300]}")
        return str(resp.json().get("id", ""))


def _already_has_it(resp: httpx.Response) -> bool:
    """Is this the mail service's own "that key is used, with a different email"? Only its
    exact name for it counts. Any other 409 (two tries at the very same moment is one) is
    an ordinary failed try and will be made again."""
    try:
        body = resp.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("name") == "invalid_idempotent_request"


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


QUO_MESSAGES = "https://api.quo.com/v1/messages"
QUO_PATIENCE = 15.0  # seconds a text may take; the phone is waiting for its answer meanwhile
# Quo's own "no" to a request, where the field that asks for Done could be the reason. By
# its reference nothing is sent on any of them. 400 is its answer to a request it will not
# take as it is written (422 is the other way an API says that). 403 and 404 are the two
# other refusals its reference has for marking a conversation done. Not here, on purpose:
# 401 (the key), 402 (credits) and 429 (a limit) are never about one field, and after a
# 5xx or no answer at all the text may have gone.
QUO_SAID_NO = (400, 403, 404, 422)


class Texter:
    """Login codes by text through the office's Quo line: a tech's code for the app, and an
    office user's code for the office page (`auth.request_office_text_code`). Off unless
    both vars are set.

    Every text opens a conversation in the office's shared Quo inbox, and the sign-in code
    is the only text this server sends. So the send itself asks Quo to file that
    conversation under Done (`"setInboxStatus": "done"`, in Quo's reference for this very
    call): out of the inbox, history kept, still found by search. It is one field on the
    request the server makes anyway, so it costs no second request and no time, there is
    no conversation to look up, and no other conversation can be touched. QUO_MARK_DONE=off
    leaves the field out.

    Signing in must not get worse for it. If Quo says no to the request (`QUO_SAID_NO`),
    nothing was sent, and the field is the one thing in it that had not been sent before.
    The same text is then sent once more without it, which is the request as it was before
    this existed, and one line in the log says the conversation is open in the inbox. On
    any other answer, and on no answer at all, nothing is sent a second time: the first
    may have gone, and a tech must never get two codes. The phone is waiting for its
    answer all the while, so the second try gets only the time the refusal left over:
    the two together wait no longer than the one did before.

    A tech's text that failed is not passed on by its caller (`auth._deliver_code`: the code
    still goes by email), so nobody would see it if the field were what Quo choked on in a
    way that is not tried again. So such a failure is one line in the log, with the switch
    that sends the text as it went before. It is said only when that switch could matter:
    the field was on the request, and the same text without it was not tried.

    The office page tells its user when his text failed, and writes a line of its own to
    the log every time (`auth.request_office_text_code`): the owner it happens to cannot
    sign in to read the audit trail. So a failed office text can have two lines here, this
    class's about the Done switch and that one. `DeliveryError.status` is what Quo
    answered, for that line."""

    def __init__(self, cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self._http = httpx.AsyncClient(transport=transport, timeout=QUO_PATIENCE)

    @property
    def configured(self) -> bool:
        return bool(self.cfg.quo_api_key and self.cfg.quo_from)

    async def _post(self, message: dict[str, Any], patience: float = QUO_PATIENCE) -> httpx.Response:
        try:
            return await self._http.post(
                QUO_MESSAGES, json=message, headers={"Authorization": self.cfg.quo_api_key}, timeout=patience
            )
        except httpx.TransportError as exc:
            raise DeliveryError(f"text provider unreachable: {exc}") from exc

    def _say(self, line: str) -> None:
        print(f"zest-video: {hush(line, self.cfg)}", file=sys.stderr, flush=True)

    def _say_it_failed(self, to_digits: str, *, answered: int | None = None, silence: str = "") -> None:
        """One line for a code text that asked for Done, failed and was not tried without
        it. Only what Quo did (a status, or the kind of silence) and the last four digits:
        Quo's own words about a request may repeat it, and it holds the code. It does not
        say the text did not go: after a lost answer nobody knows."""
        text = f"a sign-in code text for the number ending {to_digits[-4:]}"
        what = f"Quo did not answer {text} ({silence})" if answered is None else f"Quo answered {answered} to {text}"
        self._say(
            f"{what}, and it was not sent again. The request asked Quo to file the conversation under Done "
            f"(setInboxStatus). If no code text has gone through since the server began asking that, set "
            f"QUO_MARK_DONE=off on zest-video: the text then goes exactly as it went before. A code sent by "
            f"email is not affected."
        )

    async def send(self, to_digits: str, content: str) -> None:
        if not self.configured:
            raise DeliveryError("texting is not set up on this server")
        message: dict[str, Any] = {"content": content, "from": self.cfg.quo_from, "to": [f"+1{to_digits}"]}
        if self.cfg.quo_mark_done:
            started = time.monotonic()
            try:
                resp = await self._post({**message, "setInboxStatus": "done"})
            except DeliveryError as exc:
                # The kind of silence by its class name only (ReadTimeout, ConnectError).
                self._say_it_failed(to_digits, silence=type(exc.__cause__ or exc).__name__)
                raise
            if resp.status_code in QUO_SAID_NO:
                refused = resp.status_code
                resp = await self._post(message, max(1.0, QUO_PATIENCE - (time.monotonic() - started)))
                if resp.status_code < 300:
                    # Only the status and the last four digits: Quo's own words about a
                    # refused request may repeat the request, and that holds the code.
                    self._say(
                        f"Quo would not file a sign-in code text under Done (it answered {refused} to "
                        f"setInboxStatus). The code was texted without it, so the conversation with the number "
                        f"ending {to_digits[-4:]} is open in the Quo inbox. Signing in is not affected."
                    )
            elif resp.status_code >= 300:
                self._say_it_failed(to_digits, answered=resp.status_code)
        else:
            resp = await self._post(message)
        if resp.status_code >= 300:
            raise DeliveryError(f"text provider said {resp.status_code}: {resp.text[:300]}", status=resp.status_code)


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
