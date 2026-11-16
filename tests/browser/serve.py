#!/usr/bin/env python3
"""The customer side of Zest Visit Video, served for real browsers to test.

    python tests/browser/serve.py --port 19300 [--link-seconds N] [--bucket https|same-origin]

What runs:

* the REAL server (src/zest_video, unchanged) on http://127.0.0.1:PORT, with FieldRoutes,
  mail, text and push faked exactly as the pytest suite fakes them (tests/video_helpers.py);
* a stand-in bucket that behaves like the private S3-compatible bucket in production: signed
  links that really expire, byte ranges, ETag, 403 XML once a link is too old, no CORS headers.
  With --bucket https (the default) it is on ANOTHER ORIGIN, https://127.0.0.1:PORT+1, with a
  throwaway certificate, because the watch page's CSP only allows media from 'self' and https:.
  With --bucket same-origin the objects are served by the web server itself under /__bucket/,
  for clients that cannot be told to accept a throwaway certificate (real Safari, a real phone
  on the same Wi-Fi).

It prints ONE JSON line when it is ready (the same thing GET /__test/info returns), then serves
until it is stopped.

A control API, added by wrapping the app HERE and never in src/:

    GET  /__test/health                 "ok" once both servers are up
    GET  /__test/info                   the URLs: watch, watch2, dead, unknown, unsubscribe, ...
    POST /__test/video?...              film a brand new video; returns its watch link. Options:
           file=sample | moov-at-end | stretched | stretched-moov-at-end | cut-short
           throttle=BYTES_PER_SECOND    the bucket trickles this object (a slow connection)
           first_byte_ms=N              the bucket waits this long before the first byte
           link_seconds=N               this video's signed links live N seconds (default 900),
                                        and its watch page is told so (data-life), as a server
                                        set to N seconds would tell it
           address=TEXT                 the address shown in the heading
           pulled=1                     pull it straight away (a dead link)
           refusal=visible              how the bucket words its 403 (see "The refusal" below)
    GET  /__test/state?token=TOKEN      what the server recorded for that link: was "played"
                                        posted, the score, was it pulled, every request the web
                                        server and the bucket got for it (with Range headers)
    GET  /__test/email?token=TOKEN      the HTML email that was "sent" for watch / watch2
    POST /__test/expire?token=TOKEN     every signed link handed out so far for it is now dead
    POST /__test/pull?token=TOKEN       the office takes the video down, now (the link answers 410)
    POST /__test/stall?token=TOKEN&on=1 the bucket stops sending this object in mid-answer, as when
                                        a phone loses its signal; on=0 lets it flow again. Only for
                                        a video filmed with throttle= (those are sent in pieces).
           from=BYTES                   only the file from this byte on is held back: what lies
                                        before it still arrives. Set before the page is opened,
                                        the signal "drops" at the same place in every browser,
                                        however much of the file that browser asks for at once.

The refusal. A real S3-compatible bucket refuses an expired link with status 403 and a body that
starts `<?xml version="1.0" encoding="UTF-8"?><Error>...`, sent as application/xml. That is the
default here, and it matters: Chromium will not hand a cross-origin XML answer to a <video>
(net::ERR_BLOCKED_BY_ORB), so the player is never told "403": it sees a network failure and
tries the same dead link again and again for about half a minute before it gives up.
refusal=visible leaves the XML declaration off, which Chromium does pass on; the player then
fails at once. It exists to tell the page's own recovery apart from the browser's waiting.
(Checked against the production bucket on 2026-11-14 with a link whose signature was spoiled:
403, Content-Type application/xml, and a body that begins with the XML declaration. The
default here is what the real bucket sends.)

The fixtures (tests/browser/fixtures):

* sample.mp4: H.264 High, 720x1280, 30 fps, yuv420p, AAC-LC mono, 8 s, 142 KB, moov atom at
  the FRONT (faststart), the layout the iPhone app exports. A made-up "Sample video" card.
* sample-moov-at-end.mp4: the same streams with the moov atom at the END, which is what a
  recorder writes when nobody asks for faststart. Made with
      ffmpeg -i sample.mp4 -c copy -map 0 -movflags -faststart sample-moov-at-end.mp4
* "cut-short" is the stretched file with its last 45% missing, cut in the middle of a frame:
  an upload that broke off, or a file damaged in the bucket. The index at the front still
  promises all 8 seconds. Made here at start-up, like the stretched files.
* "stretched" files are made here at start-up, in memory, with no ffmpeg: zero bytes are put
  between the media chunks and the chunk offset tables are moved to match, so the 142 KB clip
  becomes a 2.7 MB file with the bit rate of a real visit video (about 5 MB for 15 s). A browser
  cannot swallow that in one gulp, so it has to make the same later range requests it makes in
  production. That is what the expiring-link and slow-link tests need.

Rate limits: the real server allows 120 page opens a minute per IP address (and 600 requests
from the player). Forty browsers tested in parallel from 127.0.0.1 are one "customer" to it, so
the limiter is switched off for them unless --rate-limit real is given. It stays REAL for any
client that says it comes from 198.18.0.0/15 (the address block set aside for testing, RFC
2544) in an X-Forwarded-For header, which is where the real server reads the address from
behind Railway's edge. A test that wants to hit the limit picks an address of its own in that
block and disturbs nobody else.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
FIXTURES = HERE / "fixtures"
sys.path.insert(0, str(REPO / "tests"))  # video_helpers.py and conftest.py live there
if (REPO / "src").is_dir():
    sys.path.insert(1, str(REPO / "src"))  # works without `pip install -e .` too

for _k, _v in {"FR_SUBDOMAIN": "zest", "FR_AUTH_KEY": "k", "FR_AUTH_TOKEN": "t", "FR_OFFICE_ID": "7"}.items():
    os.environ.setdefault(_k, _v)
os.environ.pop("VIDEO_TEST_DATABASE_URL", None)  # always a throwaway SQLite file

import sqlalchemy as sa  # noqa: E402
import uvicorn  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import video_helpers as vh  # noqa: E402
from zest_video import db, videos  # noqa: E402
from zest_video.app import build_app  # noqa: E402
from zest_video.security import RateLimiter  # noqa: E402
from zest_video.storage import MemoryStorage  # noqa: E402

SIGNING_KEY = b"local-bucket-key"
POOL_FIRST_CUSTOMER = 1000
STRETCH_TO = 2_700_000  # 8 s at the bit rate of a real visit video (about 5 MB for 15 s)
STREETS = ["123 Main St", "9 Oak Ct", "77 Elm St", "4410 Greenback Ln", "18 Sutter Creek Rd"]


# ---------------------------------------------------------------------------------------------
# MP4: read the box layout, and make a production-sized file out of the small sample
# ---------------------------------------------------------------------------------------------

def top_boxes(data: bytes) -> list[tuple[str, int, int]]:
    """(type, offset, size) of every top-level box."""
    out, i = [], 0
    while i + 8 <= len(data):
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        if size == 1:
            size = struct.unpack(">Q", data[i + 8:i + 16])[0]
        elif size == 0:
            size = len(data) - i
        out.append((kind.decode("latin-1"), i, size))
        i += size
    return out


def moov_first(data: bytes) -> bool:
    order = [kind for kind, _, _ in top_boxes(data) if kind in ("moov", "mdat")]
    return order[:1] == ["moov"]


def _chunk_tables(data: bytes, start: int, end: int) -> list[tuple[int, int, int]]:
    """Every stco/co64 table inside [start, end): (position of the first entry, entries, width)."""
    found, i = [], start
    while i + 8 <= end:
        size, kind = struct.unpack(">I4s", data[i:i + 8])
        if size < 8:
            break
        if kind in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
            found += _chunk_tables(data, i + 8, i + size)
        elif kind in (b"stco", b"co64"):
            count = struct.unpack(">I", data[i + 12:i + 16])[0]
            found.append((i + 16, count, 4 if kind == b"stco" else 8))
        i += size
    return found


def stretch(data: bytes, target: int) -> bytes:
    """The same video, `target` bytes long: zero bytes between the media chunks (inside mdat,
    where unreferenced bytes are legal) and every chunk offset moved to match."""
    boxes = top_boxes(data)
    moov = next(b for b in boxes if b[0] == "moov")
    mdat = next(b for b in boxes if b[0] == "mdat")
    tables = _chunk_tables(data, moov[1], moov[1] + moov[2])
    entries = []  # (old offset, position of the table entry, width)
    for first, count, width in tables:
        for n in range(count):
            pos = first + n * width
            entries.append((int.from_bytes(data[pos:pos + width], "big"), pos, width))
    chunks = sorted({e[0] for e in entries})
    assert chunks and mdat[1] < chunks[0] and chunks[-1] < mdat[1] + mdat[2], "chunk offsets must point into mdat"
    gap = max(0, (target - len(data)) // max(1, len(chunks) - 1))
    shift = {offset: n * gap for n, offset in enumerate(chunks)}  # nothing before the first chunk
    added = gap * (len(chunks) - 1)

    patched = bytearray(data)
    for old, pos, width in entries:
        patched[pos:pos + width] = (old + shift[old]).to_bytes(width, "big")
    patched[mdat[1]:mdat[1] + 4] = struct.pack(">I", mdat[2] + added)

    out, cursor = bytearray(), 0
    for offset in chunks[1:]:
        out += patched[cursor:offset]
        out += bytes(gap)
        cursor = offset
    out += patched[cursor:]
    return bytes(out)


def chunk_offsets(data: bytes) -> list[int]:
    """Where every media chunk starts, in file order."""
    moov = next(b for b in top_boxes(data) if b[0] == "moov")
    out = set()
    for first, count, width in _chunk_tables(data, moov[1], moov[1] + moov[2]):
        out.update(int.from_bytes(data[first + n * width:first + (n + 1) * width], "big") for n in range(count))
    return sorted(out)


def cut_short(data: bytes, share: float) -> bytes:
    """`data` with its end missing: the file stops ten bytes into the first frame that starts
    after `share` of the way in. (A cut between two frames reads as a clean, early end; a cut
    inside one is what a broken upload or a damaged object looks like to a decoder.)"""
    at = next(o for o in chunk_offsets(data) if o >= len(data) * share)
    return data[:at + 10]


def load_files() -> dict[str, bytes]:
    sample = (FIXTURES / "sample.mp4").read_bytes()
    tail = (FIXTURES / "sample-moov-at-end.mp4").read_bytes()
    assert moov_first(sample), "fixtures/sample.mp4 must have its moov atom at the front"
    assert not moov_first(tail), "fixtures/sample-moov-at-end.mp4 must have its moov atom at the end"
    return {
        "sample": sample,
        "moov-at-end": tail,
        "stretched": stretch(sample, STRETCH_TO),
        "stretched-moov-at-end": stretch(tail, STRETCH_TO),
        "cut-short": cut_short(stretch(sample, STRETCH_TO), 0.55),
    }


# ---------------------------------------------------------------------------------------------
# The stand-in bucket
# ---------------------------------------------------------------------------------------------

class Bucket(MemoryStorage):
    """Signed, expiring links, as the real bucket gives. Everything it is asked is kept."""

    def __init__(self, origin: str, seconds_override: int | None):
        super().__init__()
        self.origin = origin  # "https://127.0.0.1:19301" or "http://127.0.0.1:19300/__bucket"
        self.override = seconds_override
        self.link_seconds: dict[str, int] = {}  # per object
        self.throttle: dict[str, tuple[int, int]] = {}  # per object: (bytes a second, first byte ms)
        self.dead_before: dict[str, float] = {}  # per object: links signed before this are dead
        self.held: set[str] = set()  # objects whose answers are stopped in mid-flow (no signal)
        self.hold_from: dict[str, int] = {}  # per held object: the byte from which nothing is sent (default 0)
        self.refusal: dict[str, str] = {}  # per object: "visible" leaves the XML declaration off
        self.requests: list[dict[str, Any]] = []
        self.links: list[dict[str, Any]] = []

    @staticmethod
    def _sign(key: str, exp: str, made: str) -> str:
        return hmac.new(SIGNING_KEY, f"{key}:{exp}:{made}".encode(), hashlib.sha256).hexdigest()[:32]

    def get_url(self, key: str, seconds: int) -> str:
        now = time.time()
        life = self.link_seconds.get(key) or self.override or seconds
        exp, made = str(int(now + life)), f"{now:.3f}"
        self.links.append({"t": round(now, 3), "key": key, "seconds": life})
        return f"{self.origin}/{key}?X-Expires={exp}&X-Made={made}&X-Signature={self._sign(key, exp, made)}"

    def check(self, key: str, query: Any) -> str:
        """"" when the link is good, else why it is refused."""
        exp, made = query.get("X-Expires", ""), query.get("X-Made", "")
        if not hmac.compare_digest(self._sign(key, exp, made), query.get("X-Signature", "")):
            return "SignatureDoesNotMatch"
        try:
            if int(exp) < time.time() or float(made) < self.dead_before.get(key, 0.0):
                return "Request has expired"
        except ValueError:
            return "SignatureDoesNotMatch"
        return ""


def bucket_app(bucket: Bucket, prefix: str = "") -> Starlette:
    async def trickle(key: str, part: bytes, rate: int, first_byte_ms: int, row: dict[str, Any], start: int = 0):
        if first_byte_ms:
            await asyncio.sleep(first_byte_ms / 1000)
        step = max(512, min(rate, 2_000_000) // 20)
        for i in range(0, len(part), step):
            # The signal has dropped: nothing more arrives until it is back. With a place in the
            # file given (hold_from), what lies before that place still arrives.
            while key in bucket.held and start + i >= bucket.hold_from.get(key, 0):
                await asyncio.sleep(0.05)
            piece = part[i:i + step]
            yield piece
            row["sent"] += len(piece)
            await asyncio.sleep(len(piece) / rate)

    async def obj(request: Request) -> Response:
        key = request.path_params["key"]
        row = {"t": round(time.time(), 3), "key": key, "method": request.method, "range": request.headers.get("range"),
               "status": 0, "bytes": 0, "sent": 0, "ua": request.headers.get("user-agent", "")[:80]}
        bucket.requests.append(row)

        def done(response: Response, size: int = 0) -> Response:
            row["status"], row["bytes"] = response.status_code, size
            return response

        refused = bucket.check(key, request.query_params)
        if refused:
            row["refused"] = refused
            xml = f"<Error><Code>AccessDenied</Code><Message>{refused}</Message></Error>"
            if bucket.refusal.get(key) != "visible":  # what S3 and R2 really send; Chromium hides it from the player
                xml = '<?xml version="1.0" encoding="UTF-8"?>' + xml
            return done(Response(xml, 403, media_type="application/xml"))
        body = bucket.objects.get(key)
        if body is None:
            return done(Response("<Error><Code>NoSuchKey</Code></Error>", 404, media_type="application/xml"))
        etag = hashlib.md5(body, usedforsecurity=False).hexdigest()
        head = {"Accept-Ranges": "bytes", "Content-Type": "video/mp4", "ETag": f'"{etag}"',
                "Last-Modified": "Fri, 13 Nov 2026 17:00:00 GMT"}
        status, part, start = 200, body, 0
        rng = request.headers.get("range")
        if rng and rng.startswith("bytes=") and "," not in rng:
            a, _, b = rng[6:].partition("-")
            try:
                if a:
                    start, end = int(a), min(int(b), len(body) - 1) if b else len(body) - 1
                else:
                    start, end = max(0, len(body) - int(b)), len(body) - 1
            except ValueError:
                start, end = 0, len(body) - 1
            if start >= len(body) or start > end:
                return done(Response(status_code=416, headers={**head, "Content-Range": f"bytes */{len(body)}"}))
            status, part = 206, body[start:end + 1]
            head["Content-Range"] = f"bytes {start}-{end}/{len(body)}"
        head["Content-Length"] = str(len(part))
        if request.method == "HEAD":
            return done(Response(b"", status, headers=head))
        rate, first_byte_ms = bucket.throttle.get(key, (0, 0))
        if rate or first_byte_ms:
            row["status"], row["bytes"] = status, len(part)
            return StreamingResponse(trickle(key, part, rate or 10**9, first_byte_ms, row, start), status, headers=head)
        row["sent"] = len(part)
        return done(Response(part, status, headers=head), len(part))

    return Starlette(routes=[Route(prefix + "/{key:path}", obj, methods=["GET", "HEAD"])])


def throwaway_cert(dirpath: Path) -> tuple[str, str]:
    """A self-signed certificate for 127.0.0.1 and localhost, good for a month."""
    cert_path, key_path = dirpath / "cert.pem", dirpath / "key.pem"
    try:
        import ipaddress

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError:  # `pip install -e ".[dev,video]"` does not promise cryptography: use openssl
        conf = dirpath / "openssl.cnf"
        conf.write_text(
            "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=127.0.0.1\n"
            "[ext]\nsubjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=CA:FALSE\n"
        )
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30", "-config", str(conf),
             "-keyout", str(key_path), "-out", str(cert_path)],
            check=True, capture_output=True,
        )
        return str(cert_path), str(key_path)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(
            [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]), False)
        .sign(key, hashes.SHA256())
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(cert_path), str(key_path)


# ---------------------------------------------------------------------------------------------
# The real server, with a pool of customers to film and a control API in front of it
# ---------------------------------------------------------------------------------------------

LIMITED = ("198.18.", "198.19.")  # 198.18.0.0/15: set aside for testing (RFC 2544), never a customer


class NoLimit(RateLimiter):
    """Forty test browsers on 127.0.0.1 are not one customer hammering the page, so they are
    not limited. A client that says (X-Forwarded-For) it is in 198.18.0.0/15 gets the real
    limiter, unchanged: that is how one test hits the limit without slowing the others."""

    def retry_after(self, key: str, limit: int, window: float) -> int:
        if key.rpartition(":")[2].startswith(LIMITED):
            return super().retry_after(key, limit, window)
        return 0


def pooled_seed(size: int):
    """The usual seeded day plus `size` more finished stops for Pat, one fresh video each."""
    original = vh.seed

    def seed() -> dict[str, dict[int, dict[str, Any]]]:
        data = original()
        day = vh.today()
        for n in range(size):
            customer, appointment = POOL_FIRST_CUSTOMER + n, 20000 + n
            data["customer"][customer] = {
                "customerID": str(customer), "officeID": "7", "fname": "Test", "lname": f"Customer{n}",
                "companyName": "", "email": f"customer{n}@example.com", "phone1": "",
                "address": STREETS[n % len(STREETS)], "city": "Folsom", "state": "CA", "zip": "95630",
                "lat": "38.670000", "lng": "-121.170000", "emailReminders": "1",
            }
            data["appointment"][appointment] = {
                "appointmentID": str(appointment), "officeID": "7", "customerID": str(customer), "routeID": "70",
                "type": "89", "status": "1", "assignedTech": "20", "servicedBy": "20", "employeeID": "22",
                "date": day, "start": "08:00:00", "sequence": str(10 + n),
                "checkIn": vh.minutes_ago(50), "checkOut": vh.minutes_ago(20),
            }
        return data

    return seed


class Harness:
    """Puts /__test/ (and, in same-origin mode, /__bucket/) in front of the real app and keeps
    a list of every request the real app answered. The real app itself is not changed."""

    def __init__(self, real: Any, control: Any, bucket: Any | None):
        self.real, self.control, self.bucket = real, control, bucket
        self.requests: list[dict[str, Any]] = []
        self.page_life: dict[str, int] = {}  # per watch token: the link life its page is told

    def told_life(self, path: str, body: bytes) -> bytes:
        """The watch page tells its script how long the player's storage link lives (data-life,
        nine tenths of the server's one setting, 900 s). A video filmed with link_seconds=N has
        links that live N seconds, so its page must say so, as a server set to N would. The
        number is swapped in place, padded to the same length; nothing else is touched."""
        life = self.page_life.get(path.rsplit("/", 1)[-1]) if path.startswith("/w/") and path.count("/") == 2 else None
        found = re.search(rb'data-life="(\d+)"', body) if life else None
        if not found:
            return body
        new = b'data-life="%d"' % life
        if len(new) > len(found.group(0)):
            return body
        return body[:found.start()] + new.ljust(len(found.group(0))) + body[found.end():]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.real(scope, receive, send)
            return
        path = scope.get("path", "")
        if path.startswith("/__test/"):
            await self.control(scope, receive, send)
            return
        if self.bucket is not None and path.startswith("/__bucket/"):
            await self.bucket(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        row = {"t": round(time.time(), 3), "method": scope["method"], "path": path, "status": 0,
               "range": headers.get("range"), "ua": headers.get("user-agent", "")[:80]}
        self.requests.append(row)

        async def watched_send(message: Any) -> None:
            if message["type"] == "http.response.start":
                row["status"] = message["status"]
            elif message["type"] == "http.response.body" and self.page_life and message.get("body"):
                message = {**message, "body": self.told_life(path, message["body"])}
            await send(message)

        await self.real(scope, receive, watched_send)


class QuietServer(uvicorn.Server):
    """Two servers share one loop: signals are handled once, in main()."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("port_positional", nargs="?", type=int, help="same as --port")
    parser.add_argument("--port", type=int, default=19300, help="web server port; the https bucket uses port+1")
    parser.add_argument("--host", default="127.0.0.1", help="0.0.0.0 to reach it from a phone on the same Wi-Fi")
    parser.add_argument("--base-url", default="", help="how browsers reach the web server, if not http://HOST:PORT")
    parser.add_argument("--link-seconds", type=int, default=None, help="signed links live this long (default 900)")
    parser.add_argument("--bucket", choices=["https", "same-origin"], default="https")
    parser.add_argument("--rate-limit", choices=["off", "real"], default="off")
    parser.add_argument("--pool", type=int, default=3000, help="how many fresh videos can be filmed")
    args = parser.parse_args()
    port = args.port_positional or args.port
    shown_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    base = (args.base_url or f"http://{shown_host}:{port}").rstrip("/")
    logging.getLogger().setLevel(logging.WARNING)

    tmp = Path(tempfile.mkdtemp(prefix="zest-browser-tests-"))
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)  # also when start-up fails half way
    files = load_files()
    vh.BASE = base
    vh.seed = pooled_seed(args.pool)
    env = await vh.make_env(tmp, base_url=base)
    if args.rate_limit == "off":
        env.ctx.limiter = NoLimit()
    bucket_origin = f"{base}/__bucket" if args.bucket == "same-origin" else f"https://{shown_host}:{port + 1}"
    bucket = Bucket(bucket_origin, args.link_seconds)
    env.ctx.storage = bucket
    env.storage = bucket

    tech = await vh.login(env)
    r = await env.http.get("/v1/today", headers=tech)  # the sync that puts the pool on Pat's list
    assert r.status_code == 200, r.text
    lock = asyncio.Lock()
    used = {"n": 0}
    kinds: dict[str, str] = {}  # token -> which file

    async def film(customer_id: int, file: str = "sample") -> Any:
        """Register, upload and confirm through the real API, with a real MP4 in the bucket."""
        body = vh.video_body(customer_id, sizeBytes=len(files[file]), durationSec=8.0)
        r = await env.http.post("/v1/videos", json=body, headers=tech)
        assert r.status_code == 200, r.text
        row = await env.video(body["id"])
        bucket.objects[row.object_key] = files[file]
        r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": len(files[file])}, headers=tech)
        assert r.status_code == 200, r.text
        row = await env.video(body["id"])
        kinds[row.token] = file
        return row

    def links(row: Any) -> dict[str, Any]:
        return {"id": row.id, "token": row.token, "watch": f"{base}/w/{row.token}", "video": f"{base}/w/{row.token}/video",
                "unsubscribe": f"{base}/u/{row.token}", "address": row.address, "tech": row.tech_first_name,
                "file": kinds.get(row.token, "sample"), "bytes": row.size_bytes}

    # Three videos through the whole path, email included: two live, one pulled by the office.
    first, second, third = await film(501), await film(502), await film(503)
    for row in (first, second, third):
        await env.window_closed(row.id)
    while await videos.send_due(env.ctx):
        pass
    emails = {m.html.split("/w/")[1][:32]: m.html for m in env.mailer.sent if "/w/" in (m.html or "")}
    await videos.pull(env.ctx, third.id, "office", "browser tests: a dead link")

    info = {
        "base": base,
        "bucket": bucket.origin,
        "bucketMode": args.bucket,
        "control": f"{base}/__test",
        "watch": f"{base}/w/{first.token}",
        "watch2": f"{base}/w/{second.token}",
        "dead": f"{base}/w/{third.token}",
        "unknown": f"{base}/w/{'0' * 32}",
        "unsubscribe": f"{base}/u/{second.token}",
        "privacy": f"{base}/privacy",
        "terms": f"{base}/terms",
        "support": f"{base}/support",
        "bucket_seen": f"{base}/__test/bucket",
        "officePhone": env.ctx.cfg.office_phone,
        "brand": env.ctx.cfg.brand_name,
        "linkSeconds": args.link_seconds or env.ctx.cfg.watch_url_seconds,
        "rateLimit": args.rate_limit,  # "off": only clients from 198.18.0.0/15 are limited
        "files": {name: {"bytes": len(data), "moovFirst": moov_first(data)} for name, data in files.items()},
    }

    # -- the control API ----------------------------------------------------------------------

    async def health(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def get_info(request: Request) -> Response:
        return JSONResponse(info)

    async def new_video(request: Request) -> Response:
        q = dict(request.query_params)
        file = q.get("file", "sample")
        if file not in files:
            return JSONResponse({"error": f"file must be one of {sorted(files)}"}, status_code=400)
        async with lock:
            if used["n"] >= args.pool:
                return JSONResponse({"error": "the pool of fresh videos is used up; start with a bigger --pool"}, status_code=503)
            customer = POOL_FIRST_CUSTOMER + used["n"]
            used["n"] += 1
            row = await film(customer, file)
        if q.get("address"):
            await env.run(sa.update(db.videos).where(db.videos.c.id == row.id).values(address=q["address"][:200]))
        if q.get("link_seconds"):
            bucket.link_seconds[row.object_key] = int(q["link_seconds"])
            harness.page_life[row.token] = max(1, int(q["link_seconds"]) * 9 // 10)
        if q.get("refusal"):
            bucket.refusal[row.object_key] = q["refusal"]
        if q.get("throttle") or q.get("first_byte_ms"):
            bucket.throttle[row.object_key] = (int(q.get("throttle") or 0), int(q.get("first_byte_ms") or 0))
        if q.get("pulled"):
            await videos.pull(env.ctx, row.id, "office", "browser tests: pulled on request")
        return JSONResponse(links(await env.video(row.id)))

    async def state(request: Request) -> Response:
        token = request.query_params.get("token", "")
        row = await videos.by_token(env.ctx, token)
        if row is None:
            return JSONResponse({"error": "no such token"}, status_code=404)
        score = await videos.get_score(env.ctx, row.id)
        events = [e.action for e in await env.all(
            sa.select(db.events).where(db.events.c.video_id == row.id).order_by(db.events.c.id))]
        mine = [dict(r, path=r["path"].replace(token, "TOKEN")) for r in harness.requests if f"/{token}" in r["path"]]

        def hits(suffix: str, method: str) -> int:
            return sum(1 for r in mine if r["path"] == f"/w/TOKEN{suffix}" and r["method"] == method)

        return JSONResponse({
            **links(row),
            "state": row.state,
            "watchable": videos.is_watchable(row),
            "viewCount": row.view_count,
            "firstViewed": row.first_viewed_at.isoformat() if row.first_viewed_at else None,
            "score": {"stars": score.stars, "comment": score.comment} if score else None,
            "events": events,
            "played": events.count("video_played"),
            "scoresSaved": events.count("score_saved"),
            "notMyHome": events.count("not_my_home"),
            "hits": {"page": hits("", "GET"), "video": hits("/video", "GET"), "played": hits("/played", "POST"),
                     "score": hits("/score", "POST"), "notMyHome": hits("/not-my-home", "POST"), "all": len(mine)},
            "web": mine,
            "bucket": [r for r in bucket.requests if r["key"] == row.object_key],
            "linksSigned": sum(1 for link in bucket.links if link["key"] == row.object_key),
        })

    async def email(request: Request) -> Response:
        html = emails.get(request.query_params.get("token", ""))
        if html is None:
            return PlainTextResponse("no email was sent for that token (only watch and watch2 have one)", status_code=404)
        return Response(html, media_type="text/html; charset=utf-8")

    async def expire(request: Request) -> Response:
        row = await videos.by_token(env.ctx, request.query_params.get("token", ""))
        if row is None:
            return JSONResponse({"error": "no such token"}, status_code=404)
        bucket.dead_before[row.object_key] = time.time()
        return JSONResponse({"ok": True})

    async def pull(request: Request) -> Response:
        row = await videos.by_token(env.ctx, request.query_params.get("token", ""))
        if row is None:
            return JSONResponse({"error": "no such token"}, status_code=404)
        await videos.pull(env.ctx, row.id, "office", "browser tests: pulled while the customer had the page open")
        return JSONResponse({"ok": True})

    async def stall(request: Request) -> Response:
        row = await videos.by_token(env.ctx, request.query_params.get("token", ""))
        if row is None:
            return JSONResponse({"error": "no such token"}, status_code=404)
        if row.object_key not in bucket.throttle:
            return JSONResponse({"error": "only a video filmed with throttle= can be stalled"}, status_code=400)
        if request.query_params.get("on", "1") in ("1", "true", "yes"):
            bucket.hold_from[row.object_key] = int(request.query_params.get("from") or 0)
            bucket.held.add(row.object_key)
        else:
            bucket.held.discard(row.object_key)
            bucket.hold_from.pop(row.object_key, None)
        return JSONResponse({"ok": True, "stalled": row.object_key in bucket.held})

    async def bucket_seen(request: Request) -> Response:
        return JSONResponse(bucket.requests)

    control = Starlette(routes=[
        Route("/__test/health", health),
        Route("/__test/info", get_info),
        Route("/__test/video", new_video, methods=["POST"]),
        Route("/__test/state", state),
        Route("/__test/email", email),
        Route("/__test/expire", expire, methods=["POST"]),
        Route("/__test/pull", pull, methods=["POST"]),
        Route("/__test/stall", stall, methods=["POST"]),
        Route("/__test/bucket", bucket_seen),
    ])

    same_origin = args.bucket == "same-origin"
    harness = Harness(build_app(env.ctx, run_jobs=False), control, bucket_app(bucket, "/__bucket") if same_origin else None)
    servers = [QuietServer(uvicorn.Config(harness, host=args.host, port=port, log_level="warning", access_log=False))]
    if not same_origin:
        cert, key = throwaway_cert(tmp)
        servers.append(QuietServer(uvicorn.Config(
            bucket_app(bucket), host=args.host, port=port + 1, log_level="warning", access_log=False,
            ssl_certfile=cert, ssl_keyfile=key)))

    def stop(*_: Any) -> None:
        for server in servers:
            server.should_exit = True

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, stop)

    tasks = [asyncio.create_task(server.serve()) for server in servers]
    while not all(server.started for server in servers):
        if any(task.done() for task in tasks):  # a port was taken
            stop()
            break
        await asyncio.sleep(0.05)
    else:
        print(json.dumps(info), flush=True)
    try:
        await asyncio.gather(*tasks)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
