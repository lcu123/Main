"""Fixtures for the Zest Visit Video server tests.

Everything outside the server is faked: FieldRoutes is the same `FakeFR` the MCP
tests use (real wire dialect), the bucket, mail, text and push are in-memory
recorders, and the database is a throwaway SQLite file (or Postgres when
`VIDEO_TEST_DATABASE_URL` is set, which is how CI proves both).
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import sqlalchemy as sa

from conftest import FakeFR, _client_for

from zest_video import auth, config, db
from zest_video.app import build_app, build_ctx
from zest_video.core import Ctx
from zest_video.notify import MemoryMailer, MemoryPusher, MemoryTexter
from zest_video.storage import MemoryStorage

TZ = ZoneInfo("America/Los_Angeles")
BASE = "https://video.test"
OWNER = "owner@zest.test"
STAFF = "staff@zest.test"
DEMO_CONTACT = "review@zest.test"
DEMO_CODE = "424242"


def today() -> str:
    return datetime.now(TZ).date().isoformat()


def local(hhmmss: str) -> str:
    """A FieldRoutes-style office-local datetime string for today."""
    return f"{today()} {hhmmss}"


def minutes_ago(minutes: float) -> str:
    return (datetime.now(TZ) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")


def seed() -> dict[str, dict[int, dict[str, Any]]]:
    """A small day shaped like Zest's real one: string-valued fields, a customer with
    three appointments in one visit, two techs, a business with no person on it, a
    customer with no email, and an appointment whose tech lives only on the route."""
    day = today()
    return {
        "office": {7: {"officeID": 7, "officeName": "Zest Lawn & Pest"}},
        "employee": {
            20: {"employeeID": "20", "officeID": "7", "fname": "Pat ", "lname": "Rivera", "type": "1", "active": "1",
                 "email": "Pat.Rivera@example.com", "phone": "(916) 555-0101"},
            21: {"employeeID": "21", "officeID": "7", "fname": "Sam", "lname": "Ortiz", "type": "1", "active": "1",
                 "email": "sam@example.com", "phone": ""},
            22: {"employeeID": "22", "officeID": "7", "fname": "Olive", "lname": "Office", "type": "0", "active": "1",
                 "email": "olive@example.com", "phone": "9165550199"},
            23: {"employeeID": "23", "officeID": "7", "fname": "Gone", "lname": "Tech", "type": "1", "active": "0",
                 "email": "gone@example.com", "phone": "9165550123"},
        },
        "serviceType": {
            89: {"typeID": "89", "officeID": "7", "description": "Bi-Weekly Yard Maintenance"},
            95: {"typeID": "95", "officeID": "7", "description": "Leaf Pick-up Service"},
            80: {"typeID": "80", "officeID": "7", "description": "Lawn Care Essential"},
        },
        "customer": {
            501: {"customerID": "501", "officeID": "7", "fname": "Jane", "lname": "Smith ", "companyName": "",
                  "email": "jane@example.com", "phone1": "9165550001", "address": "123 Main St", "city": "Folsom",
                  "state": "CA", "zip": "95630", "lat": "38.670000", "lng": "-121.170000", "emailReminders": "1",
                  "balance": "150.00", "notes": "Gate code 1234"},
            502: {"customerID": "502", "officeID": "7", "fname": "Bob", "lname": "Jones", "companyName": "",
                  "email": "bob@example.com", "phone1": "9165550002", "address": "125 Main St", "city": "Folsom",
                  "state": "CA", "zip": "95630", "lat": "38.670100", "lng": "-121.170000", "emailReminders": "1"},
            503: {"customerID": "503", "officeID": "7", "fname": "", "lname": "", "companyName": "Acme Corp",
                  "email": "", "phone1": "", "address": "\t1 Industrial Way", "city": "Sacramento", "state": "CA",
                  "zip": "95814", "lat": "0", "lng": "0", "emailReminders": "1"},
            504: {"customerID": "504", "officeID": "7", "fname": "Ann", "lname": "Lee", "companyName": "",
                  "email": "ann@example.com", "phone1": "", "address": "9 Oak Ct", "city": "Roseville", "state": "CA",
                  "zip": "95661", "lat": "38.750000", "lng": "-121.290000", "emailReminders": "0"},
            505: {"customerID": "505", "officeID": "7", "fname": "Cy", "lname": "Young", "companyName": "",
                  "email": "cy@example.com", "phone1": "", "address": "77 Elm St", "city": "Folsom", "state": "CA",
                  "zip": "95630", "lat": "38.680000", "lng": "-121.160000", "emailReminders": "1"},
        },
        "appointment": {
            # Jane: three appointments in one visit, all finished.
            9001: {"appointmentID": "9001", "officeID": "7", "customerID": "501", "routeID": "70", "type": "89",
                   "status": "1", "assignedTech": "20", "servicedBy": "20", "employeeID": "22", "date": day,
                   "start": "08:00:00", "sequence": "1", "checkIn": minutes_ago(50), "checkOut": minutes_ago(20)},
            9002: {"appointmentID": "9002", "officeID": "7", "customerID": "501", "routeID": "70", "type": "95",
                   "status": "1", "assignedTech": "20", "servicedBy": "20", "employeeID": "22", "date": day,
                   "start": "08:00:00", "sequence": "1", "checkIn": minutes_ago(49), "checkOut": minutes_ago(21)},
            9003: {"appointmentID": "9003", "officeID": "7", "customerID": "501", "routeID": "70", "type": "89",
                   "status": "1", "assignedTech": "20", "servicedBy": "20", "employeeID": "22", "date": day,
                   "start": "08:00:00", "sequence": "1", "checkIn": minutes_ago(48), "checkOut": minutes_ago(22)},
            # Bob: next door, checked in right now and not out yet.
            9004: {"appointmentID": "9004", "officeID": "7", "customerID": "502", "routeID": "70", "type": "89",
                   "status": "0", "assignedTech": "20", "employeeID": "22", "date": day, "start": "09:00:00",
                   "sequence": "2", "checkIn": minutes_ago(5), "checkOut": "0000-00-00 00:00:00"},
            # Acme: pending, no check-in, no email, no map pin.
            9005: {"appointmentID": "9005", "officeID": "7", "customerID": "503", "routeID": "70", "type": "80",
                   "status": "0", "assignedTech": "20", "employeeID": "22", "date": day, "start": "10:00:00",
                   "sequence": "3"},
            # Ann: Sam's stop, and the appointment itself has no tech; the route does.
            9006: {"appointmentID": "9006", "officeID": "7", "customerID": "504", "routeID": "71", "type": "80",
                   "status": "0", "assignedTech": "0", "employeeID": "22", "date": day, "start": "08:00:00",
                   "sequence": "1"},
            # Cancelled: must never show up as a stop.
            9007: {"appointmentID": "9007", "officeID": "7", "customerID": "505", "routeID": "70", "type": "89",
                   "status": "-1", "assignedTech": "20", "employeeID": "22", "date": day, "start": "11:00:00"},
            # Another day entirely.
            9008: {"appointmentID": "9008", "officeID": "7", "customerID": "505", "routeID": "60", "type": "89",
                   "status": "1", "assignedTech": "20", "employeeID": "22", "date": "2026-01-05", "start": "08:00:00"},
        },
        "route": {
            70: {"routeID": "70", "officeID": "7", "title": "Mowing", "date": day, "assignedTech": "20"},
            71: {"routeID": "71", "officeID": "7", "title": "FWC and Pest", "date": day, "assignedTech": "21"},
        },
        "note": {},
        "task": {},
        "review": {},
    }


@dataclass
class Env:
    ctx: Ctx
    fake: FakeFR
    app: Any
    http: httpx.AsyncClient
    storage: MemoryStorage
    mailer: MemoryMailer
    texter: MemoryTexter
    pusher: MemoryPusher

    def fr_calls(self, entity: str, action: str) -> list[Any]:
        return [r for r in self.fake.requests if r.entity == entity and r.action == action]

    def reconfigure(self, **changes: Any) -> None:
        self.ctx.cfg = replace(self.ctx.cfg, **changes)
        self.ctx.fr.cfg = self.ctx.cfg

    async def one(self, query: Any) -> Any:
        async with self.ctx.engine.connect() as conn:
            return (await conn.execute(query)).first()

    async def all(self, query: Any) -> list[Any]:
        async with self.ctx.engine.connect() as conn:
            return list((await conn.execute(query)).all())

    async def run(self, statement: Any) -> None:
        async with self.ctx.engine.begin() as conn:
            await conn.execute(statement)

    async def video(self, video_id: str) -> Any:
        return await self.one(sa.select(db.videos).where(db.videos.c.id == video_id))

    async def finished(self, *customer_ids: int, ago: timedelta | None = None, force: bool = True) -> None:
        """Mark stops finished the way a sync would have seen it, `ago` in the past:
        every appointment completed (when `force`), each with the time we first saw it
        done. No customer IDs means every stop."""
        now = db.now()
        seen = now - (ago or timedelta())
        query = sa.select(db.stops)
        if customer_ids:
            query = query.where(db.stops.c.customer_id.in_(customer_ids))
        for row in await self.all(query):
            appointments = []
            for a in row.appointments:
                a = dict(a)
                if force:
                    a["status"] = 1
                if a["status"] == 1:
                    a["doneSeenAt"] = seen.isoformat()
                appointments.append(a)
            done = all(a["status"] == 1 for a in appointments)
            await self.run(
                sa.update(db.stops)
                .where(db.stops.c.day == row.day, db.stops.c.customer_id == row.customer_id)
                .values(appointments=appointments, done=done, done_seen_at=seen if done else None)
            )

    async def flags(self, kind: str | None = None, *, open_only: bool = True) -> list[Any]:
        query = sa.select(db.flags)
        if kind:
            query = query.where(db.flags.c.kind == kind)
        if open_only:
            query = query.where(db.flags.c.cleared_at.is_(None))
        return await self.all(query)

    async def actions(self) -> list[str]:
        return [r.action for r in await self.all(sa.select(db.events).order_by(db.events.c.id))]

    async def window_closed(self, video_id: str) -> None:
        """Jump past the tech's undo window for one video."""
        await self.run(
            sa.update(db.videos).where(db.videos.c.id == video_id).values(email_due_at=db.now() - timedelta(seconds=5))
        )


