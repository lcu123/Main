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
                                                            "route": "long" gives twenty
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
  POST /__test/route   {"remove": [203], "moveToOtherTech": [204], "restore": [203],
                        "add": [{"customerId": 206, "name": "Morgan Tidewater",
                                 "lastName": "Tidewater", "address": "50 Oak Ave",
                                 "north": 300, "west": 280, "after": 202}]}
                                                            the OFFICE changes the day's
                                                            route. "remove": the stop is
                                                            cancelled, on nobody's list.
                                                            "moveToOtherTech": it leaves
                                                            this tech's list but is still a
                                                            stop that day. "restore" puts
                                                            either back. "add" puts a new
                                                            stop after the customer named
                                                            (at the end if none). Any mix,
                                                            as often as wanted. The answer
                                                            lists this tech's stops, in
                                                            route order.
  POST /__test/email-due {"customerId": N}                 the time has come to email this
                                                            tech's video for that customer.
                                                            If the office has cancelled the
                                                            stop since it was filmed, no
                                                            email goes: the video is held
                                                            for the office ("no_email", and
                                                            "withOffice": true on it in
                                                            /v1/today) until the stop is
                                                            restored, which sends it.
                                                            Otherwise it is "sent".
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
  offList             str  what register says to a video for a customer whose stop the
                           office has cancelled: "accept" (the default, and the
                           contract: it is taken like any other, and the office
                           decides what becomes of it) or "refuse" (400 bad_request,
                           "That stop is not on the list for that day. Use Not On My
                           List.": what a server from before that rule answers, and
                           what any server answers for a customer who was never a
                           stop that day)
  refuseRegisters     bool answer 400 bad_request to every register, whatever it is for
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
    "offList": "accept",
    "refuseRegisters": False,
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

