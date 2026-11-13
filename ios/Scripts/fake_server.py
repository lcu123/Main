#!/usr/bin/env python3
"""A fake Zest video server for the app's UI tests.

Standard library only. It speaks the contract in docs/video/API.md closely
enough for the app, keeps everything in memory, and can be told to misbehave.

    python3 fake_server.py --port 8765          # run it (on 127.0.0.1 and ::1)
    python3 fake_server.py --self-test          # check it against itself

Two kinds of path:

  /v1/...  /upload/...  /terms  /privacy     what the APP calls
  /__test/...                                 what the TESTS call

Test controls (all JSON):

  POST /__test/reset   {"test": "name", "config": {...}}   forget everything; the answer
                                                            carries a device token that is
                                                            already signed in ("token").
                                                            "route": "long" gives twelve
                                                            stops side by side on one street
                                                            and no locked stop.
  POST /__test/set     {"switch": value, ...}              change behaviour
  POST /__test/other-tech {"customerId": N, "state": "sent", "helper": true}
                                                            another tech already recorded
                                                            that stop today. "helper": true
                                                            means the signed-in tech has no
                                                            appointment of his own there (he
                                                            is shown that video); false means
                                                            he has his own (he is not).
  POST /__test/note    {"note": "text"}                    the APP says what it is doing
                                                            (each upload's start and end)
  GET  /__test/state                                        everything it saw
  GET  /__test/log                                          the request log, as text

Switches for /__test/set (each stays until changed or reset):

  failNext            N    answer 500 to the next N app calls
  failNextComplete    N    answer 500 to the next N "complete" calls only
  down                bool drop every app connection with no answer (server gone)
  unauthorized        bool answer 401 to every signed-in call
  updateRequired      N    answer 426 with minBuild N to every /v1 call (0 = off)
  visitHasVideo       bool answer 409 visit_has_video to register
  expireNextPut       N    answer 403 to the next N uploads (the URL "expired")
  notUploadedNext     N    answer 409 not_uploaded to the next N completes
  todayDelay          s    wait before answering GET /v1/today
  requestCodeDelay    s    wait before answering request-code
  registerDelay       s    wait before answering register
  uploadDelay         s    wait after reading an upload, before answering
  uploadBytesPerSecond N   read uploads no faster than this (0 = full speed)
  completeDelay       s    wait before answering complete
  maxSeconds, holdMinutes, farMeters, recommendButton     the config in /v1/today

Every name, address and number in the fixtures is made up.
"""

import argparse
import datetime
import json
import socket
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CODE = "123456"
TECH = {"id": 7, "firstName": "Sam", "demo": False}
# A second tech, who never signs in here: his videos are put in by the tests.
OTHER_TECH = {"id": 8, "firstName": "Pat"}

# Where the test "phone" stands: on the first customer's lawn.
BASE_LAT = 38.67800
BASE_LNG = -121.17600

DEFAULT_SWITCHES = {
    "failNext": 0,
    "failNextComplete": 0,
    "down": False,
    "unauthorized": False,
    "updateRequired": 0,
    "visitHasVideo": False,
    "expireNextPut": 0,
    "notUploadedNext": 0,
    "todayDelay": 0,
    "requestCodeDelay": 0,
    "registerDelay": 0,
    "uploadDelay": 0,
    "uploadBytesPerSecond": 0,
    "completeDelay": 0,
}

DEFAULT_CONFIG = {
    "maxSeconds": 15,
    "holdMinutes": 10,
    "farMeters": 150,
    "minBuild": 1,
    "requireVideo": True,
    "recommendButton": True,
    "leaveReminders": False,
    "prompts": ["Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate"],
    "officePhone": "+19165550100",
}

LOCKED_VIDEO_ID = "00000000-0000-4000-8000-00000000f1ed"

# Twelve made-up customers for the long route.
LONG_ROUTE_NAMES = [
    ("Ada", "Abbott"), ("Ben", "Barrow"), ("Cleo", "Castell"), ("Dev", "Dunmore"),
    ("Edie", "Ellery"), ("Finn", "Fenwick"), ("Gia", "Garrow"), ("Hugo", "Hadley"),
    ("Iris", "Ingram"), ("Joss", "Jessop"), ("Kit", "Kendrick"), ("Lena", "Lowell"),
]