async def make_env(tmp_path: Any, **cfg_changes: Any) -> Env:
    os.environ.setdefault("FR_OFFICE_ID", "7")
    url = os.environ.get("VIDEO_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path}/video.db"
    base = config.load()
    cfg = replace(
        base,
        database_url=url,
        base_url=BASE,
        tz=TZ,
        office_id=7,
        fr_writes=True,
        fr_write_customer_ids=None,
        fr_employee_id=9,
        fr_note_type_id=5,
        fr_sales_task_category_id=10002,
        sync_seconds=180,
        work_start_hour=0,
        work_end_hour=24,
        mail_backend="resend",
        mail_reply_to="office@zest.test",
        email_mode="live",
        email_test_to="",
        email_customer_ids=None,
        owner_emails=(OWNER,),
        demo_contact=DEMO_CONTACT,
        demo_code=DEMO_CODE,
        secret="test-secret",
        office_phone="+19165550100",
        resend_webhook_secret="whsec_dGVzdC1zZWNyZXQ=",
        # Where this is running and the start-up overrides are never taken from the machine
        # the tests run on. With one of Railway's own variables in the environment
        # (`railway run pytest`, or a box Railway hosts) every test would otherwise be
        # taken for a real deployment and refused; an override left set would switch a
        # gate off. A test that wants either says so itself.
        platform="",
        allow_throwaway_storage=False,
        start_with_files_missing=False,
        start_with_new_database=False,
    )
    cfg = replace(cfg, **cfg_changes)
    if config.public_https(cfg.base_url) and "allow_throwaway_storage" not in cfg_changes:
        # The one case that is not a test: the browser rig (tests/browser/serve.py) given a
        # public https address, so that a real TV or a phone off the Wi-Fi can reach it.
        # That makes it a real deployment in the server's eyes, and it starts only with the
        # same loud override a demo needs, set by the person running it.
        cfg = replace(cfg, allow_throwaway_storage=base.allow_throwaway_storage)
    fake = FakeFR(data=seed())
    storage, mailer, texter, pusher = MemoryStorage(), MemoryMailer(), MemoryTexter(), MemoryPusher()
    ctx = build_ctx(cfg, fr_client=_client_for(fake), storage=storage, mailer=mailer, texter=texter, pusher=pusher)
    if not url.startswith("sqlite"):
        async with ctx.engine.begin() as conn:  # a shared Postgres: start every test clean
            await conn.run_sync(db.metadata.drop_all)
    await db.create_all(ctx.engine)
    await auth.ensure_owners(ctx)
    app = build_app(ctx, run_jobs=False)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE)
    return Env(ctx, fake, app, http, storage, mailer, texter, pusher)


