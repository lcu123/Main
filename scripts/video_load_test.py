#!/usr/bin/env python3
"""Stress test for the Visit Video server.

Starts the real server on a local port against a fake FieldRoutes and an in-memory
bucket, then plays a whole day at once: every tech opens the app, films every stop,
uploads and confirms, all at the same time. Prints response times and checks the
promises that matter: nothing lost, nothing doubled, one email per customer.

    python scripts/video_load_test.py --techs 15 --stops 15
    VIDEO_DATABASE_URL=postgresql+asyncpg://... python scripts/video_load_test.py --techs 200

Never point this at the live server: it creates its own data and wipes the tables
of the database it is given.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("FR_SUBDOMAIN", "zest")
os.environ.setdefault("FR_AUTH_KEY", "key")
os.environ.setdefault("FR_AUTH_TOKEN", "token")
os.environ.setdefault("FR_OFFICE_ID", "7")

import httpx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
import uvicorn  # noqa: E402

from conftest import FakeFR, _client_for  # noqa: E402

from zest_video import auth, config, db, security, videos  # noqa: E402
from zest_video.app import build_app, build_ctx  # noqa: E402
from zest_video.notify import MemoryMailer, MemoryPusher, MemoryTexter  # noqa: E402
from zest_video.storage import MemoryStorage  # noqa: E402

TZ_NAME = "America/Los_Angeles"


def make_day(techs: int, stops: int) -> dict:
    from zoneinfo import ZoneInfo

    day = datetime.now(ZoneInfo(TZ_NAME)).date().isoformat()
    data: dict = {"office": {7: {"officeID": 7}}, "employee": {}, "customer": {}, "appointment": {}, "route": {},
                  "serviceType": {89: {"typeID": "89", "officeID": "7", "description": "Lawn Care"}}}
    appt = 100000
    for t in range(techs):
        tech_id = 1000 + t
        data["employee"][tech_id] = {"employeeID": str(tech_id), "officeID": "7", "fname": f"Tech{t}", "lname": "Load",
                                     "type": "1", "active": "1", "email": f"tech{t}@example.com", "phone": ""}
        data["route"][tech_id] = {"routeID": str(tech_id), "officeID": "7", "date": day, "assignedTech": str(tech_id)}
        for s in range(stops):
            customer_id = 200000 + t * 100 + s
            data["customer"][customer_id] = {
                "customerID": str(customer_id), "officeID": "7", "fname": f"First{s}", "lname": f"Last{t}x{s}",
                "email": f"c{customer_id}@example.com", "address": f"{100 + s} Load St", "city": "Folsom",
                "state": "CA", "zip": "95630", "lat": "38.67", "lng": "-121.17", "emailReminders": "1",
            }
            for _ in range(1 + (s % 3 == 0)):  # every third customer has two appointments in the visit
                appt += 1
                data["appointment"][appt] = {
                    "appointmentID": str(appt), "officeID": "7", "customerID": str(customer_id), "routeID": str(tech_id),
                    "type": "89", "status": "1", "assignedTech": str(tech_id), "date": day, "start": "08:00:00",
                }
    return data


class Timer:
    def __init__(self) -> None:
        self.samples: dict[str, list[float]] = {}
        self.errors: dict[str, int] = {}

    async def call(self, name: str, coro) -> httpx.Response:
        start = time.perf_counter()
        response = await coro
        self.samples.setdefault(name, []).append((time.perf_counter() - start) * 1000)
        if response.status_code >= 400:
            self.errors[f"{name} {response.status_code}"] = self.errors.get(f"{name} {response.status_code}", 0) + 1
        return response

    def report(self) -> None:
        print(f"\n{'call':<12}{'count':>7}{'median':>10}{'95%':>10}{'slowest':>10}   (milliseconds)")
        for name, values in self.samples.items():
            values = sorted(values)
            p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
            print(f"{name:<12}{len(values):>7}{statistics.median(values):>10.0f}{p95:>10.0f}{values[-1]:>10.0f}")
        print("errors:", self.errors or "none")


async def run(techs: int, stops: int, port: int, concurrency: int) -> int:
    url = os.environ.get("VIDEO_DATABASE_URL") or f"sqlite+aiosqlite:///{tempfile.mkdtemp()}/load.db"
    cfg = replace(
        config.load(), database_url=url if "+" in url.split("://")[0] else url.replace("postgresql://", "postgresql+asyncpg://"),
        base_url=f"http://127.0.0.1:{port}", office_id=7, email_mode="live", mail_backend="resend", fr_writes=True,
        fr_note_type_id=5, fr_employee_id=9, work_start_hour=0, work_end_hour=24, sync_seconds=180,
    )
    fake = FakeFR(data=make_day(techs, stops))
    storage, mailer = MemoryStorage(), MemoryMailer()
    ctx = build_ctx(cfg, fr_client=_client_for(fake), storage=storage, mailer=mailer, texter=MemoryTexter(), pusher=MemoryPusher())
    async with ctx.engine.begin() as conn:
        await conn.run_sync(db.metadata.drop_all)
    await db.create_all(ctx.engine)
    await auth.sync_techs(ctx)

    # Sign every tech in directly (the code flow is rate limited on purpose).
    tokens: dict[int, str] = {}
    now = db.now()
    async with ctx.engine.begin() as conn:
        for t in range(techs):
            token = security.new_token()
            tokens[1000 + t] = token
            await conn.execute(sa.insert(db.devices).values(
                id=str(uuid.uuid4()), tech_id=1000 + t, token_hash=security.digest(token), name="load", model="load",
                os_version="0", app_build=1, apns_token="", apns_sandbox=False, created_at=now, last_seen_at=now))

    server = uvicorn.Server(uvicorn.Config(build_app(ctx, run_jobs=False), host="127.0.0.1", port=port, log_level="warning"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)

    timer = Timer()
    gate = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    filmed: list[str] = []

    async with httpx.AsyncClient(base_url=cfg.base_url, limits=limits, timeout=60) as http:

        async def one_tech(tech_id: int) -> None:
            headers = {"Authorization": f"Bearer {tokens[tech_id]}", "X-Zest-Build": "1"}
            async with gate:
                today = (await timer.call("today", http.get("/v1/today", headers=headers))).json()

            async def one_stop(stop: dict) -> None:
                body = {
                    "id": str(uuid.uuid4()), "stopDate": today["date"], "customerId": stop["customerId"],
                    "appointmentIds": stop["appointmentIds"], "notOnMyList": False,
                    "recordedAt": (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "durationSec": 14.5, "sizeBytes": 5000, "location": {"lat": 38.67, "lng": -121.17, "accuracyM": 8},
                    "signals": {"spoken": "match", "gps": "match", "checkIn": "unknown"}, "override": False,
                    "recommend": False, "spokenText": "load test",
                }
                async with gate:
                    # The phone retries on bad signal: send every registration twice, at once.
                    first, _ = await asyncio.gather(
                        timer.call("register", http.post("/v1/videos", json=body, headers=headers)),
                        timer.call("register", http.post("/v1/videos", json=body, headers=headers)),
                    )
                if first.status_code != 200:
                    return
                storage.objects[f"videos/{today['date']}/{body['id']}.mp4"] = b"v" * 5000
                async with gate:
                    done, _ = await asyncio.gather(
                        timer.call("complete", http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": 5000}, headers=headers)),
                        timer.call("complete", http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": 5000}, headers=headers)),
                    )
                if done.status_code == 200:
                    filmed.append(body["id"])

            await asyncio.gather(*(one_stop(s) for s in today["stops"]))
            async with gate:
                await timer.call("today", http.get("/v1/today", headers=headers))

        started = time.perf_counter()
        await asyncio.gather(*(one_tech(t) for t in tokens))
        wall = time.perf_counter() - started

        # The customer side: open every watch page once the emails are out.
        async with ctx.engine.begin() as conn:
            await conn.execute(sa.update(db.videos).values(email_due_at=db.now() - timedelta(seconds=1)))
        fr_before = len(fake.requests)
        send_started = time.perf_counter()
        sent = 0
        while True:
            batch = await videos.send_due(ctx)
            sent += batch
            if batch == 0:
                break
        send_wall = time.perf_counter() - send_started
        fr_for_email = len(fake.requests) - fr_before

        async with ctx.engine.connect() as conn:
            rows = (await conn.execute(sa.select(db.videos.c.token, db.videos.c.state, db.videos.c.customer_id))).all()
        sample = [r.token for r in rows if r.state == "sent"][:200]

        async def watch(token: str) -> None:
            async with gate:
                await timer.call("watch page", http.get(f"/w/{token}"))

        ctx.limiter.reset()
        await asyncio.gather(*(watch(t) for t in sample[:100]))  # 100 pages from one address stays under the rate limit

    server.should_exit = True
    await task

    expected = techs * stops
    states: dict[str, int] = {}
    for r in rows:
        states[r.state] = states.get(r.state, 0) + 1
    emails = [m.to for m in mailer.sent]
    timer.report()
    print(f"\n{techs} techs x {stops} stops = {expected} videos, {concurrency} requests at a time")
    print(f"whole day played in {wall:.1f} s  ({expected / wall:.0f} videos a second)")
    print(f"videos on the server: {len(rows)}   by state: {states}")
    print(f"emails sent: {len(emails)} in {send_wall:.1f} s   distinct customers: {len(set(emails))}   FieldRoutes calls for them: {fr_for_email}")
    print(f"FieldRoutes calls in total: {len(fake.requests)}  (reads {fake.reads_today}, writes {fake.writes_today})")

    problems = []
    if len(rows) != expected:
        problems.append(f"expected {expected} videos, found {len(rows)}")
    if states.get("sent", 0) != expected:
        problems.append(f"expected {expected} sent, found {states}")
    if len(emails) != expected or len(set(emails)) != expected:
        problems.append("emails are not exactly one per customer")
    if timer.errors:
        problems.append(f"errors: {timer.errors}")
    print("\nRESULT:", "PASS" if not problems else "FAIL: " + "; ".join(problems))
    await ctx.engine.dispose()
    return 0 if not problems else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--techs", type=int, default=15)
    parser.add_argument("--stops", type=int, default=15)
    parser.add_argument("--port", type=int, default=18200)
    parser.add_argument("--concurrency", type=int, default=50)
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.techs, args.stops, args.port, args.concurrency)))