def iso(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


class State:
    """Everything the server knows. One lock guards it all."""

    def __init__(self):
        self.lock = threading.RLock()
        self.reset(None, None)

    def reset(self, test, config, route=None):
        with self.lock:
            self.test = test or ""
            self.route = route if route in ("usual", "long") else "usual"
            self.started = utc_now()
            self.switches = dict(DEFAULT_SWITCHES)
            self.config = dict(DEFAULT_CONFIG)
            if config:
                self.config.update(config)
            self.requests = []        # everything the app sent, in order
            self.videos = {}          # id -> record
            self.order = []           # video ids in the order they were first registered
            self.other_order = []     # the other tech's video ids, in the order they were put in
            self.helper_stops = set() # customers where the signed-in tech only helps
            # One device token is issued ahead of time, so a test can start
            # the app already signed in instead of typing a number and a code.
            self.issued_token = "issued-" + uuid.uuid4().hex
            self.tokens = {self.issued_token}
            self.notes = []           # what the app said about its uploads
            self.codes = []           # contacts that asked for a code
            self.logins = 0
            self.logouts = 0
            self.events = []
            self.push_tokens = []
            # One stop already has a video that went to its customer: locked.
            # (Not on the long route, where every stop is still to be recorded.)
            if self.route == "usual":
                self.videos[LOCKED_VIDEO_ID] = self._video(
                    LOCKED_VIDEO_ID, 205, state="sent",
                    recorded=self.started - datetime.timedelta(minutes=40), fixture=True)

    def _video(self, video_id, customer_id, state, recorded, fixture=False, tech=None):
        return {
            "id": video_id,
            "customerId": customer_id,
            "techId": TECH["id"] if tech is None else tech,
            "state": state,
            "recordedAt": iso(recorded),
            "emailAt": iso(recorded + datetime.timedelta(minutes=self.config["holdMinutes"])),
            "payload": None,
            "registers": 0,
            "putsStarted": 0,         # uploads begun (counted before the bytes arrive)
            "puts": [],               # one entry per finished upload attempt: {"status", "bytes"}
            "bytes": 0,               # bytes of the last complete upload
            "completes": [],          # one status per complete call
            "deletes": 0,
            "fixture": fixture,
        }

    # -- the route ---------------------------------------------------------

    def stops(self):
        checked_in = iso(self.started - datetime.timedelta(minutes=10))
        checked_out = iso(self.started - datetime.timedelta(minutes=1))
        rows = [
            # customerId, name, lastName, address, north (m), west (m), order, checked, done, service
            (205, "Tom Wilson", "Wilson", "300 Cedar Ln", -1900, -1400, 1, False, True, "Mowing"),
            (201, "Jane Smith", "Smith", "123 Main St", 0, 0, 2, True, True, "Mowing"),
            (202, "Robert Johnson", "Johnson", "125 Main St", 0, 22, 3, False, False, "Pest Control"),
            (203, "Maria Garcia", "Garcia", "48 Oak Ave", 270, 260, 4, False, False, "Fertilization"),
            (204, "Green Valley HOA", "", "5 Park Blvd", 2700, 2200, 5, False, False, "Irrigation"),
        ]
        if self.route == "long":
            # A long day: twelve houses in a row, 8 metres apart, none checked
            # in. Customers 301 to 312, in route order.
            rows = [
                (301 + i, "%s %s" % (first, last), last, "%d Maple Row" % (101 + 2 * i), 0, 8 * i, i + 1, False, False, "Mowing")
                for i, (first, last) in enumerate(LONG_ROUTE_NAMES)
            ]
        out = []
        for customer, name, last, address, north, west, order, checked, done, service in rows:
            # One video per tech per property: the tech sees his own video.
            # Another tech's is shown only where he has no appointment of
            # his own (a helper), and only until he has recorded one himself.
            video = self.live_video(customer)
            by_you = True
            if video is None and customer in self.helper_stops:
                video = self.live_video(customer, tech=OTHER_TECH["id"])
                by_you = False
            out.append({
                "customerId": customer,
                "name": name,
                "lastName": last,
                "address": address,
                "cityLine": "Sampletown, CA 90000",
                "lat": round(BASE_LAT + north / 111_000.0, 6),
                "lng": round(BASE_LNG - west / 86_900.0, 6),
                "appointmentIds": [customer * 10, customer * 10 + 1],
                "services": [service],
                "order": order,
                "checkIn": checked_in if checked else None,
                "checkOut": checked_out if checked else None,
                "done": done,
                "video": None if video is None else {"id": video["id"], "state": video["state"], "byYou": by_you},
            })
        return out

    def live_video(self, customer_id, tech=None):
        """The newest video one tech made for a customer that still counts.
        (The signed-in tech's, unless another tech is named.)"""
        tech = TECH["id"] if tech is None else tech
        live = [v for v in self.videos.values()
                if v["customerId"] == customer_id and v["techId"] == tech
                and v["state"] not in ("deleted", "replaced", "pulled")]
        if not live:
            return None
        return max(live, key=lambda v: v["recordedAt"])

    def add_other_tech_video(self, customer_id, state="sent", helper=False):
        """Another tech recorded this stop earlier today."""
        with self.lock:
            video_id = "00000000-0000-4000-8000-%012d" % (800000 + len(self.other_order))
            self.videos[video_id] = self._video(
                video_id, customer_id, state=state,
                recorded=self.started - datetime.timedelta(minutes=30), fixture=True, tech=OTHER_TECH["id"])
            self.other_order.append(video_id)
            if helper:
                self.helper_stops.add(customer_id)
            else:
                self.helper_stops.discard(customer_id)
            return video_id

    def today(self):
        return {
            "date": datetime.date.today().isoformat(),
            "serverTime": iso(utc_now()),
            "tech": TECH,
            "config": self.config,
            "stops": self.stops(),
            "videos": [
                {"id": v["id"], "customerId": v["customerId"], "state": v["state"], "emailAt": v["emailAt"]}
                for v in self.videos.values()
                if v["techId"] == TECH["id"] and v["state"] not in ("deleted", "replaced", "pulled")
            ],
        }

    def summary(self):
        with self.lock:
            return {
                "test": self.test,
                "switches": self.switches,
                "config": self.config,
                "requests": self.requests,
                "videos": [self.videos[i] for i in self.order],
                "otherTechVideos": [self.videos[i] for i in self.other_order],
                "codes": self.codes,
                "logins": self.logins,
                "logouts": self.logouts,
                "events": self.events,
                "pushTokens": self.push_tokens,
                "notes": self.notes,
            }

    def log_text(self):
        with self.lock:
            lines = ["test: %s" % (self.test or "(none)")]
            for r in self.requests:
                extra = ""
                if r.get("bytes") is not None:
                    extra = " bytes=%d" % r["bytes"]
                elif r.get("body") is not None:
                    extra = " " + json.dumps(r["body"], sort_keys=True)[:300]
                lines.append("%3d  %-6s %-46s -> %s%s" % (r["n"], r["method"], r["path"], r["status"], extra))
            for v in (self.videos[i] for i in self.order):
                lines.append("video %s customer=%s state=%s registers=%d puts=%s bytes=%d completes=%s deletes=%d" % (
                    v["id"], v["customerId"], v["state"], v["registers"],
                    [p["status"] for p in v["puts"]], v["bytes"], v["completes"], v["deletes"]))
            for v in (self.videos[i] for i in self.other_order):
                lines.append("another tech's video %s customer=%s state=%s (%s)" % (
                    v["id"], v["customerId"], v["state"],
                    "this tech only helps there" if v["customerId"] in self.helper_stops else "this tech has his own appointment there"))
            if self.notes:
                lines.append("what the app said about its uploads:")
                for note in self.notes:
                    lines.append("  after request %d: %s" % (note["after"], note["note"]))
            else:
                lines.append("the app said nothing about its uploads (no upload task was made)")
            return "\n".join(lines) + "\n"


STATE = State()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ZestFake/1"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):   # the default logs to stderr per request; ours is tidier
        pass

    def reply(self, status, body=None, record=True, extra=None, raw=None, content_type="application/json"):
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True
        if record:
            self.record(status, extra)

    def record(self, status, extra=None):
        with STATE.lock:
            entry = {
                "n": len(STATE.requests) + 1,
                "test": STATE.test,
                "method": self.command,
                "path": self.path,
                "status": status,
                "body": getattr(self, "json_body", None),
                "bytes": None,
            }
            if extra:
                entry.update(extra)
            STATE.requests.append(entry)
        note = ""
        if entry["bytes"] is not None:
            note = " bytes=%d" % entry["bytes"]
        print("[%s] %-6s %-44s -> %s%s" % (entry["test"] or "-", self.command, self.path, status, note), flush=True)

    def error(self, status, code, message, **more):
        body = {"error": code, "message": message}
        body.update(more)
        self.reply(status, body)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            self.json_body = json.loads(raw.decode() or "{}")
        except ValueError:
            self.json_body = None
        return self.json_body

    def drop(self):
        """Hang up with no answer, as a dead server would."""
        self.record("dropped")
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass

    def take(self, switch):
        """Use up one of a counted switch. True if there was one to use."""
        with STATE.lock:
            if STATE.switches.get(switch, 0) > 0:
                STATE.switches[switch] -= 1
                return True
        return False

    def wait(self, switch):
        seconds = STATE.switches.get(switch, 0)
        if seconds:
            time.sleep(seconds)

    def signed_in(self):
        header = self.headers.get("Authorization") or ""
        token = header[7:] if header.startswith("Bearer ") else ""
        with STATE.lock:
            return bool(token) and token in STATE.tokens and not STATE.switches["unauthorized"]

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def route(self):
        self.json_body = None
        path = self.path.split("?")[0]
        if path.startswith("/__test/"):
            return self.control(path)

        # Everything below is the app talking.
        if STATE.switches["down"]:
            return self.drop()
        if path.startswith("/upload/"):
            return self.upload(path[len("/upload/"):])
        if path in ("/terms", "/privacy"):
            page = "<html><body><h1>%s</h1><p>Fake page for tests.</p></body></html>" % path[1:].title()
            return self.reply(200, raw=page.encode(), content_type="text/html")
        if not path.startswith("/v1/"):
            return self.error(404, "not_found", "No such page.")

        body = self.read_json() if self.command in ("POST", "PUT") else None
        if self.take("failNext"):
            return self.error(500, "server_error", "The server had a problem.")
        minimum = STATE.switches["updateRequired"]
        if minimum:
            return self.error(426, "update_required", "Please update Zest Video.", minBuild=minimum)

        if path == "/v1/auth/request-code" and self.command == "POST":
            return self.request_code(body)
        if path == "/v1/auth/verify" and self.command == "POST":
            return self.verify(body)

        if not self.signed_in():
            return self.error(401, "unauthorized", "This phone was signed out. Please sign in again.")

        if path == "/v1/auth/logout" and self.command == "POST":
            return self.logout()
        if path == "/v1/today" and self.command == "GET":
            self.wait("todayDelay")
            with STATE.lock:
                answer = STATE.today()
            return self.reply(200, answer)
        if path == "/v1/videos" and self.command == "POST":
            return self.register(body)
        if path.startswith("/v1/videos/"):
            rest = path[len("/v1/videos/"):]
            if rest.endswith("/complete") and self.command == "POST":
                return self.complete(rest[:-len("/complete")], body)
            if "/" not in rest and self.command == "DELETE":
                return self.delete(rest)
        if path == "/v1/events" and self.command == "POST":
            with STATE.lock:
                STATE.events.extend((body or {}).get("events", []))
            return self.reply(200, {"ok": True})
        if path == "/v1/devices/push" and self.command == "POST":
            with STATE.lock:
                STATE.push_tokens.append(body)
            return self.reply(200, {"ok": True})
        return self.error(404, "not_found", "No such call.")

    # -- the app's calls ---------------------------------------------------

    def request_code(self, body):
        self.wait("requestCodeDelay")
        contact = str((body or {}).get("contact", "")).strip()
        if not contact:
            return self.error(400, "bad_request", "Type your mobile number first.")
        with STATE.lock:
            STATE.codes.append(contact)
        self.reply(200, {"ok": True})

    def verify(self, body):
        body = body or {}
        if str(body.get("code", "")) != CODE:
            return self.error(401, "bad_code", "That code is not right.", triesLeft=4)
        token = "token-" + uuid.uuid4().hex
        with STATE.lock:
            STATE.tokens.add(token)
            STATE.logins += 1
        self.reply(200, {"token": token, "tech": TECH})

    def logout(self):
        header = self.headers.get("Authorization") or ""
        with STATE.lock:
            STATE.tokens.discard(header[7:])
            STATE.logouts += 1
        self.reply(200, {"ok": True})

    def register(self, body):
        self.wait("registerDelay")
        body = body or {}
        video_id = str(body.get("id", ""))
        if not video_id:
            return self.error(400, "bad_request", "The video has no ID.")
        if STATE.switches["visitHasVideo"]:
            return self.error(
                409, "visit_has_video",
                "This video already went to the customer. Call the office to change it.",
                existing={"id": LOCKED_VIDEO_ID, "state": "sent", "byYou": True, "techFirstName": TECH["firstName"]})

        with STATE.lock:
            customer = body.get("customerId")
            existing = STATE.videos.get(video_id)
            if existing is None:
                # One video per tech per property. Only THIS tech's own
                # earlier video matters here: once it has gone to the
                # customer the stop takes no new one from him, and before
                # that recording again replaces it. Another tech's video is
                # never replaced and never stands in the way.
                current = STATE.live_video(customer) if customer is not None else None
                if current is not None and current["state"] in ("sent", "watched"):
                    locked = current
                else:
                    locked = None
                    # Recording again replaces his own old one by itself.
                    if current is not None:
                        current["state"] = "replaced"
            if existing is None and locked is not None:
                answer = None
            else:
                if existing is None:
                    recorded = utc_now()
                    try:
                        recorded = datetime.datetime.strptime(
                            body.get("recordedAt", ""), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
                    except ValueError:
                        pass
                    existing = STATE._video(video_id, customer, "awaiting_upload", recorded)
                    STATE.videos[video_id] = existing
                    STATE.order.append(video_id)
                existing["payload"] = body
                existing["registers"] += 1
                if existing["state"] == "awaiting_upload":
                    host = self.headers.get("Host") or "127.0.0.1:%d" % self.server.server_address[1]
                    answer = {
                        "id": video_id,
                        "state": "awaiting_upload",
                        "upload": {
                            "method": "PUT",
                            "url": "http://%s/upload/%s?attempt=%d" % (host, video_id, existing["registers"]),
                            "headers": {"Content-Type": "video/mp4"},
                            "expiresAt": iso(utc_now() + datetime.timedelta(hours=24)),
                        },
                    }
                else:
                    # A repeat for a video the server already holds.
                    answer = {"id": video_id, "state": existing["state"], "upload": None,
                              "emailAt": existing["emailAt"]}
        if answer is None:
            return self.error(
                409, "visit_has_video",
                "This video already went to the customer. Call the office to change it.",
                existing={"id": locked["id"], "state": locked["state"], "byYou": True,
                          "techFirstName": TECH["firstName"]})
        self.reply(200, answer)

    def upload(self, video_id):
        if self.command != "PUT":
            return self.error(405, "bad_request", "Uploads are PUT.")
        length = int(self.headers.get("Content-Length") or 0)
        rate = STATE.switches["uploadBytesPerSecond"]
        with STATE.lock:
            known = STATE.videos.get(video_id)
            if known is not None:
                known["putsStarted"] += 1      # visible to tests while the upload is still going
        received = 0
        chunks = []
        try:
            while received < length:
                step = min(length - received, max(1, rate // 10) if rate else 65536)
                chunk = self.rfile.read(step)
                if not chunk:
                    break
                chunks.append(chunk)
                received += len(chunk)
                if rate:
                    time.sleep(0.1)
        except (ConnectionResetError, OSError):
            pass
        self.wait("uploadDelay")

        with STATE.lock:
            video = STATE.videos.get(video_id)
        if video is None:
            return self.reply(404, {"error": "not_found"}, extra={"bytes": received})
        if received < length:
            # The phone hung up part-way (the app was killed, say).
            with STATE.lock:
                video["puts"].append({"status": "cut-off", "bytes": received})
            self.record("cut-off", {"bytes": received})
            self.close_connection = True
            return
        if self.take("expireNextPut"):
            with STATE.lock:
                video["puts"].append({"status": 403, "bytes": received})
            return self.reply(403, raw=b"<Error><Code>AccessDenied</Code></Error>",
                              content_type="application/xml", extra={"bytes": received})
        if self.headers.get("Content-Type") != "video/mp4":
            with STATE.lock:
                video["puts"].append({"status": 400, "bytes": received})
            return self.reply(400, {"error": "bad_content_type"}, extra={"bytes": received})
        with STATE.lock:
            video["puts"].append({"status": 200, "bytes": received})
            video["bytes"] = received
            video["head"] = b"".join(chunks)[:12].hex()
        self.reply(200, raw=b"", content_type="application/xml", extra={"bytes": received})

    def complete(self, video_id, body):
        self.wait("completeDelay")
        with STATE.lock:
            video = STATE.videos.get(video_id)
        if video is None:
            return self.error(404, "not_found", "No such video.")
        if self.take("failNextComplete"):
            with STATE.lock:
                video["completes"].append(500)
            return self.error(500, "server_error", "The server had a problem.")
        if self.take("notUploadedNext"):
            with STATE.lock:
                video["completes"].append(409)
                video["bytes"] = 0     # as if the bucket never got the whole file
            return self.error(409, "not_uploaded", "The server did not get the whole file.")
        size = (body or {}).get("sizeBytes")
        with STATE.lock:
            if video["state"] in ("deleted", "replaced", "pulled"):
                video["completes"].append(200)
                answer = {"id": video_id, "state": video["state"], "emailAt": None}
            elif video["bytes"] <= 0 or video["bytes"] != size:
                video["completes"].append(409)
                answer = None
            else:
                if video["state"] == "awaiting_upload":
                    video["state"] = "held"
                video["completes"].append(200)
                answer = {"id": video_id, "state": video["state"], "emailAt": video["emailAt"]}
        if answer is None:
            return self.error(409, "not_uploaded", "The server did not get the whole file.")
        self.reply(200, answer)

    def delete(self, video_id):
        with STATE.lock:
            video = STATE.videos.get(video_id)
            if video is None or video["techId"] != TECH["id"]:
                # Another tech's video is not his to delete.
                answer, status = None, 404
            elif video["state"] in ("sent", "watched"):
                answer, status = None, 409
            else:
                video["deletes"] += 1
                if video["state"] != "replaced":
                    video["state"] = "deleted"
                answer, status = {"id": video_id, "state": "deleted"}, 200
        if status == 404:
            return self.error(404, "not_found", "No such video.")
        if status == 409:
            return self.error(409, "too_late", "This video already went to the customer. Call the office to change it.")
        self.reply(200, answer)

    # -- the tests' calls --------------------------------------------------

    def control(self, path):
        body = self.read_json() if self.command == "POST" else None
        if path == "/__test/reset" and self.command == "POST":
            body = body or {}
            STATE.reset(body.get("test"), body.get("config"), body.get("route"))
            print("\n===== %s =====" % (STATE.test or "reset"), flush=True)
            return self.reply(200, {"ok": True, "token": STATE.issued_token}, record=False)
        if path == "/__test/note" and self.command == "POST":
            text = str((body or {}).get("note", ""))[:600]
            with STATE.lock:
                STATE.notes.append({"after": len(STATE.requests), "note": text})
            print("      app: %s" % text, flush=True)
            return self.reply(200, {"ok": True}, record=False)
        if path == "/__test/set" and self.command == "POST":
            with STATE.lock:
                for key, value in (body or {}).items():
                    if key in STATE.switches:
                        STATE.switches[key] = value
                    elif key in STATE.config:
                        STATE.config[key] = value
                    else:
                        return self.reply(400, {"error": "unknown switch: %s" % key}, record=False)
                answer = {"ok": True, "switches": STATE.switches, "config": STATE.config}
            print("      set %s" % json.dumps(body, sort_keys=True), flush=True)
            return self.reply(200, answer, record=False)
        if path == "/__test/other-tech" and self.command == "POST":
            body = body or {}
            customer = body.get("customerId")
            state = body.get("state", "sent")
            if not isinstance(customer, int) or state not in ("awaiting_upload", "held", "sent", "watched"):
                return self.reply(400, {"error": "customerId (a number) and a live state are needed"}, record=False)
            video_id = STATE.add_other_tech_video(customer, state, bool(body.get("helper")))
            print("      another tech's video for customer %s (%s, %s)" % (
                customer, state, "helper" if body.get("helper") else "own appointment"), flush=True)
            return self.reply(200, {"ok": True, "id": video_id}, record=False)
        if path == "/__test/state" and self.command == "GET":
            return self.reply(200, STATE.summary(), record=False)
        if path == "/__test/log" and self.command == "GET":
            return self.reply(200, raw=STATE.log_text().encode(), content_type="text/plain", record=False)
        if path == "/__test/ping":
            return self.reply(200, {"ok": True}, record=False)
        self.reply(404, {"error": "unknown control"}, record=False)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


class Server6(Server):
    address_family = socket.AF_INET6


def serve(port, host="127.0.0.1"):
    kind = Server6 if ":" in host else Server
    server = kind((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


# -- self-test -------------------------------------------------------------

def self_test():
    """Walks the app's whole flow against the server, plus every switch."""
    import http.client

    server = serve(0)
    port = server.server_address[1]

    def call(method, path, body=None, token=None, raw=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        send_headers = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            send_headers["Content-Type"] = "application/json"
        if token:
            send_headers["Authorization"] = "Bearer " + token
        try:
            connection.request(method, path, body=data, headers=send_headers)
            response = connection.getresponse()
            payload = response.read()
        except (http.client.HTTPException, OSError) as error:
            return "dropped", {"error": str(error)}
        finally:
            connection.close()
        try:
            return response.status, json.loads(payload.decode() or "{}")
        except ValueError:
            return response.status, payload

    def check(condition, what):
        if not condition:
            raise AssertionError(what)

    def put(url, data, content_type="video/mp4"):
        return call("PUT", url[url.index("/upload/"):], raw=data, headers={"Content-Type": content_type})

    video_bytes = b"\x00\x00\x00\x18ftypmp42" + b"v" * 5000

    def register(token, video_id, customer=201, **more):
        payload = {
            "id": video_id, "stopDate": datetime.date.today().isoformat(), "customerId": customer,
            "appointmentIds": [2010], "notOnMyList": customer is None, "recordedAt": iso(utc_now()),
            "durationSec": 2.0, "sizeBytes": len(video_bytes), "location": None,
            "signals": {"spoken": "unknown", "gps": "match", "checkIn": "match"},
            "override": False, "recommend": False, "spokenText": None,
        }
        payload.update(more)
        return call("POST", "/v1/videos", payload, token)

    # Sign in.
    status, body = call("POST", "/__test/reset", {"test": "self-test", "config": {"maxSeconds": 3}})
    check(status == 200 and body["token"].startswith("issued-"), "reset issues a token")
    check(call("GET", "/v1/today", token=body["token"])[0] == 200, "the issued token is signed in")
    check(call("POST", "/__test/note", {"note": "upload task 1 made"})[0] == 200, "note")
    state = call("GET", "/__test/state")[1]
    check(state["notes"] == [{"after": 1, "note": "upload task 1 made"}] and state["logins"] == 0, "notes are kept")
    check("upload task 1 made" in call("GET", "/__test/log")[1].decode(), "notes are in the log")
    check(call("POST", "/__test/reset", {"test": "self-test", "config": {"maxSeconds": 3}})[0] == 200, "reset")
    check("the app said nothing about its uploads" in call("GET", "/__test/log")[1].decode(), "no notes after reset")
    check(call("POST", "/v1/auth/request-code", {"contact": "9165550100"})[0] == 200, "request-code")
    status, body = call("POST", "/v1/auth/verify", {"contact": "9165550100", "code": "000000"})
    check(status == 401 and body["error"] == "bad_code" and body["triesLeft"] == 4, "wrong code")
    status, body = call("POST", "/v1/auth/verify", {"contact": "9165550100", "code": CODE})
    check(status == 200 and body["tech"]["firstName"] == "Sam", "right code")
    token = body["token"]
    check(call("GET", "/v1/today")[0] == 401, "today needs a token")

    # The route.
    status, today = call("GET", "/v1/today", token=token)
    check(status == 200 and today["config"]["maxSeconds"] == 3, "today config")
    stops = {s["customerId"]: s for s in today["stops"]}
    check(len(stops) == 5, "five stops")
    check(stops[201]["checkIn"] and stops[201]["checkOut"], "first stop was checked in and out")
    check(stops[202]["checkIn"] is None and stops[204]["lastName"] == "", "plain stop and business")
    check(stops[205]["video"]["state"] == "sent", "one stop is locked")
    check(stops[201]["video"] is None, "no video yet")

    # Happy path: register, upload, complete.
    status, body = register(token, "v1")
    check(status == 200 and body["state"] == "awaiting_upload", "register")
    url = body["upload"]["url"]
    check(put(url, video_bytes)[0] == 200, "upload")
    status, body = call("POST", "/v1/videos/v1/complete", {"sizeBytes": len(video_bytes) + 1}, token)
    check(status == 409 and body["error"] == "not_uploaded", "wrong size is not_uploaded")
    status, body = call("POST", "/v1/videos/v1/complete", {"sizeBytes": len(video_bytes)}, token)
    check(status == 200 and body["state"] == "held" and body["emailAt"], "complete")
    status, body = register(token, "v1")
    check(status == 200 and body["upload"] is None and body["state"] == "held", "repeat register is done")
    status, today = call("GET", "/v1/today", token=token)
    check({s["customerId"]: s for s in today["stops"]}[201]["video"]["state"] == "held", "stop shows its video")

    # Record again replaces; delete frees the stop; a locked stop refuses.
    status, body = register(token, "v2")
    check(status == 200, "second video for the stop")
    state = call("GET", "/__test/state")[1]
    by_id = {v["id"]: v for v in state["videos"]}
    check(by_id["v1"]["state"] == "replaced" and by_id["v2"]["state"] == "awaiting_upload", "replaced")
    check(call("DELETE", "/v1/videos/v1", token=token)[0] == 200, "deleting a replaced video is fine")
    check(call("DELETE", "/v1/videos/v2", token=token)[1]["state"] == "deleted", "delete")
    check(call("DELETE", "/v1/videos/nope", token=token)[0] == 404, "delete unknown")
    status, today = call("GET", "/v1/today", token=token)
    check({s["customerId"]: s for s in today["stops"]}[201]["video"] is None, "stop is free again")
    status, body = register(token, "v3", customer=205)
    check(status == 409 and body["error"] == "visit_has_video", "locked stop")
    status, body = call("DELETE", "/v1/videos/" + LOCKED_VIDEO_ID, token=token)
    check(status == 409 and body["error"] == "too_late", "too late")

    # Switches.
    call("POST", "/__test/set", {"failNext": 2})
    check(call("GET", "/v1/today", token=token)[0] == 500, "fail 1")
    check(register(token, "v4")[0] == 500, "fail 2")
    check(call("GET", "/v1/today", token=token)[0] == 200, "healthy again")

    status, body = register(token, "v4", customer=202)
    url = body["upload"]["url"]
    call("POST", "/__test/set", {"expireNextPut": 1})
    check(put(url, video_bytes)[0] == 403, "expired url")
    status, body = register(token, "v4", customer=202)
    check(body["upload"] is not None, "a fresh url")
    check(put(body["upload"]["url"], video_bytes)[0] == 200, "upload after a fresh url")
    call("POST", "/__test/set", {"notUploadedNext": 1})
    check(call("POST", "/v1/videos/v4/complete", {"sizeBytes": len(video_bytes)}, token)[1]["error"] == "not_uploaded", "not uploaded once")
    check(call("POST", "/v1/videos/v4/complete", {"sizeBytes": len(video_bytes)}, token)[0] == 409, "still not there until uploaded again")
    check(put(body["upload"]["url"], video_bytes)[0] == 200, "upload again")
    call("POST", "/__test/set", {"failNextComplete": 1})
    check(call("POST", "/v1/videos/v4/complete", {"sizeBytes": len(video_bytes)}, token)[0] == 500, "complete fails once")
    check(call("POST", "/v1/videos/v4/complete", {"sizeBytes": len(video_bytes)}, token)[0] == 200, "then goes through")
    check(put(url, video_bytes, content_type="text/plain")[0] == 400, "wrong content type")

    call("POST", "/__test/set", {"visitHasVideo": True})
    check(register(token, "v5", customer=203)[0] == 409, "visit_has_video switch")
    call("POST", "/__test/set", {"visitHasVideo": False})

    call("POST", "/__test/set", {"unauthorized": True})
    check(call("GET", "/v1/today", token=token)[0] == 401, "401 switch")
    call("POST", "/__test/set", {"unauthorized": False})

    call("POST", "/__test/set", {"updateRequired": 9})
    status, body = call("GET", "/v1/today", token=token)
    check(status == 426 and body["minBuild"] == 9, "426 switch")
    call("POST", "/__test/set", {"updateRequired": 0})

    call("POST", "/__test/set", {"down": True})
    check(call("GET", "/v1/today", token=token)[0] == "dropped", "down drops the connection")
    check(call("GET", "/__test/ping")[0] == 200, "controls still answer")
    call("POST", "/__test/set", {"down": False})

    call("POST", "/__test/set", {"todayDelay": 0.4, "uploadBytesPerSecond": 20000, "uploadDelay": 0.2})
    began = time.time()
    check(call("GET", "/v1/today", token=token)[0] == 200 and time.time() - began >= 0.4, "today delay")
    status, body = register(token, "v6", customer=None)
    began = time.time()
    check(put(body["upload"]["url"], video_bytes)[0] == 200 and time.time() - began >= 0.4, "slow upload")
    call("POST", "/__test/set", {"todayDelay": 0, "uploadBytesPerSecond": 0, "uploadDelay": 0})
    check(call("POST", "/__test/set", {"noSuchSwitch": 1})[0] == 400, "unknown switch is refused")

    # Events, push, logout, pages.
    check(call("POST", "/v1/events", {"events": [{"type": "override"}]}, token)[0] == 200, "events")
    check(call("POST", "/v1/devices/push", {"apnsToken": "ab", "sandbox": True}, token)[0] == 200, "push")
    check(call("GET", "/terms")[0] == 200 and call("GET", "/privacy")[0] == 200, "pages")
    check(call("POST", "/v1/auth/logout", token=token)[0] == 200, "logout")
    check(call("GET", "/v1/today", token=token)[0] == 401, "token is dead after logout")

    # What the tests can read back.
    status, state = call("GET", "/__test/state")
    by_id = {v["id"]: v for v in state["videos"]}
    check([v["id"] for v in state["videos"]] == ["v1", "v2", "v4", "v6"], "videos in order: %s" % list(by_id))
    check(by_id["v4"]["registers"] == 2 and [p["status"] for p in by_id["v4"]["puts"]] == [403, 200, 200, 400], "v4 history: %s" % by_id["v4"]["puts"])
    check(by_id["v4"]["completes"] == [409, 409, 500, 200] and by_id["v4"]["state"] == "held", "v4 completes: %s" % by_id["v4"]["completes"])
    check(by_id["v6"]["customerId"] is None and by_id["v6"]["payload"]["notOnMyList"], "off-list video")
    check(state["logouts"] == 1 and state["logins"] == 1 and state["codes"] == ["9165550100"], "counters")
    check(state["events"] == [{"type": "override"}], "events kept")
    log = call("GET", "/__test/log")[1].decode()
    check("POST   /v1/videos/v4/complete" in log and "video v4" in log, "log text")

    # The long route: twelve stops in a row, none locked, and a locked stop
    # does not come back with the next ordinary reset.
    status, body = call("POST", "/__test/reset", {"test": "long", "route": "long"})
    status, today = call("GET", "/v1/today", token=body["token"])
    long_stops = today["stops"]
    check([s["customerId"] for s in long_stops] == list(range(301, 313)), "twelve stops, 301 to 312")
    check(all(s["video"] is None and s["checkIn"] is None for s in long_stops), "no videos, no check-ins")
    check(long_stops[0]["address"] == "101 Maple Row" and long_stops[11]["address"] == "123 Maple Row", "one street")
    check(abs(long_stops[11]["lng"] - long_stops[0]["lng"]) < 0.0012, "all within about 90 metres")
    for index in range(12):
        check(register(body["token"], "long-%02d" % index, customer=301 + index)[0] == 200, "register %d" % index)
    state = call("GET", "/__test/state")[1]
    check([v["customerId"] for v in state["videos"]] == list(range(301, 313)), "videos kept in the order they were registered")

    # One video per tech, per property, per day.
    def stop_video(token, customer):
        stops = {s["customerId"]: s for s in call("GET", "/v1/today", token=token)[1]["stops"]}
        return stops[customer]["video"]

    def other(state):
        return {v["customerId"]: v for v in state["otherTechVideos"]}

    status, body = call("POST", "/__test/reset", {"test": "two techs", "config": {"maxSeconds": 3}})
    token = body["token"]
    check(call("POST", "/__test/other-tech", {"customerId": "x"})[0] == 400, "other-tech needs a customer")
    check(call("POST", "/__test/other-tech", {"customerId": 202, "state": "deleted"})[0] == 400, "and a live state")

    # (a) The lead: he has his own appointment at 202, where another tech
    #     already recorded. He is not shown that video, his register is not
    #     refused, and neither video replaces the other.
    status, body = call("POST", "/__test/other-tech", {"customerId": 202, "state": "sent", "helper": False})
    check(status == 200, "another tech's sent video at 202")
    pat_at_202 = body["id"]
    check(stop_video(token, 202) is None, "the lead does not see the other tech's video")
    today = call("GET", "/v1/today", token=token)[1]
    check(pat_at_202 not in [v["id"] for v in today["videos"]], "nor is it among his own videos")
    status, body = register(token, "mine-202", customer=202)
    check(status == 200 and body["upload"] is not None, "his own video is taken, no 409: %s" % status)
    check(put(body["upload"]["url"], video_bytes)[0] == 200, "upload")
    check(call("POST", "/v1/videos/mine-202/complete", {"sizeBytes": len(video_bytes)}, token)[0] == 200, "complete")
    state = call("GET", "/__test/state")[1]
    check(other(state)[202]["state"] == "sent", "the other tech's video was not replaced")
    check(stop_video(token, 202) == {"id": "mine-202", "state": "held", "byYou": True}, "the stop now shows his own")
    check(call("DELETE", "/v1/videos/" + pat_at_202, token=token)[0] == 404, "another tech's video is not his to delete")

    # (b) His own re-record still replaces his own, and only his own.
    status, body = register(token, "mine-202-again", customer=202)
    check(status == 200, "recording again inside the ten minutes")
    state = call("GET", "/__test/state")[1]
    mine = {v["id"]: v for v in state["videos"]}
    check(mine["mine-202"]["state"] == "replaced" and mine["mine-202-again"]["state"] == "awaiting_upload", "his first is replaced by his second")
    check(other(state)[202]["state"] == "sent", "the other tech's video is still there")

    # (c) His own locked video still answers 409, and says it is his.
    status, body = register(token, "mine-205", customer=205)
    check(status == 409 and body["error"] == "visit_has_video" and body["existing"]["byYou"] is True
          and body["existing"]["id"] == LOCKED_VIDEO_ID, "his own sent video locks the stop: %s %s" % (status, body))

    # (d) The helper: no appointment of his own at 203, where another tech's
    #     video is still inside its ten minutes, and at 204, where it has
    #     gone. He is shown both as not his, and can still record his own.
    check(call("POST", "/__test/other-tech", {"customerId": 203, "state": "held", "helper": True})[0] == 200, "held, helper")
    status, body = call("POST", "/__test/other-tech", {"customerId": 204, "state": "sent", "helper": True})
    pat_at_204 = body["id"]
    check(stop_video(token, 203)["byYou"] is False and stop_video(token, 203)["state"] == "held", "the helper sees the held one")
    check(stop_video(token, 204) == {"id": pat_at_204, "state": "sent", "byYou": False}, "and the sent one")
    today = call("GET", "/v1/today", token=token)[1]
    check(sorted(v["id"] for v in today["videos"]) == [LOCKED_VIDEO_ID, "mine-202-again"], "his own videos only: %s" % today["videos"])
    status, body = register(token, "mine-204", customer=204)
    check(status == 200 and body["upload"] is not None, "the helper's own video is taken even after the lead's was sent: %s" % status)
    check(register(token, "mine-203", customer=203)[0] == 200, "and beside one still held")
    state = call("GET", "/__test/state")[1]
    check(other(state)[203]["state"] == "held" and other(state)[204]["state"] == "sent", "neither of the lead's was replaced")
    check(stop_video(token, 204) == {"id": "mine-204", "state": "awaiting_upload", "byYou": True}, "once he has his own, the stop shows his own")
    check(call("DELETE", "/v1/videos/mine-204", token=token)[0] == 200, "he can delete his own")
    check(stop_video(token, 204) == {"id": pat_at_204, "state": "sent", "byYou": False}, "and the stop is the lead's again")
    check("another tech's video" in call("GET", "/__test/log")[1].decode(), "the log names the other tech's videos")

    # Reset forgets everything except the locked fixture.
    call("POST", "/__test/reset", {"test": "again"})
    state = call("GET", "/__test/state")[1]
    check(state["videos"] == [] and state["otherTechVideos"] == [] and state["requests"] == []
          and state["config"]["maxSeconds"] == 15, "reset")
    check(stop_video(call("POST", "/__test/reset", {"test": "again"})[1]["token"], 203) is None, "no helper stops after a reset")

    server.shutdown()
    print("self-test passed")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--self-test", action="store_true")
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
        return
    server = serve(arguments.port, arguments.host)
    print("fake Zest server on http://%s:%d  (code %s)" % (arguments.host, server.server_address[1], CODE), flush=True)
    if arguments.host == "127.0.0.1":
        # The app is pointed at "localhost", which a phone may look up as
        # ::1 first. Answer there too, with the same memory.
        try:
            serve(server.server_address[1], "::1")
            print("also on http://[::1]:%d" % server.server_address[1], flush=True)
        except OSError as error:
            print("not listening on ::1 (%s); 127.0.0.1 only" % error, flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