def code_from(text: str) -> str:
    match = re.search(r"\b(\d{6})\b", text)
    assert match, f"no 6-digit code in {text!r}"
    return match.group(1)


async def login(env: Env, contact: str = "916-555-0101", build: int = 1) -> dict[str, str]:
    """Sign a tech in the way the app does; returns the headers for later calls."""
    before = len(env.mailer.sent)
    r = await env.http.post("/v1/auth/request-code", json={"contact": contact})
    assert r.status_code == 200, r.text
    assert len(env.mailer.sent) == before + 1, "no code was emailed"
    code = code_from(env.mailer.sent[-1].text)
    r = await env.http.post(
        "/v1/auth/verify",
        json={"contact": contact, "code": code, "deviceName": "iPhone", "deviceModel": "iPhone15,2",
              "osVersion": "18.1", "appBuild": build},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "X-Zest-Build": str(build)}


async def demo_login(env: Env) -> dict[str, str]:
    r = await env.http.post("/v1/auth/request-code", json={"contact": DEMO_CONTACT})
    assert r.status_code == 200
    r = await env.http.post("/v1/auth/verify", json={"contact": DEMO_CONTACT, "code": DEMO_CODE, "appBuild": 1})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}", "X-Zest-Build": "1"}


def video_body(customer_id: int | None = 501, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "stopDate": today(),
        "customerId": customer_id,
        "appointmentIds": [],
        "notOnMyList": customer_id is None,
        "recordedAt": (datetime.now(timezone.utc) - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "durationSec": 14.6,
        "sizeBytes": 5000,
        "location": {"lat": 38.67, "lng": -121.17, "accuracyM": 8},
        "signals": {"spoken": "match", "gps": "match", "checkIn": "unknown"},
        "override": False,
        "recommend": False,
        "spokenText": "This is Pat at the Smith home on Main Street.",
    }
    body.update(changes)
    return body


async def film(env: Env, headers: dict[str, str], customer_id: int | None = 501, **changes: Any) -> str:
    """Register, 'upload' to the fake bucket, and confirm. Returns the video ID."""
    body = video_body(customer_id, **changes)
    r = await env.http.post("/v1/videos", json=body, headers=headers)
    assert r.status_code == 200, r.text
    key = (await env.video(body["id"])).object_key
    env.storage.objects[key] = b"v" * body["sizeBytes"]
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert r.status_code == 200, r.text
    return body["id"]


async def office_login(env: Env, email: str = OWNER) -> str:
    """Sign in to the office page; returns the CSRF token for forms. The cookie stays on env.http."""
    r = await env.http.post("/office/login", data={"email": email})
    assert r.status_code == 200, r.text
    code = code_from(env.mailer.sent[-1].text)
    r = await env.http.post("/office/verify", data={"email": email, "code": code})
    assert r.status_code == 303, r.text
    page = await env.http.get("/office")
    assert page.status_code == 200, page.text
    match = re.search(r'name="csrf" value="([0-9a-f]{64})"', page.text)
    assert match
    return match.group(1)