# Twenty made-up customers for the long route. (The brief: "Done when 20
# videos in a row arrive after airplane-mode tests, with none lost.")
LONG_ROUTE_NAMES = [
    ("Ada", "Abbott"), ("Ben", "Barrow"), ("Cleo", "Castell"), ("Dev", "Dunmore"),
    ("Edie", "Ellery"), ("Finn", "Fenwick"), ("Gia", "Garrow"), ("Hugo", "Hadley"),
    ("Iris", "Ingram"), ("Joss", "Jessop"), ("Kit", "Kendrick"), ("Lena", "Lowell"),
    ("Milo", "Marlowe"), ("Nell", "Norcott"), ("Otis", "Oakhurst"), ("Pia", "Penhale"),
    ("Quin", "Quarry"), ("Rhea", "Rushden"), ("Seth", "Sorrel"), ("Tess", "Thornby"),
]
# How far apart the long route's houses are, in metres. Twenty in a row
# still fit inside the far-away distance (150 m) from the first one.
LONG_ROUTE_GAP_M = 6


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
            self.began = time.monotonic()  # every request and note is timed from here
            self.switches = dict(DEFAULT_SWITCHES)
            self.config = dict(DEFAULT_CONFIG)
            if config:
                self.config.update(config)
            self.requests = []        # everything the app sent, in order
            self.videos = {}          # id -> record
            self.order = []           # video ids in the order they were first registered
            self.other_order = []     # the other tech's video ids, in the order they were put in
            self.helper_stops = set() # customers where the signed-in tech only helps
            # What the office has changed on the route since the morning.
            self.cancelled = set()    # stops taken off the day: on nobody's list
            self.moved = set()        # stops given to the other tech: still stops that day
            self.added = []           # stops added during the day: (row, after which customer)
            self.route_changes = []   # each change, in order, with its time
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

    def _video(self, video_id, customer_id, state, recorded, fixture=False, tech=None, day=None):
        return {
            "id": video_id,
            "customerId": customer_id,
            "techId": TECH["id"] if tech is None else tech,
            # The day's route the video belongs to (the stopDate it was sent with).
            "day": day or datetime.date.today().isoformat(),
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
            # Held for the office to decide: the visit left the day's route
            # after it was filmed, and the email time came.
            "withOffice": False,
        }

    # -- the route ---------------------------------------------------------

    def stops(self):
        checked_in = iso(self.started - datetime.timedelta(minutes=10))
        # "He checked out a minute ago", whenever the phone asks. The app
        # counts a check-in as matching for ten minutes after check-out and
        # takes a stop finished within the hour to be where the tech still
        # is; a time fixed at the reset would run out under a slow test.
        checked_out = iso(utc_now() - datetime.timedelta(minutes=1))
        out = []
        for customer, name, last, address, north, west, order, checked, done, service in self.rows():
            # A stop the office gave to another tech is still a stop that
            # day (it keeps its place in the order): it is just not his.
            if customer in self.moved:
                continue
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
                "video": None if video is None else self.video_ref(video, by_you),
            })
        return out

    @staticmethod
    def video_ref(video, by_you):
        """A stop's video as /v1/today shows it. "withOffice" is there, and
        true, only while the server holds the video for the office."""
        ref = {"id": video["id"], "state": video["state"], "byYou": by_you}
        if video.get("withOffice"):
            ref["withOffice"] = True
        return ref

    def email_due(self, customer_id):
        """The time has come to email this tech's video for a customer (the
        real server's own step when the ten minutes are up). A visit the
        office has taken off the day is not emailed by itself: the video is
        kept and held for the office. Returns the video, or None."""
        with self.lock:
            video = self.live_video(customer_id)
            if video is None or video["state"] != "held":
                return None
            if customer_id in self.cancelled:
                video["state"] = "no_email"
                video["withOffice"] = True
            else:
                video["state"] = "sent"
            return video

    def rows(self):
        """The office's day as it stands now: the morning's stops and the
        ones added since, less the cancelled ones, in route order.

        `order` is each stop's place in that order, counted afresh after
        every change, as the real server counts it at every sync: cancel a
        stop and the later ones move up; add one and they move down. (A
        stop moved to another tech keeps its place, so this tech's own
        numbers can have a gap: the app must not expect 1, 2, 3.)"""
        rows = [
            # customerId, name, lastName, address, north (m), west (m), order, checked, done, service
            (205, "Tom Wilson", "Wilson", "300 Cedar Ln", -1900, -1400, 1, False, True, "Mowing"),
            (201, "Jane Smith", "Smith", "123 Main St", 0, 0, 2, True, True, "Mowing"),
            (202, "Robert Johnson", "Johnson", "125 Main St", 0, 22, 3, False, False, "Pest Control"),
            (203, "Maria Garcia", "Garcia", "48 Oak Ave", 270, 260, 4, False, False, "Fertilization"),
            (204, "Green Valley HOA", "", "5 Park Blvd", 2700, 2200, 5, False, False, "Irrigation"),
        ]
        if self.route == "long":
            # A long day: twenty houses in a row, 6 metres apart, none checked
            # in. Customers 301 to 320, in route order.
            rows = [
                (301 + i, "%s %s" % (first, last), last, "%d Maple Row" % (101 + 2 * i), 0, LONG_ROUTE_GAP_M * i, i + 1, False, False, "Mowing")
                for i, (first, last) in enumerate(LONG_ROUTE_NAMES)
            ]
        for row, after in self.added:
            places = [i for i, existing in enumerate(rows) if existing[0] == after]
            rows.insert(places[0] + 1 if places else len(rows), row)
        rows = [row for row in rows if row[0] not in self.cancelled]
        return [row[:6] + (place,) + row[7:] for place, row in enumerate(rows, start=1)]

    def has_stop(self, customer_id):
        """Is this customer a stop today, on anyone's list?"""
        return any(row[0] == customer_id for row in self.rows())

    def change_route(self, body):
        """The office changes the day's route. Returns an error text, or None."""
        with self.lock:
            known = {row[0] for row in self.rows()} | self.cancelled
            for key in ("remove", "moveToOtherTech", "restore"):
                wanted = body.get(key, [])
                if not isinstance(wanted, list) or any(not isinstance(c, int) for c in wanted):
                    return "%s takes a list of customer numbers" % key
                unknown = [c for c in wanted if c not in known]
                if unknown:
                    return "%s: no such stop today: %s" % (key, unknown)
            added = body.get("add", [])
            if not isinstance(added, list):
                return "add takes a list of stops"
            new_rows = []
            for stop in added:
                customer = stop.get("customerId") if isinstance(stop, dict) else None
                if not isinstance(customer, int) or customer in known or any(r[0][0] == customer for r in new_rows):
                    return "add: each stop needs a customerId that is not on the day already"
                name = str(stop.get("name") or "Customer %d" % customer)
                new_rows.append((
                    (customer, name, str(stop.get("lastName", name.split(" ")[-1])), str(stop.get("address") or "1 New St"),
                     float(stop.get("north", 0)), float(stop.get("west", 0)), 0, False, False, str(stop.get("service") or "Mowing")),
                    stop.get("after")))
            for customer in body.get("remove", []):
                self.cancelled.add(customer)
                self.moved.discard(customer)
            for customer in body.get("moveToOtherTech", []):
                self.moved.add(customer)
                self.cancelled.discard(customer)
            for customer in body.get("restore", []):
                self.cancelled.discard(customer)
                self.moved.discard(customer)
                # The stop is back on the day: a video that was held for
                # the office over it goes out by itself.
                held = self.live_video(customer)
                if held is not None and held.get("withOffice"):
                    held["state"] = "sent"
                    held["withOffice"] = False
            self.added.extend(new_rows)
            self.route_changes.append({
                "after": len(self.requests), "t": round(time.monotonic() - self.began, 1),
                "change": {k: v for k, v in body.items() if v},
            })
            return None

    def live_video(self, customer_id, tech=None, day=None):
        """The newest video one tech made for a customer on one day that
        still counts. (The signed-in tech's and today's, unless told.)
        A video saved late for an earlier day belongs to that day's visit:
        it is not on today's stop, and today's video does not replace it."""
        tech = TECH["id"] if tech is None else tech
        day = day or datetime.date.today().isoformat()
        live = [v for v in self.videos.values()
                if v["customerId"] == customer_id and v["techId"] == tech and v["day"] == day
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
                dict(
                    {"id": v["id"], "customerId": v["customerId"], "state": v["state"],
                     # Not on the list: no email is waiting until the office matches it.
                     "emailAt": None if v["state"] == "unmatched" else v["emailAt"]},
                    **({"withOffice": True} if v.get("withOffice") else {}))
                for v in self.videos.values()
                if v["techId"] == TECH["id"] and v["day"] == datetime.date.today().isoformat()
                and v["state"] not in ("deleted", "replaced", "pulled")
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
                "route": {
                    "mine": [s["customerId"] for s in self.stops()],
                    "cancelled": sorted(self.cancelled),
                    "moved": sorted(self.moved),
                    "added": [row[0] for row, _ in self.added],
                    "changes": self.route_changes,
                },
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
                lines.append("%3d %7.1fs  %-6s %-46s -> %s%s" % (r["n"], r.get("t", 0.0), r["method"], r["path"], r["status"], extra))
            for v in (self.videos[i] for i in self.order):
                lines.append("video %s customer=%s state=%s%s registers=%d puts=%s bytes=%d completes=%s deletes=%d" % (
                    v["id"], v["customerId"], v["state"], " (held for the office)" if v.get("withOffice") else "",
                    v["registers"], [p["status"] for p in v["puts"]], v["bytes"], v["completes"], v["deletes"]))
            for v in (self.videos[i] for i in self.other_order):
                lines.append("another tech's video %s customer=%s state=%s (%s)" % (
                    v["id"], v["customerId"], v["state"],
                    "this tech only helps there" if v["customerId"] in self.helper_stops else "this tech has his own appointment there"))
            for change in self.route_changes:
                lines.append("the office changed the route after request %d (%.1fs): %s" % (
                    change["after"], change["t"], json.dumps(change["change"], sort_keys=True)))
            # Where the app went (each change of screen, each big button a
            # finger came down on, and any tap its double-tap shield kept),
            # apart from what it said of its uploads.
            def of_screens(note):
                return note["note"].startswith(("screen ", "shield", "press "))
            screens = [note for note in self.notes if of_screens(note)]
            uploads = [note for note in self.notes if not of_screens(note)]
            if screens:
                lines.append("the app's screens:")
                for note in screens:
                    lines.append("  %.1fs: %s" % (note.get("t", 0.0), note["note"]))
            if uploads:
                lines.append("what the app said about its uploads:")
                for note in uploads:
                    lines.append("  after request %d (%.1fs): %s" % (note["after"], note.get("t", 0.0), note["note"]))
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
                "t": round(time.monotonic() - STATE.began, 1),
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
            # A small JSON call is read first, so the log can say WHICH video
            # a call that never got through was for. The app sees no difference.
            length = int(self.headers.get("Content-Length") or 0)
            if self.command == "POST" and path.startswith("/v1/") and 0 < length <= 262144:
                self.read_json()
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
        # (The real server's word for an address it does not have.)
        return self.error(404, "bad_request", "No such address.")

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

        # The real server takes a video up to 14 days after its stop (a phone
        # that was offline for days still gets to send), and no older.
        try:
            filmed = datetime.datetime.strptime(str(body.get("stopDate") or ""), "%Y-%m-%d").date()
        except ValueError:
            return self.error(400, "bad_request", "This video has a bad date.")
        age = (datetime.date.today() - filmed).days
        if not -1 <= age <= 14:
            return self.error(400, "bad_request", "This video is too old to send.")

        if STATE.switches["refuseRegisters"]:
            return self.error(400, "bad_request", "This video cannot be taken.")

        # A video for a customer who is not a stop that day. The contract:
        # a stop the office cancelled (or gave to another tech) after the
        # phone's list was fetched is still taken, and the office decides.
        # A customer who was never a stop that day is refused, with the
        # real server's words. ("offList": "refuse" answers a cancelled
        # stop the same way, as a server from before that rule did.)
        with STATE.lock:
            customer = body.get("customerId")
            for_customer = not body.get("notOnMyList") and customer is not None and video_id not in STATE.videos
            cancelled = customer in STATE.cancelled
            never_a_stop = not cancelled and not STATE.has_stop(customer)
        if for_customer and (never_a_stop or (cancelled and STATE.switches["offList"] == "refuse")):
            return self.error(400, "bad_request", "That stop is not on the list for that day. Use Not On My List.")

        with STATE.lock:
            customer = body.get("customerId")
            existing = STATE.videos.get(video_id)
            if existing is None:
                # One video per tech per property. Only THIS tech's own
                # earlier video matters here: once it has gone to the
                # customer the stop takes no new one from him, and before
                # that recording again replaces it. Another tech's video is
                # never replaced and never stands in the way.
                current = STATE.live_video(customer, day=filmed.isoformat()) if customer is not None else None
                if current is not None and current["state"] in ("sent", "watched", "no_email"):
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
                    existing = STATE._video(video_id, customer, "awaiting_upload", recorded, day=filmed.isoformat())
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
                409, "visit_has_video", self.locked_words(locked),
                existing={"id": locked["id"], "state": locked["state"], "byYou": True,
                          "techFirstName": TECH["firstName"]})
        self.reply(200, answer)

    @staticmethod
    def locked_words(video):
        """What a tech is told when he can no longer change a video himself."""
        if video.get("withOffice"):
            # Held back because the visit left the day: it has gone to nobody yet.
            return "The office is looking at this video. Call the office to change it."
        return "This video already went to the customer. Call the office to change it."

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
        if video is None or video["techId"] != TECH["id"]:
            return self.error(404, "bad_request", "We do not know this video.")
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
                    # Not on the list: it waits for the office to pick the
                    # customer. No email is due until then.
                    off_list = bool((video["payload"] or {}).get("notOnMyList")) or video["customerId"] is None
                    video["state"] = "unmatched" if off_list else "held"
                video["completes"].append(200)
                answer = {"id": video_id, "state": video["state"],
                          "emailAt": None if video["state"] == "unmatched" else video["emailAt"]}
        if answer is None:
            return self.error(409, "not_uploaded", "The server did not get the whole file.")
        self.reply(200, answer)

    def delete(self, video_id):
        message = "This video already went to the customer. Call the office to change it."
        with STATE.lock:
            video = STATE.videos.get(video_id)
            if video is None or video["techId"] != TECH["id"]:
                # Unknown, or another tech's: not his to delete.
                answer, status = None, 404
            elif video["state"] in ("deleted", "replaced"):
                # A repeat is answered the same way. It is still counted, so
                # a test can tell that the app only asked once.
                video["deletes"] += 1
                answer, status = {"id": video_id, "state": video["state"]}, 200
            elif video["state"] not in ("awaiting_upload", "held", "unmatched"):
                answer, status = None, 409
                message = self.locked_words(video)
            elif video["state"] == "unmatched" and utc_now() > self.delete_closes(video):
                # Not on the list: no email closes the window, so the same
                # ten minutes are counted from when it was filmed.
                answer, status = None, 409
                message = "The time to delete this video has passed. Call the office to change it."
            else:
                video["deletes"] += 1
                video["state"] = "deleted"
                answer, status = {"id": video_id, "state": "deleted"}, 200
        if status == 404:
            return self.error(404, "bad_request", "We do not know this video.")
        if status == 409:
            return self.error(409, "too_late", message)
        self.reply(200, answer)

    @staticmethod
    def delete_closes(video):
        recorded = datetime.datetime.strptime(video["recordedAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        return recorded + datetime.timedelta(minutes=STATE.config["holdMinutes"])

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
                STATE.notes.append({"after": len(STATE.requests), "t": round(time.monotonic() - STATE.began, 1), "note": text})
            print("      app: %s" % text, flush=True)
            return self.reply(200, {"ok": True}, record=False)
        if path == "/__test/set" and self.command == "POST":
            with STATE.lock:
                for key, value in (body or {}).items():
                    if key == "offList" and value not in ("accept", "refuse"):
                        return self.reply(400, {"error": "offList is \"accept\" or \"refuse\""}, record=False)
                    if key in STATE.switches:
                        STATE.switches[key] = value
                    elif key in STATE.config:
                        STATE.config[key] = value
                    else:
                        return self.reply(400, {"error": "unknown switch: %s" % key}, record=False)
                answer = {"ok": True, "switches": STATE.switches, "config": STATE.config}
            print("      set %s" % json.dumps(body, sort_keys=True), flush=True)
            return self.reply(200, answer, record=False)
        if path == "/__test/route" and self.command == "POST":
            problem = STATE.change_route(body or {})
            if problem:
                return self.reply(400, {"error": problem}, record=False)
            with STATE.lock:
                mine = [s["customerId"] for s in STATE.stops()]
            print("      the office changed the route %s: this tech now has %s" % (
                json.dumps(body, sort_keys=True), mine), flush=True)
            return self.reply(200, {"ok": True, "customers": mine}, record=False)
        if path == "/__test/email-due" and self.command == "POST":
            customer = (body or {}).get("customerId")
            video = STATE.email_due(customer) if isinstance(customer, int) else None
            if video is None:
                return self.reply(400, {"error": "this tech has no held video for that customer"}, record=False)
            print("      the email time came for customer %s: %s%s" % (
                customer, video["state"], " (held for the office)" if video["withOffice"] else ""), flush=True)
            return self.reply(200, {"ok": True, "id": video["id"], "state": video["state"],
                                    "withOffice": video["withOffice"]}, record=False)
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
    kept = [{k: v for k, v in note.items() if k != "t"} for note in state["notes"]]
    check(kept == [{"after": 1, "note": "upload task 1 made"}] and state["logins"] == 0, "notes are kept")
    check(all(isinstance(note.get("t"), float) for note in state["notes"]), "notes are timed")
    check(all(isinstance(r.get("t"), float) for r in state["requests"]), "requests are timed")
    check("upload task 1 made" in call("GET", "/__test/log")[1].decode(), "notes are in the log")
    call("POST", "/__test/note", {"note": "screen review to record"})
    call("POST", "/__test/note", {"note": "shield kept a tap 0.21 s after its screen appeared"})
    text = call("GET", "/__test/log")[1].decode()
    check("the app's screens:" in text and "screen review to record" in text and "shield kept a tap" in text,
          "the app's screens are in the log")
    check(text.index("the app's screens:") < text.index("what the app said about its uploads:")
          and "screen review" not in text.split("what the app said about its uploads:")[1],
          "and are kept apart from what it said about its uploads")
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
    left = datetime.datetime.strptime(stops[201]["checkOut"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    check(0 <= (utc_now() - left).total_seconds() <= 120 and stops[201]["checkIn"] < stops[201]["checkOut"] and stops[201]["done"],
          "201 was checked out a minute ago, however long ago the reset was: %s" % stops[201]["checkOut"])
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
    check(call("POST", "/v1/videos", {"id": "never-arrives", "customerId": 201}, token)[0] == "dropped", "down drops a register too")
    seen = call("GET", "/__test/state")[1]
    last = seen["requests"][-1]
    check(last["status"] == "dropped" and (last["body"] or {}).get("id") == "never-arrives", "a dropped register still says which video it was for")
    check(all(v["id"] != "never-arrives" for v in seen["videos"]), "and the server never heard of that video")
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

    # The long route: twenty stops in a row, none locked, and a locked stop
    # does not come back with the next ordinary reset.
    status, body = call("POST", "/__test/reset", {"test": "long", "route": "long"})
    status, today = call("GET", "/v1/today", token=body["token"])
    long_stops = today["stops"]
    check([s["customerId"] for s in long_stops] == list(range(301, 321)), "twenty stops, 301 to 320")
    check(len(LONG_ROUTE_NAMES) == 20 and len(set(LONG_ROUTE_NAMES)) == 20, "twenty different customers")
    check(all(s["video"] is None and s["checkIn"] is None for s in long_stops), "no videos, no check-ins")
    check(long_stops[0]["address"] == "101 Maple Row" and long_stops[19]["address"] == "139 Maple Row", "one street")
    # One degree of longitude is about 86.9 km here: 0.0015 is about 130 m,
    # inside the 150 m far-away distance from the first house.
    check(abs(long_stops[19]["lng"] - long_stops[0]["lng"]) < 0.0015, "all within about 130 metres")
    check((len(long_stops) - 1) * LONG_ROUTE_GAP_M < 150, "the last house is not far from the first")
    for index in range(20):
        check(register(body["token"], "long-%02d" % index, customer=301 + index)[0] == 200, "register %d" % index)
    state = call("GET", "/__test/state")[1]
    check([v["customerId"] for v in state["videos"]] == list(range(301, 321)), "videos kept in the order they were registered")

    # The answers the real server gives where this one had drifted (the
    # contract test in tests/test_video_contract.py plays both).
    status, body = call("POST", "/__test/reset", {"test": "like the real server", "config": {"maxSeconds": 3}})
    token = body["token"]
    # (1) Not on my list: complete answers "unmatched" with no email time,
    #     and it stays that way in the day's videos.
    status, body = register(token, "loose", customer=None)
    check(status == 200 and put(body["upload"]["url"], video_bytes)[0] == 200, "off-list register and upload")
    status, body = call("POST", "/v1/videos/loose/complete", {"sizeBytes": len(video_bytes)}, token)
    check(status == 200 and body == {"id": "loose", "state": "unmatched", "emailAt": None}, "off-list complete: %s" % body)
    check(call("POST", "/v1/videos/loose/complete", {"sizeBytes": len(video_bytes)}, token)[1]["state"] == "unmatched", "and on a repeat")
    status, body = register(token, "loose", customer=None)
    check(body["state"] == "unmatched" and body["upload"] is None, "a repeat register says the server has it")
    listed = [v for v in call("GET", "/v1/today", token=token)[1]["videos"] if v["id"] == "loose"]
    check(listed == [{"id": "loose", "customerId": None, "state": "unmatched", "emailAt": None}], "in the day's videos: %s" % listed)
    # Inside the ten minutes it can still be deleted; after them it cannot.
    status, body = register(token, "loose-old", customer=None, recordedAt=iso(utc_now() - datetime.timedelta(minutes=11)))
    check(put(body["upload"]["url"], video_bytes)[0] == 200, "an off-list video filmed 11 minutes ago")
    check(call("POST", "/v1/videos/loose-old/complete", {"sizeBytes": len(video_bytes)}, token)[1]["state"] == "unmatched", "unmatched")
    status, body = call("DELETE", "/v1/videos/loose-old", token=token)
    check(status == 409 and body["error"] == "too_late" and "passed" in body["message"], "too late to delete it: %s %s" % (status, body))
    status, body = call("DELETE", "/v1/videos/loose", token=token)
    check(status == 200 and body == {"id": "loose", "state": "deleted"}, "the fresh one can be deleted")
    check(call("DELETE", "/v1/videos/loose", token=token)[1] == {"id": "loose", "state": "deleted"}, "and a repeat says the same")
    # A video on the list still answers "held" with the time its email goes.
    status, body = register(token, "kept", customer=202)
    put(body["upload"]["url"], video_bytes)
    status, body = call("POST", "/v1/videos/kept/complete", {"sizeBytes": len(video_bytes)}, token)
    check(body["state"] == "held" and bool(body["emailAt"]), "on the list: held, with an email time")
    # (2) A video nobody knows, and an address nobody has: 404 "bad_request".
    status, body = call("DELETE", "/v1/videos/no-such-video", token=token)
    check(status == 404 and body["error"] == "bad_request" and body["message"], "delete of an unknown video: %s %s" % (status, body))
    status, body = call("POST", "/v1/videos/no-such-video/complete", {"sizeBytes": 5}, token)
    check(status == 404 and body["error"] == "bad_request" and body["message"], "complete of an unknown video: %s %s" % (status, body))
    status, body = call("GET", "/v1/no-such-call", token=token)
    check(status == 404 and body["error"] == "bad_request" and body["message"], "an unknown address: %s %s" % (status, body))
    # (3) The day on a video: up to 14 days back, and no older.
    def on_day(days_ago):
        return (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()
    check(register(token, "day-1", customer=203, stopDate=on_day(1))[0] == 200, "yesterday's video is taken")
    check(register(token, "day-14", customer=204, stopDate=on_day(14))[0] == 200, "so is one from 14 days ago")
    status, body = register(token, "day-15", customer=204, stopDate=on_day(15))
    check(status == 400 and body["error"] == "bad_request" and "too old" in body["message"], "15 days is too old: %s %s" % (status, body))
    check(register(token, "day+1", customer=201, stopDate=on_day(-1))[0] == 200, "a phone a day ahead is taken")
    check(register(token, "day+2", customer=201, stopDate=on_day(-2))[0] == 400, "two days ahead is not")
    check(register(token, "no-day", customer=201, stopDate="soon")[0] == 400, "nor a date that is not one")
    kept = [v["id"] for v in call("GET", "/__test/state")[1]["videos"]]
    check("day-15" not in kept and "day-14" in kept, "a refused video is not kept: %s" % kept)
    # A video saved late for yesterday is yesterday's: today's stop for the
    # same customer is still open, and today's video does not replace it.
    today = call("GET", "/v1/today", token=token)[1]
    check({s["customerId"]: s for s in today["stops"]}[203]["video"] is None, "yesterday's video is not on today's stop")
    check("day-1" not in [v["id"] for v in today["videos"]], "nor among today's videos")
    check(register(token, "today-203", customer=203)[0] == 200, "today's video for the same customer is taken")
    by_id = {v["id"]: v for v in call("GET", "/__test/state")[1]["videos"]}
    check(by_id["day-1"]["state"] == "awaiting_upload" and by_id["today-203"]["state"] == "awaiting_upload", "and neither replaces the other")
    check(register(token, "day-1-again", customer=203, stopDate=on_day(1))[0] == 200, "recording again for yesterday")
    by_id = {v["id"]: v for v in call("GET", "/__test/state")[1]["videos"]}
    check(by_id["day-1"]["state"] == "replaced" and by_id["today-203"]["state"] == "awaiting_upload", "replaces yesterday's only")

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
    # The office changes the route during the day.
    status, body = call("POST", "/__test/reset", {"test": "route day", "config": {"maxSeconds": 3}})
    token = body["token"]

    def my_stops():
        return call("GET", "/v1/today", token=token)[1]["stops"]

    def numbers():
        return [(s["customerId"], s["order"]) for s in my_stops()]

    def send(video_id, **more):
        """Register, upload, complete. Returns the state complete gave."""
        status, body = register(token, video_id, **more)
        if status != 200:
            return status, body
        check(put(body["upload"]["url"], video_bytes)[0] == 200, "upload %s" % video_id)
        return call("POST", "/v1/videos/%s/complete" % video_id, {"sizeBytes": len(video_bytes)}, token=token)

    check(numbers() == [(205, 1), (201, 2), (202, 3), (203, 4), (204, 5)], "the morning's route")
    status, body = call("POST", "/__test/route", {"remove": [203]})
    check(status == 200 and body["customers"] == [205, 201, 202, 204], "a stop is cancelled")
    check(numbers() == [(205, 1), (201, 2), (202, 3), (204, 4)], "the later stops move up")
    # A video for the cancelled stop is still taken, for that customer.
    status, body = send("66666666-6666-4666-8666-666666666601", customer=203)
    check(status == 200 and body["state"] == "held", "a cancelled stop's video is taken, not refused")
    today = call("GET", "/v1/today", token=token)[1]
    check(203 not in [s["customerId"] for s in today["stops"]]
          and [v["customerId"] for v in today["videos"] if v["id"].endswith("01")] == [203],
          "it is among his videos though its stop is off the list")
    # A server from before that rule refuses it, in these words.
    call("POST", "/__test/set", {"offList": "refuse"})
    status, body = register(token, "66666666-6666-4666-8666-666666666602", customer=203)
    check(status == 400 and body["error"] == "bad_request" and "Not On My List" in body["message"], "offList refuse: 400")
    check(len(call("GET", "/__test/state")[1]["videos"]) == 1, "and no video is made of it")
    # The same video, sent again as not on my list, is taken for the office.
    status, body = send("66666666-6666-4666-8666-666666666602", customer=None, notOnMyList=True, appointmentIds=[])
    check(status == 200 and body["state"] == "unmatched" and body["emailAt"] is None, "as not on my list: unmatched")
    check(call("POST", "/__test/set", {"offList": "unmatched"})[0] == 400, "offList takes two words only")
    call("POST", "/__test/set", {"offList": "accept"})
    # The email time comes for that video: the visit is off the day, so no
    # email goes and the server holds it for the office.
    status, body = call("POST", "/__test/email-due", {"customerId": 203})
    check(status == 200 and body["state"] == "no_email" and body["withOffice"] is True, "held for the office")
    today = call("GET", "/v1/today", token=token)[1]
    held = [v for v in today["videos"] if v["customerId"] == 203]
    check(len(held) == 1 and held[0]["state"] == "no_email" and held[0].get("withOffice") is True, "withOffice in videos[]")
    check(all("withOffice" not in v for v in today["videos"] if v["customerId"] != 203), "and on no other video")
    status, body = register(token, "66666666-6666-4666-8666-666666666607", customer=203)
    check(status == 409 and body["error"] == "visit_has_video" and body["existing"]["state"] == "no_email"
          and body["existing"]["byYou"] is True and body["message"].startswith("The office is looking at this video."),
          "a new video for it: 409, the office is looking")
    status, body = call("DELETE", "/v1/videos/66666666-6666-4666-8666-666666666601", token=token)
    check(status == 409 and body["error"] == "too_late" and body["message"].startswith("The office is looking"),
          "and it cannot be deleted from the phone")
    check(call("POST", "/__test/email-due", {"customerId": 203})[0] == 400, "the email time comes once")
    check(call("POST", "/__test/email-due", {"customerId": 204})[0] == 400, "and only for a video the server holds")
    # A customer who was never a stop that day is refused whatever the switch.
    status, body = register(token, "66666666-6666-4666-8666-666666666606", customer=999)
    check(status == 400 and "Not On My List" in body["message"], "never a stop that day: 400")

    # Moved to another tech: off his list, still a stop, the video is taken.
    status, body = call("POST", "/__test/route", {"moveToOtherTech": [204]})
    check(body["customers"] == [205, 201, 202], "a stop goes to another tech")
    status, body = send("66666666-6666-4666-8666-666666666603", customer=204)
    check(status == 200 and body["state"] == "held", "its video is still taken for the customer")

    # Put back, and one added in the middle.
    status, body = call("POST", "/__test/route", {"restore": [203, 204], "add": [
        {"customerId": 206, "name": "Morgan Tidewater", "lastName": "Tidewater", "address": "50 Oak Ave",
         "north": 300, "west": 280, "after": 202}]})
    check(status == 200 and body["customers"] == [205, 201, 202, 206, 203, 204], "restored, and one added after 202")
    back = [s for s in my_stops() if s["customerId"] == 203][0]["video"]
    check(back["state"] == "sent" and "withOffice" not in back and back["byYou"] is True,
          "the stop is back, so its held video went out and withOffice is gone")
    check(numbers() == [(205, 1), (201, 2), (202, 3), (206, 4), (203, 5), (204, 6)], "numbered afresh")
    added = [s for s in my_stops() if s["customerId"] == 206][0]
    check(added["name"] == "Morgan Tidewater" and added["video"] is None and not added["done"], "the new stop is an open stop")
    check(register(token, "66666666-6666-4666-8666-666666666604", customer=206)[0] == 200, "and takes a video")
    # A gap in his numbers when a stop in the middle is another tech's.
    call("POST", "/__test/route", {"moveToOtherTech": [202]})
    check(numbers() == [(205, 1), (201, 2), (206, 4), (203, 5), (204, 6)], "his own numbers can have a gap")
    # Twice a day, and back again.
    call("POST", "/__test/route", {"remove": [206, 204], "restore": [202]})
    call("POST", "/__test/route", {"restore": [204]})
    check(numbers() == [(205, 1), (201, 2), (202, 3), (203, 4), (204, 5)], "the morning's route again")
    seen = call("GET", "/__test/state")[1]["route"]
    check(seen["cancelled"] == [206] and seen["moved"] == [] and seen["added"] == [206] and len(seen["changes"]) == 6,
          "the state says what the office did")
    check("the office changed the route" in call("GET", "/__test/log")[1].decode(), "and so does the log")
    for bad in ({"remove": [999]}, {"remove": "203"}, {"add": [{"customerId": 201}]}, {"add": [{"name": "x"}]}):
        check(call("POST", "/__test/route", bad)[0] == 400, "a bad route change is refused: %s" % json.dumps(bad))
    # A register that is refused whatever it is for.
    call("POST", "/__test/set", {"refuseRegisters": True})
    status, body = register(token, "66666666-6666-4666-8666-666666666605", customer=None, notOnMyList=True, appointmentIds=[])
    check(status == 400 and body["error"] == "bad_request", "refuseRegisters: 400 even for the office")
    call("POST", "/__test/set", {"refuseRegisters": False})

    call("POST", "/__test/reset", {"test": "again"})
    check(call("GET", "/__test/state")[1]["route"]["changes"] == [], "a reset puts the route back")
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
