"""A restart loses nothing, and makes nothing happen twice.

Railway replaces the container at every deploy and may restart it at any time. Each test
here stops the server at one point in a video's life and starts a new one: a new context
and app over the same database and the same stand-in bucket, with everything that lived
in memory gone (rate limits, the time of the last look at FieldRoutes, the job timers, the
settings cache, the FieldRoutes client and its quota count). A start runs what the real
one runs: the start-up checks, then the first pass of the server's own job loop.

The mail, text and push recorders stand for the outside world, so they are kept across a
restart. So is the fake FieldRoutes: it is the same FieldRoutes.

What these tests first found that did not survive is at the bottom. Those gaps are
closed now, and the tests that pinned them are plain tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from conftest import _client_for
from video_helpers import (
    BASE, STAFF, Env, code_from, film, fr_time, login, make_env, minutes_ago, office_login, seed, today, video_body,
)

from zest_video import app as video_app
from zest_video import auth, db, jobs, videos
from zest_video.app import build_app, build_ctx
from zest_video.core import get_settings, set_setting
from zest_video.fr import FieldRoutes, FieldRoutesError
from zest_video.notify import ResendMailer
from zest_video.route import sync_day

CONTACT = "916-555-0101"  # Pat's number in the seed


class Killed(BaseException):
    """The process died right here. Not an Exception on purpose: nothing may catch it and
    tidy up, because a killed process tidies nothing."""


class DyingDatabase:
    """The server's database with a kill switch on it. Every save the server makes goes
    through `begin()`, so this can stop the server at any save: just before save number
    `at` is written (`before`, and then that save is undone, as a real kill would undo it),
    just after it, or just after the first save that makes `when()` true. Killed is raised
    in the middle of whatever the server was doing and nothing after that line runs. It
    kills once."""

    def __init__(self, engine: Any, *, at: int = 0, before: bool = False, when: Any = None):
        self.engine, self.at, self.before, self.when = engine, at, before, when
        self.saves = 0
        self.dead = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.engine, name)

    def begin(self) -> Any:
        return _Save(self)


class _Save:
    def __init__(self, database: DyingDatabase):
        self.database = database

    async def __aenter__(self) -> Any:
        self.inner = self.database.engine.begin()
        return await self.inner.__aenter__()

    async def __aexit__(self, kind: Any, exc: Any, tb: Any) -> Any:
        d = self.database
        if kind is not None or d.dead:
            return await self.inner.__aexit__(kind, exc, tb)
        d.saves += 1
        if d.before and d.saves == d.at:
            d.dead = True
            killed = Killed()
            await self.inner.__aexit__(Killed, killed, None)  # nothing of this save is kept
            raise killed
        await self.inner.__aexit__(None, None, None)
        if (not d.before and d.saves == d.at) or (d.when is not None and await d.when()):
            d.dead = True
            raise Killed
        return False


def kill_switch(env: Env, **how: Any) -> DyingDatabase:
    """Put the kill switch on the running server's database. A restart takes it off."""
    env.ctx.engine = DyingDatabase(env.ctx.engine, **how)  # type: ignore[assignment]
    return env.ctx.engine  # type: ignore[return-value]


class Clock:
    """The server's clock, which a test can move forward."""

    def __init__(self) -> None:
        self.ahead = timedelta()

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self.ahead

    def forward(self, **amount: float) -> None:
        self.ahead += timedelta(**amount)

    def to(self, when: datetime) -> None:
        self.ahead += when - self.now()


@pytest.fixture(autouse=True)
def not_this_machines_own_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests may themselves be run on Railway (`railway run pytest`, or a box that
    Railway hosts), or on a machine with an override left set. The shared fixture reads the
    environment, so without this every test here would be taken for a real deployment
    and refused."""
    for name in list(os.environ):
        if name.startswith("RAILWAY_") or name in (
            "VIDEO_ALLOW_THROWAWAY_STORAGE", "VIDEO_START_WITH_FILES_MISSING", "VIDEO_START_WITH_NEW_DATABASE",
        ):
            monkeypatch.delenv(name)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(db, "now", c.now)
    return c


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    # Two switches the fake FieldRoutes does not have: down for good, or refusing writes.
    e.fake.down = False
    e.fake.writes_down = False
    real = e.fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        is_read = request.url.path.rsplit("/", 1)[-1] in ("search", "get")
        if e.fake.down or (e.fake.writes_down and not is_read):
            return httpx.Response(200, json={"success": False, "errorMessage": "FieldRoutes is down"})
        return real(request)

    e.fake.handler = handler
    e.ctx.fr = FieldRoutes(_client_for(e.fake), e.ctx.cfg)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def just_now(clock: Clock) -> str:
    """Filmed a minute ago by the server's clock (inside the seeded visit's check-in time)."""
    return stamp(clock.now() - timedelta(minutes=1))


def parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


async def ready(env: Env) -> dict[str, str]:
    headers = await login(env)
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
    env.mailer.sent.clear()
    return headers


async def one_pass(*servers: Env) -> None:
    """The server's own job loop, run for exactly its first pass: every job once, in the
    loop's order, with the loop's own handling of failures. That is what a start does.
    Given two servers it runs both loops side by side, as at a deploy.

    A job that fails in a way the loop only logs (anything but FieldRoutes being down)
    fails the test."""
    finished: set[int] = set()
    failures: list[tuple[str, BaseException]] = []

    def watched(name: str, job: Any) -> Any:
        async def run(ctx: Any) -> Any:
            try:
                return await job(ctx)
            except FieldRoutesError:
                raise
            except Exception as exc:
                failures.append((name, exc))
                raise

        return run

    async def end_of_pass(ctx: Any) -> None:
        finished.add(id(ctx))

    original = jobs.JOBS
    jobs.JOBS = [*((name, every, watched(name, job)) for name, every, job in original),
                 ("end of the pass", 10**9, end_of_pass)]
    tasks = [asyncio.create_task(jobs.run_forever(s.ctx, tick=3600)) for s in servers]
    try:
        for _ in range(3000):
            if all(id(s.ctx) in finished or t.done() for s, t in zip(servers, tasks)):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("the job loop never finished its first pass")
    finally:
        jobs.JOBS = original
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert failures == [], f"a job failed: {failures}"


async def restart(env: Env, *, mailer: Any = None, jobs_too: bool = True) -> None:
    """Stop the server and start a new one over the same database and bucket."""
    cookies = env.http.cookies  # the office's browser keeps its cookie; the server keeps nothing
    await env.http.aclose()
    await env.ctx.engine.dispose()
    if mailer is not None:
        env.mailer = mailer
    env.ctx = build_ctx(
        replace(env.ctx.cfg), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
        texter=env.texter, pusher=env.pusher,
    )
    env.app = build_app(env.ctx, run_jobs=False)
    env.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=env.app), base_url=BASE, cookies=cookies)
    await video_app.start_up(env.ctx)
    if jobs_too:
        await one_pass(env)


def fr_writes(env: Env, entity: str) -> list[Any]:
    return env.fr_calls(entity, "create")


# == one video, with a restart after every step ========================================


async def test_one_video_with_a_restart_after_every_step_reaches_the_customer_exactly_once(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await restart(env)

    body = video_body(501, recordedAt=just_now(clock), recommend=True)
    r = await env.http.post("/v1/videos", json=body, headers=headers)
    assert r.status_code == 200, "the phone's sign-in from before the restart still works"
    upload = r.json()["upload"]
    await restart(env)

    # The phone uploads with the link it was handed before the restart. The link is signed
    # with the bucket's key and checked by the bucket; the server keeps no list of links.
    key = (await env.video(body["id"])).object_key
    assert key in upload["url"]
    env.storage.objects[key] = b"v" * body["sizeBytes"]
    await restart(env)

    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "held"
    due = parse(r.json()["emailAt"])
    assert due == parse(body["recordedAt"]) + timedelta(minutes=10), "ten minutes from the recording, restarts or not"
    await restart(env)
    assert env.mailer.sent == [], "inside the tech's ten minutes"

    clock.forward(minutes=5)
    await restart(env)
    assert env.mailer.sent == []

    clock.to(due + timedelta(seconds=2))
    await restart(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "the email went, once"
    await restart(env)
    await restart(env)
    assert len(env.mailer.sent) == 1, "and more restarts do not send it again"

    video = await env.video(body["id"])
    assert video.state == "sent" and timedelta(0) <= video.emailed_at - due < timedelta(seconds=30)
    link = f"/w/{video.token}"
    assert f"{BASE}{link}" in env.mailer.sent[0].text
    assert (await env.http.get(link)).status_code == 200, "the customer's link works on the new server"
    await restart(env)
    assert (await env.http.get(f"{link}/video")).status_code == 302
    assert (await env.http.post(f"{link}/played")).status_code == 204
    await restart(env)
    assert (await env.http.post(f"{link}/score", data={"stars": "5", "comment": "Great job"})).status_code == 303
    await restart(env)
    await restart(env)

    score = await videos.get_score(env.ctx, body["id"])
    assert (score.stars, score.comment) == (5, "Great job")
    (note,) = fr_writes(env, "note")
    assert f"{BASE}{link}" in note.one("notes"), "the link is on the customer's account, once"
    assert len(fr_writes(env, "task")) == 1, "the follow-up for what the tech recommended, once"
    assert len(fr_writes(env, "review")) == 1, "the score, once"
    assert [f.kind for f in await env.flags()] == ["recommend"]

    assert len(await env.all(sa.select(db.videos))) == 1
    actions = await env.actions()
    for once in ("video_registered", "video_uploaded", "email_sent", "video_played", "score_saved"):
        assert actions.count(once) == 1, once
    today = (await env.http.get("/v1/today", headers=headers)).json()
    assert today["stops"][0]["video"] == {"id": body["id"], "state": "watched", "byYou": True}


# == after register, before the upload =================================================


async def test_a_restart_between_register_and_upload_loses_nothing(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    body = video_body(501, recordedAt=just_now(clock))
    first = (await env.http.post("/v1/videos", json=body, headers=headers)).json()
    await restart(env)

    # A phone that never got its answer asks again: same video, same file, no second row.
    again = await env.http.post("/v1/videos", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["state"] == "awaiting_upload"
    key = (await env.video(body["id"])).object_key
    assert key in first["upload"]["url"] and key in again.json()["upload"]["url"]
    assert len(await env.all(sa.select(db.videos))) == 1

    # The new server does not take the phone's word for an upload it cannot see.
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "not_uploaded", "so the phone keeps its file"

    env.storage.objects[key] = b"v" * body["sizeBytes"]
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "held"


async def test_a_video_stuck_on_a_phone_is_chased_once_however_often_the_server_restarts(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await env.http.post("/v1/devices/push", json={"apnsToken": "abcd12", "sandbox": False}, headers=headers)
    body = video_body(501, recordedAt=just_now(clock))
    await env.http.post("/v1/videos", json=body, headers=headers)

    clock.forward(minutes=20)
    await restart(env)
    assert env.pusher.sent == [], "the 30 minutes are counted from the video, not from the restart"
    clock.forward(minutes=11)
    for _ in range(3):
        await restart(env)
    assert len(env.pusher.sent) == 1, "one nudge to the tech, not one for each restart"

    clock.forward(hours=2, minutes=20)
    await restart(env)
    assert await env.flags("stuck_upload") == [], "2 hours 51 minutes: not yet"
    clock.forward(minutes=10)
    for _ in range(3):
        await restart(env)
    assert len(await env.flags("stuck_upload")) == 1 and len(env.pusher.sent) == 1

    # It arrives at last. The flag clears, and the email goes a minute later, once.
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert r.json()["state"] == "held"
    await restart(env)
    assert await env.flags("stuck_upload") == [] and env.mailer.sent == []
    clock.forward(minutes=2)
    await restart(env)
    await restart(env)
    assert len(env.mailer.sent) == 1


# == after the upload, before "complete" ===============================================


async def test_a_restart_after_the_upload_and_before_complete(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    body = video_body(501, recordedAt=just_now(clock))
    await env.http.post("/v1/videos", json=body, headers=headers)
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    clock.forward(minutes=3)
    await restart(env)
    assert (await env.video(body["id"])).state == "awaiting_upload", "a file in the bucket is not a confirmed video"
    assert env.mailer.sent == []

    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "held"
    assert parse(r.json()["emailAt"]) == parse(body["recordedAt"]) + timedelta(minutes=10), (
        "the tech's ten minutes still run from the recording"
    )


async def test_an_answer_lost_to_the_restart_is_given_again_and_changes_nothing(env: Env, clock: Clock) -> None:
    """The server saved the confirm, and was stopped before its answer reached the phone."""
    headers = await ready(env)
    body = video_body(501, recordedAt=just_now(clock))
    await env.http.post("/v1/videos", json=body, headers=headers)
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    first = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    clock.forward(minutes=4)
    await restart(env)

    again = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert again.status_code == 200 and again.json() == first.json(), "same state, same email time"
    assert (await env.actions()).count("video_uploaded") == 1


# == in the middle of the ten-minute hold ==============================================


async def test_restarts_all_through_the_hold_send_one_email_at_the_right_time(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    due = (await env.video(video_id)).email_due_at

    assert timedelta(minutes=8) < due - clock.now() <= timedelta(minutes=9), "ten minutes from the recording"
    for minute in range(1, 9):
        clock.forward(minutes=1)
        await restart(env)
        assert env.mailer.sent == [], f"minute {minute}: not early"
        mine = (await env.http.get("/v1/today", headers=headers)).json()["videos"][0]
        assert (mine["state"], parse(mine["emailAt"])) == ("held", due), "the phone is told the same time throughout"

    clock.to(due - timedelta(seconds=2))
    await one_pass(env)
    assert env.mailer.sent == [], "two seconds before: still not early"

    clock.to(due + timedelta(seconds=2))
    await restart(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "not late, and not zero"
    video = await env.video(video_id)
    assert video.state == "sent" and due <= video.emailed_at < due + timedelta(seconds=30)

    for _ in range(3):
        clock.forward(minutes=6)
        await restart(env)
    assert len(env.mailer.sent) == 1, "and not two"


async def test_the_techs_undo_still_works_after_a_restart_and_then_no_email_ever_goes(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    key = (await env.video(video_id)).object_key
    clock.forward(minutes=5)
    await restart(env)

    r = await env.http.delete(f"/v1/videos/{video_id}", headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "deleted"
    assert key not in env.storage.objects
    clock.forward(minutes=30)
    await restart(env)
    await restart(env)
    assert env.mailer.sent == [] and fr_writes(env, "note") == []


async def test_a_restart_does_not_reopen_the_techs_ten_minutes(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await restart(env)
    r = await env.http.delete(f"/v1/videos/{video_id}", headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "too_late"


async def test_the_old_and_the_new_server_side_by_side_at_a_deploy_send_one_email(env: Env, clock: Clock) -> None:
    """At a deploy the new server is up before the old one is stopped, and both run jobs."""
    headers = await ready(env)
    ids = [await film(env, headers, customer, recordedAt=just_now(clock)) for customer in (501, 502)]
    clock.forward(minutes=11)

    new = replace(env)
    new.ctx = build_ctx(
        replace(env.ctx.cfg), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
        texter=env.texter, pusher=env.pusher,
    )
    try:
        await video_app.start_up(new.ctx)
        await one_pass(env, new)
        await one_pass(new, env)
    finally:
        await new.ctx.engine.dispose()
    assert sorted(m.to for m in env.mailer.sent) == ["bob@example.com", "jane@example.com"]
    assert {(await env.video(v)).state for v in ids} == {"sent"}


# == with an email half sent ===========================================================


async def test_killed_after_the_email_was_claimed_and_before_it_was_handed_over(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)

    async def die(email: Any) -> str:
        raise Killed

    env.mailer.send = die  # type: ignore[method-assign]
    await one_pass(env)
    del env.mailer.send
    assert (await env.video(video_id)).state == "held" and env.mailer.sent == []

    await restart(env)
    assert env.mailer.sent == [], "the dead server's claim on it is honoured for five minutes"
    clock.forward(minutes=4)
    await restart(env)
    assert env.mailer.sent == []
    clock.forward(minutes=2)
    await restart(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "then it goes: not zero"
    clock.forward(minutes=10)
    await restart(env)
    assert len(env.mailer.sent) == 1 and (await env.video(video_id)).state == "sent"


class MailService:
    """Stands in for the mail provider, with its rule for a repeated Idempotency-Key as
    its documentation gives it, both halves. The same key with the same email again is
    answered with the first one's id and delivers nothing new. The same key with a
    different email is refused: 409, `invalid_idempotent_request`. The first half alone
    would be kinder than the real service. (Not yet proven with real keys.)"""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.inbox: list[dict[str, Any]] = []
        self.by_key: dict[str, tuple[str, bytes]] = {}
        self.refused = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("idempotency-key", "")
        self.requests.append(key)
        if key and key in self.by_key:
            first_id, first_email = self.by_key[key]
            if request.content != first_email:
                self.refused += 1
                return httpx.Response(409, json={
                    "statusCode": 409, "name": "invalid_idempotent_request",
                    "message": "Same idempotency key used with a different request payload.",
                })
            return httpx.Response(200, json={"id": first_id})
        self.inbox.append(json.loads(request.content))
        self.by_key[key] = (f"email-{len(self.inbox)}", request.content)
        return httpx.Response(200, json={"id": self.by_key[key][0]})


async def test_killed_after_the_mail_service_took_the_email_and_before_that_was_written_down(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    service = MailService()

    def mailer() -> ResendMailer:
        return ResendMailer(env.ctx.cfg, transport=httpx.MockTransport(service.handler))

    dying = mailer()
    hand_over = dying.send

    async def send_then_die(email: Any) -> str:
        await hand_over(email)
        raise Killed

    dying.send = send_then_die  # type: ignore[method-assign]
    env.ctx.mailer = dying
    await one_pass(env)
    assert len(service.inbox) == 1 and (await env.video(video_id)).state == "held", "sent, and the server does not know"

    await restart(env, mailer=mailer())
    assert len(service.requests) == 1, "not tried again at once"
    clock.forward(minutes=6)
    await restart(env, mailer=mailer())

    assert len(service.requests) == 2 and service.requests[0] == service.requests[1] != "", (
        "the second try carries the same key as the first"
    )
    assert service.refused == 0, "and the very same email, byte for byte, or the mail service would refuse it"
    assert [m["to"] for m in service.inbox] == [["jane@example.com"]], "so the customer has one email, not two"
    video = await env.video(video_id)
    assert video.state == "sent" and video.email_id == "email-1"
    clock.forward(minutes=10)
    await restart(env, mailer=mailer())
    assert len(service.requests) == 2 and len(fr_writes(env, "note")) == 1


async def killed_after_the_hand_over_then_the_wording_changes(env: Env, clock: Clock) -> tuple[str, MailService]:
    """The mail service took the email and the server was killed before it could write
    that down. In the five minutes before the second try the owner tidies the subject
    line, so the second try is the same key with a different email: the documented case
    the mail service refuses."""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    service = MailService()

    def mailer() -> ResendMailer:
        return ResendMailer(env.ctx.cfg, transport=httpx.MockTransport(service.handler))

    dying = mailer()
    hand_over = dying.send

    async def send_then_die(email: Any) -> str:
        await hand_over(email)
        raise Killed

    dying.send = send_then_die  # type: ignore[method-assign]
    env.ctx.mailer = dying
    await one_pass(env)
    assert len(service.inbox) == 1 and (await env.video(video_id)).state == "held"

    await set_setting(env.ctx, "email_subject", "Your visit video from {tech}", "owner@zest.test")
    for _ in range(videos.EMAIL_MAX_ATTEMPTS + 2):
        clock.forward(minutes=6)
        await restart(env, mailer=mailer())
    return video_id, service


async def test_a_second_try_whose_wording_changed_still_leaves_the_customer_with_one_email(env: Env, clock: Clock) -> None:
    video_id, service = await killed_after_the_hand_over_then_the_wording_changes(env, clock)
    assert len(service.inbox) == 1, "one email reached the customer, however the retries went"
    assert len(set(service.requests)) == 1 and service.requests[0] != "", "every try carried the same key"
    assert service.refused >= 1, "and the mail service did refuse the changed one, as its documentation says"
    assert (await env.video(video_id)).token in service.inbox[0]["text"], "the email she has carries a link that works"


async def test_an_email_the_mail_service_already_has_is_not_written_down_as_never_sent(
    env: Env, clock: Clock
) -> None:
    """The customer has the email. The mail service refuses the second try (same key,
    other words), and that refusal means "I already have one under this key". It used to
    be counted as a failed send: six refusals later the record said "no email" and the
    office was flagged "The email could not be sent after 6 tries", which invites someone
    to send it again by hand. (This was a known gap; closed in notify.py.)"""
    video_id, service = await killed_after_the_hand_over_then_the_wording_changes(env, clock)
    assert len(service.inbox) == 1
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("sent", ""), "the record says what happened: it was sent"
    assert await env.flags("no_email") == [], "and the office is not told that no email went"
    assert service.refused == 1, "one refusal was enough: the server did not go on trying"
    assert len(fr_writes(env, "note")) == 1, "and the link is filed on her account, once"
    assert (await env.actions()).count("email_failed") == 0


async def test_any_other_refusal_from_the_mail_service_is_still_a_failed_send(env: Env) -> None:
    """Only the mail service's exact name for "that key is used with another email" counts
    as sent. Two tries at the very same moment, a plain 409, or words that are not its
    own must stay failures, or an email that never went would be written down as sent."""
    from zest_video.notify import AlreadyTaken, DeliveryError, Email

    answers: list[httpx.Response] = []
    mailer = ResendMailer(env.ctx.cfg, transport=httpx.MockTransport(lambda request: answers.pop(0)))
    email = Email(to="jane@example.com", subject="s", html="<p>h</p>", text="t", idempotency_key="video-1")
    for refusal in (
        httpx.Response(409, json={"statusCode": 409, "name": "concurrent_idempotent_requests", "message": "busy"}),
        httpx.Response(409, text="Conflict"),
        httpx.Response(409, json=["invalid_idempotent_request"]),
        httpx.Response(422, json={"statusCode": 422, "name": "invalid_idempotent_request", "message": "no"}),
    ):
        answers.append(refusal)
        with pytest.raises(DeliveryError) as failed:
            await mailer.send(email)
        assert failed.type is DeliveryError, "a plain failed send, to be tried again"
    # And an email sent with no key at all cannot be "already taken under its key".
    answers.append(httpx.Response(409, json={"statusCode": 409, "name": "invalid_idempotent_request", "message": "x"}))
    with pytest.raises(DeliveryError) as failed:
        await mailer.send(Email(to="jane@example.com", subject="s", html="<p>h</p>", text="t"))
    assert failed.type is DeliveryError
    # The one refusal that means "I have one already under this key" is told apart from all
    # of those. (It used to come back from the mailer as "sent". It proves only that the key
    # was seen before, so what it means is now decided by the emailer, which knows whether
    # an earlier try for this video failed. To anyone else it is a send that did not happen.)
    answers.append(httpx.Response(409, json={"statusCode": 409, "name": "invalid_idempotent_request", "message": "x"}))
    with pytest.raises(AlreadyTaken):
        await mailer.send(email)
    assert issubclass(AlreadyTaken, DeliveryError)


class MailServiceThatRemembersAFailedTry(MailService):
    """As MailService, but the first request under a key is answered with an error and
    delivers nothing, and the key is remembered all the same. Some services do keep the
    first answer under a key, errors included. Whether the real one does is NOT known, so
    the server must not be right only if it does not."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("idempotency-key", "")
        self.requests.append(key)
        if key not in self.by_key:
            self.by_key[key] = ("", request.content)
        elif request.content != self.by_key[key][1]:
            self.refused += 1
            return httpx.Response(409, json={"statusCode": 409, "name": "invalid_idempotent_request", "message": "x"})
        return httpx.Response(500, json={"statusCode": 500, "name": "internal_server_error", "message": "try later"})


async def test_the_mail_services_i_already_have_it_is_no_proof_after_a_try_that_failed(env: Env, clock: Clock) -> None:
    """ "I already have an email under this key" proves the key was seen before, not that
    an email reached anyone. When this server was stopped after handing an email over, it
    is the proof that was missing, and the record says sent (the test above). When an
    earlier try was answered with an error, nothing was handed over as far as this server
    knows, and the same answer proves nothing: the record must not say sent."""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    service = MailServiceThatRemembersAFailedTry()
    env.ctx.mailer = env.mailer = ResendMailer(env.ctx.cfg, transport=httpx.MockTransport(service.handler))
    clock.forward(minutes=11)
    await one_pass(env)  # the first try: the mail service says 500 and delivers nothing
    assert (await env.video(video_id)).state == "held" and (await env.actions()).count("email_failed") == 1

    await set_setting(env.ctx, "email_subject", "Your visit video from {tech}", "owner@zest.test")  # the wording changes
    clock.forward(minutes=6)
    await one_pass(env)
    assert service.inbox == [] and service.refused == 1, "nothing was ever delivered, and the second try was refused"
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "send_failed"), "the record does not say sent"
    (flag,) = await env.flags("no_email")
    assert flag.video_id == video_id and flag.detail == (
        "The mail service says it already has an email for this video, and this server has no record of one going "
        "out: its earlier tries were written down as failed. The customer may or may not have it. Ask the customer, "
        "or look in the mail service's list of sent emails. The link is saved on the account as a note."
    )
    assert len(fr_writes(env, "note")) == 1, "and the link is on the account, as the flag says"

    # The mail service will take nothing more under that key, so the server stops asking.
    for _ in range(3):
        clock.forward(minutes=6)
        await one_pass(env)
    assert len(service.requests) == 2 and len(await env.flags("no_email", open_only=False)) == 1


async def test_an_email_that_went_to_the_test_inbox_does_not_stand_for_the_customers(env: Env, clock: Clock) -> None:
    """Test mode: every customer email goes to the owner's own inbox. The server hands one
    over and is stopped before it can write that down, by the very deploy that switches
    customer emails to live. The second try is for the customer. It carried the same key
    as the first, so the mail service refused it, and the record said "sent to the
    customer" for an email only the test inbox ever got. The key now names the inbox."""
    env.reconfigure(email_mode="test", email_test_to="owner-test@zest.test")
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    service = MailService()

    def mailer() -> ResendMailer:
        return ResendMailer(env.ctx.cfg, transport=httpx.MockTransport(service.handler))

    dying = mailer()
    hand_over = dying.send

    async def send_then_die(email: Any) -> str:
        await hand_over(email)
        raise Killed

    dying.send = send_then_die  # type: ignore[method-assign]
    env.ctx.mailer = dying
    await one_pass(env)
    assert [m["to"] for m in service.inbox] == [["owner-test@zest.test"]] and (await env.video(video_id)).state == "held"

    env.reconfigure(email_mode="live")
    clock.forward(minutes=6)
    await restart(env, mailer=mailer())
    assert [m["to"] for m in service.inbox] == [["owner-test@zest.test"], ["jane@example.com"]], "the customer has hers"
    assert service.refused == 0 and service.requests[0] != service.requests[1] != ""
    video = await env.video(video_id)
    assert (video.state, video.email_to, video.email_id) == ("sent", "jane@example.com", "email-2")


# == with FieldRoutes writes waiting in the outbox =====================================


async def test_fieldroutes_writes_waiting_in_the_outbox_survive_restarts_and_are_written_once(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock), recommend=True)
    clock.forward(minutes=11)
    env.fake.writes_down = True
    await restart(env)  # the email goes; the note and the task are tried once and fail
    assert len(env.mailer.sent) == 1
    rows = await env.all(sa.select(db.fr_outbox).order_by(db.fr_outbox.c.id))
    assert [(r.kind, r.attempts, r.done_at) for r in rows] == [("note", 1, None), ("task", 1, None)]

    for _ in range(3):
        await restart(env)
    rows = await env.all(sa.select(db.fr_outbox).order_by(db.fr_outbox.c.id))
    assert [r.attempts for r in rows] == [1, 1], (
        "the wait before the next try is in the database: a restart does not hammer FieldRoutes"
    )

    env.fake.writes_down = False
    clock.forward(minutes=4)
    await restart(env)
    assert fr_writes(env, "note") == [], "four minutes: the five-minute wait is not over"
    clock.forward(minutes=2)
    await restart(env)
    (note,) = fr_writes(env, "note")
    (task,) = fr_writes(env, "task")
    token = (await env.video(video_id)).token
    assert f"{BASE}/w/{token}" in note.one("notes") and f"{BASE}/w/{token}" in task.one("task")

    for _ in range(3):
        clock.forward(hours=8)
        await restart(env)
    assert len(fr_writes(env, "note")) == 1 and len(fr_writes(env, "task")) == 1, "written once, for good"
    assert await env.flags("fr_write_failed") == []


async def test_writes_queued_and_never_tried_are_made_by_the_next_server(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    assert await videos.send_due(env.ctx) == 1  # the old server got this far, and no further
    row = await env.one(sa.select(db.fr_outbox))
    assert (row.kind, row.attempts, row.done_at) == ("note", 0, None)

    await restart(env)
    assert len(fr_writes(env, "note")) == 1 and (await env.one(sa.select(db.fr_outbox))).done_at is not None
    assert video_id in (await env.one(sa.select(db.fr_outbox))).dedupe


async def test_a_score_left_while_fieldroutes_is_down_reaches_it_after_a_restart(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await restart(env)
    token = (await env.video(video_id)).token
    env.fake.down = True
    assert (await env.http.post(f"/w/{token}/score", data={"stars": "2", "comment": "Missed a spot"})).status_code == 303
    await restart(env)
    assert fr_writes(env, "review") == []

    env.fake.down = False
    clock.forward(minutes=6)
    await restart(env)
    await restart(env)
    (review,) = fr_writes(env, "review")
    assert review.one("starRating") == "2" and review.one("feedback") == "Missed a spot"
    assert [f.kind for f in await env.flags()] == ["low_score"], "the office was told at once, FieldRoutes or not"


async def test_a_write_that_keeps_failing_still_ends_as_one_flag_with_a_restart_before_every_try(
    env: Env, clock: Clock
) -> None:
    headers = await ready(env)
    await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    env.fake.writes_down = True
    await restart(env)
    for wait in videos.OUTBOX_BACKOFF[1:]:
        clock.forward(minutes=wait + 1)
        await restart(env)
    row = await env.one(sa.select(db.fr_outbox))
    assert row.attempts == len(videos.OUTBOX_BACKOFF) and row.failed_at is not None
    (flag,) = await env.flags("fr_write_failed")
    assert "FieldRoutes is down" in flag.detail

    clock.forward(days=1)
    await restart(env)
    assert len(await env.flags("fr_write_failed")) == 1 and fr_writes(env, "note") == []


# == with a "finished, no video" check due =============================================


async def test_a_finished_stop_with_no_video_is_flagged_on_time_and_once_across_restarts(env: Env, clock: Clock) -> None:
    await ready(env)  # Pat has the app. Jane's stop is finished in FieldRoutes, and first seen now.

    async def first_seen() -> str:
        stop = await env.one(sa.select(db.stops).where(db.stops.c.customer_id == 501))
        return stop.appointments[0]["doneSeenAt"]

    seen = await first_seen()
    clock.forward(minutes=30)
    await restart(env)  # the new server reads the day from FieldRoutes again
    assert await first_seen() == seen, "a fresh look at FieldRoutes does not start the hour again"
    assert await env.flags("no_video") == []

    clock.forward(minutes=29)
    await restart(env)
    assert await env.flags("no_video") == [], "59 minutes: not early"

    clock.forward(minutes=2)
    await restart(env)
    (flag,) = await env.flags("no_video")
    assert (flag.customer_id, flag.tech_id) == (501, 20) and "finished with no video" in flag.detail

    for _ in range(3):
        clock.forward(minutes=10)
        await restart(env)
    assert len(await env.flags("no_video", open_only=False)) == 1, "one flag, not one for each restart"


async def test_a_late_video_clears_the_flag_and_a_restart_does_not_bring_it_back(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    clock.forward(minutes=61)
    await restart(env)
    assert len(await env.flags("no_video")) == 1

    await film(env, headers, 501, recordedAt=stamp(clock.now() - timedelta(minutes=50)))
    assert await env.flags("no_video") == []
    for _ in range(2):
        clock.forward(minutes=30)
        await restart(env)
    assert await env.flags("no_video") == [] and len(await env.flags("no_video", open_only=False)) == 1


# == what lives only in memory, and what a restart costs ================================


async def test_sign_ins_and_codes_are_in_the_database_and_rate_limits_are_not(env: Env) -> None:
    # A code texted just before the restart works just after it.
    assert (await env.http.post("/v1/auth/request-code", json={"contact": CONTACT})).status_code == 200
    code = code_from(env.mailer.sent[-1].text)
    await restart(env)
    r = await env.http.post("/v1/auth/verify", json={"contact": CONTACT, "code": code, "appBuild": 1})
    assert r.status_code == 200
    headers = {"Authorization": f"Bearer {r.json()['token']}", "X-Zest-Build": "1"}
    await restart(env)
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200, "the phone stays signed in"

    # Wrong tries are counted in the database, so a restart is no way round the lock.
    await env.http.post("/v1/auth/request-code", json={"contact": CONTACT})
    code = code_from(env.mailer.sent[-1].text)
    wrong = "000000" if code != "000000" else "111111"
    for left in (4, 3, 2, 1):
        r = await env.http.post("/v1/auth/verify", json={"contact": CONTACT, "code": wrong})
        assert (r.status_code, r.json()["triesLeft"]) == (401, left)
    await restart(env)
    r = await env.http.post("/v1/auth/verify", json={"contact": CONTACT, "code": wrong})
    assert r.status_code == 423, "the fifth wrong try locks the code, though the tries were on two servers"
    assert (await env.http.post("/v1/auth/verify", json={"contact": CONTACT, "code": code})).status_code == 423

    # The per-address counters are memory only. This is what a restart costs: whoever was
    # told to slow down may try again at once. The lock above is what guards a code.
    for _ in range(5):
        assert (await env.http.post("/v1/auth/request-code", json={"contact": CONTACT})).status_code == 200
    assert (await env.http.post("/v1/auth/request-code", json={"contact": CONTACT})).status_code == 429
    await restart(env)
    assert (await env.http.post("/v1/auth/request-code", json={"contact": CONTACT})).status_code == 200

    # A phone that was cut off stays cut off.
    assert (await env.http.post("/v1/auth/logout", headers=headers)).status_code == 200
    await restart(env)
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 401


async def test_the_office_stays_signed_in_and_a_form_opened_before_the_restart_still_works(env: Env) -> None:
    csrf = await office_login(env)
    await restart(env)
    assert (await env.http.get("/office")).status_code == 200
    r = await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff"})
    assert r.status_code == 303
    assert (await env.one(sa.select(db.office_users).where(db.office_users.c.email == STAFF))).role == "staff"

    await env.http.post("/office/logout", data={"csrf": csrf})
    await restart(env)
    assert (await env.http.get("/office")).status_code == 303, "signed out stays signed out"


async def test_what_a_customer_did_before_a_restart_still_holds_after_it(env: Env, clock: Clock) -> None:
    # Sam has work of his own at Jane's today (two techs at one property, as in the real
    # week), so his video of it is his to send and only her own choice stands in its way.
    env.fake.data["appointment"][9011] = {
        "appointmentID": "9011", "officeID": "7", "customerID": "501", "routeID": "71", "type": "80",
        "status": "0", "assignedTech": "21", "employeeID": "22", "date": today(), "start": "09:30:00",
        "sequence": "2", "checkIn": minutes_ago(3), "checkOut": "0000-00-00 00:00:00",
    }
    pat = await ready(env)
    sam = await login(env, "sam@example.com")
    env.mailer.sent.clear()
    first = await film(env, pat, 501, recordedAt=just_now(clock))
    second = await film(env, pat, 502, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await restart(env)
    assert len(env.mailer.sent) == 2
    jane, bob = (await env.video(first)).token, (await env.video(second)).token

    assert (await env.http.post(f"/u/{jane}")).status_code == 200  # Jane: no more video emails
    assert (await env.http.post(f"/w/{bob}/not-my-home")).status_code == 200  # Bob's link: not my home
    await restart(env)

    assert (await env.http.get(f"/w/{jane}")).status_code == 200, "her link still plays"
    assert "will not get video emails" in (await env.http.get(f"/u/{jane}")).text
    assert (await env.http.get(f"/w/{bob}")).status_code == 410, "the video taken down stays down"
    assert len(await env.flags("not_my_home")) == 1

    # Sam films Jane's property the same day. She asked for no more emails: none goes.
    later = await film(env, sam, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await restart(env)
    video = await env.video(later)
    assert (video.state, video.email_skip) == ("no_email", "opted_out") and len(env.mailer.sent) == 2


async def test_a_restart_costs_a_handful_of_fieldroutes_reads_and_no_customer_is_read_again(env: Env) -> None:
    headers = await ready(env)
    # As on a normal day: the tech has had the app since last week, so the daily check
    # (which runs at every start) also takes its one more look at yesterday.
    await env.run(sa.update(db.devices).values(created_at=db.now() - timedelta(days=7)))
    before = len(env.fake.requests)
    await restart(env)
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
    reads = [f"{r.entity}/{r.action}" for r in env.fake.requests[before:]]
    assert set(reads) == {"appointment/search", "employee/search", "route/search", "serviceType/search"}, (
        "the day's stops, the tech list, the routes (only on a day with an unassigned appointment) and "
        "the service names. Stops and names are in the database, so no customer is fetched again."
    )
    assert len(reads) <= 8, f"of the 3,000 a day the office shares: {reads}"


async def test_the_saved_route_is_served_after_a_restart_even_with_fieldroutes_down(env: Env) -> None:
    headers = await ready(env)
    before = (await env.http.get("/v1/today", headers=headers)).json()["stops"]
    env.fake.down = True
    await restart(env)
    r = await env.http.get("/v1/today", headers=headers)
    assert r.status_code == 200 and r.json()["stops"] == before and len(before) == 3


async def test_the_owners_settings_are_read_from_the_database_by_the_new_server(env: Env) -> None:
    headers = await ready(env)
    await set_setting(env.ctx, "hold_minutes", 7, "owner@zest.test")
    await set_setting(env.ctx, "missing_alert_minutes", 45, "owner@zest.test")
    await restart(env)
    assert (await env.http.get("/v1/today", headers=headers)).json()["config"]["holdMinutes"] == 7
    assert (await get_settings(env.ctx))["missing_alert_minutes"] == 45


async def test_ten_restarts_delete_nothing_that_should_be_kept(env: Env, clock: Clock) -> None:
    """The daily clean-up runs at every start, so ten deploys in a night run it ten times."""
    pat = await ready(env)
    sam = await login(env, "sam@example.com")
    env.mailer.sent.clear()
    held = await film(env, pat, 501, recordedAt=just_now(clock))
    sent = await film(env, pat, 502, recordedAt=stamp(clock.now() - timedelta(minutes=30)))
    pulled = await film(env, pat, 503, recordedAt=stamp(clock.now() - timedelta(minutes=30)))
    replaced = await film(env, sam, 504, recordedAt=just_now(clock))
    again = await film(env, sam, 504, recordedAt=just_now(clock))
    waiting = video_body(501, recordedAt=just_now(clock))
    assert (await env.http.post("/v1/videos", json=waiting, headers=sam)).status_code == 200
    clock.forward(minutes=2)
    await restart(env)  # the two older ones are past their ten minutes; Bob's has an email to go to
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"]
    await videos.pull(env.ctx, pulled, "owner@zest.test", "wrong take")
    await env.run(sa.update(db.videos).where(db.videos.c.id == pulled).values(ended_at=db.now() - timedelta(days=9)))

    for _ in range(10):
        await restart(env)

    rows = {name: await env.video(video_id) for name, video_id in
            (("held", held), ("sent", sent), ("pulled", pulled), ("replaced", replaced), ("again", again),
             ("waiting", waiting["id"]))}
    assert {name: row.state for name, row in rows.items()} == {
        "held": "held", "sent": "sent", "pulled": "pulled", "replaced": "replaced", "again": "held",
        "waiting": "awaiting_upload",
    }
    for name in ("held", "sent", "pulled", "again"):
        assert rows[name].object_key in env.storage.objects and not rows[name].object_deleted, name
    assert rows["replaced"].object_key not in env.storage.objects, "only the one the tech recorded over is gone"
    assert len(env.mailer.sent) == 1


# == a database backup put back ========================================================
#
# GO-LIVE.md tells the owner to switch on backups and to try a restore. A restore takes
# the database back to the moment of the backup and tells nobody: on Railway the video
# server is not even restarted. Whatever was true then is true again, and the server acts
# on it in its next minute.


async def take_backup(env: Env) -> dict[str, list[dict[str, Any]]]:
    """Every row of every table, as a backup holds them."""
    async with env.ctx.engine.connect() as conn:
        return {
            table.name: [dict(row._mapping) for row in (await conn.execute(sa.select(table))).all()]
            for table in db.metadata.sorted_tables
        }


async def restore_backup(env: Env, backup: dict[str, list[dict[str, Any]]]) -> None:
    """Put the backup back: rows made since are gone, rows changed since are as they were."""
    async with env.ctx.engine.begin() as conn:
        for table in reversed(db.metadata.sorted_tables):
            await conn.execute(sa.delete(table))
        for table in db.metadata.sorted_tables:
            if backup[table.name]:
                await conn.execute(sa.insert(table), backup[table.name])


async def test_a_restore_done_the_safe_way_emails_nobody_twice_and_writes_nothing_twice(env: Env, clock: Clock) -> None:
    """The steps GO-LIVE.md gives for a restore: customer email off and FieldRoutes writes
    off first, then the restore, then a look at the office page, then both back on."""
    headers = await ready(env)
    emailed = await film(env, headers, 501, recordedAt=just_now(clock), recommend=True)  # Jane
    pulled = await film(env, headers, 502, recordedAt=just_now(clock))  # Bob: the office will pull this one
    backup = await take_backup(env)  # the backup falls inside both videos' ten minutes

    await videos.pull(env.ctx, pulled, "owner@zest.test", "wrong house")
    clock.forward(minutes=11)
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "Jane's went; Bob's was pulled in time"
    assert len(fr_writes(env, "note")) == 1 and len(fr_writes(env, "task")) == 1
    token = (await env.video(emailed)).token
    assert (await env.http.post(f"/u/{token}")).status_code == 200  # Jane: no more video emails
    env.mailer.sent.clear()

    # Two days later. Step 1: both switches off. On Railway that restarts the server.
    clock.forward(days=2)
    env.reconfigure(email_mode="off", fr_writes=False)
    await restart(env)
    # Step 2: the restore. Nothing restarts the video server; its jobs just carry on.
    await restore_backup(env, backup)
    await one_pass(env)
    await restart(env)
    assert env.mailer.sent == [], "nobody is emailed a second time, and Bob is not emailed at all"
    assert len(fr_writes(env, "note")) == 1 and len(fr_writes(env, "task")) == 1, "nothing is written twice"

    # Step 3, what the office finds and has to put right by hand. These are what a restore
    # undoes; GO-LIVE.md says so in these words.
    jane, bob = await env.video(emailed), await env.video(pulled)
    assert (jane.state, jane.email_skip) == ("no_email", "email_off"), "reads: customer emails are off"
    assert jane.token == token and (await env.http.get(f"/w/{token}")).status_code == 200, (
        "the link in the email she already has still plays"
    )
    assert (bob.state, bob.email_skip) == ("no_email", "email_off"), "the pulled video is back: pull it again"
    assert not await videos.has_opted_out(env.ctx, jane), "her 'no more emails' is forgotten: note it before a restore"
    await videos.pull(env.ctx, pulled, "owner@zest.test", "wrong house")

    # Step 4: both switches back on. Nothing old goes out.
    env.reconfigure(email_mode="live", fr_writes=True)
    await restart(env)
    clock.forward(minutes=30)
    await restart(env)
    assert env.mailer.sent == [], "the old emails stay unsent once customer email is back on"
    assert len(fr_writes(env, "note")) == 1 and len(fr_writes(env, "task")) == 1
    assert (await env.video(pulled)).state == "pulled"


async def test_a_restore_with_the_switches_left_on_emails_no_customer_a_second_time(
    env: Env, clock: Clock
) -> None:
    """The same restore without step 1. The video was inside its ten minutes when the backup
    was taken, and was emailed afterwards. Put back, it is "waiting" again, two days
    overdue. It used to go out in the server's next minute: a second email, a second note
    and a second task (the mail service only remembers a key for 24 hours). Now an email
    that is hours late does not go by itself: it waits for the office. (This was a known
    gap; closed in videos.py, behind the owner's setting `overdue_hours`.)"""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock), recommend=True)
    backup = await take_backup(env)
    clock.forward(minutes=11)
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"]

    clock.forward(days=2)
    await restore_backup(env, backup)
    await one_pass(env)  # the running server's next minute; nothing restarted it
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "one email, not two"
    assert len(fr_writes(env, "note")) == 1 and len(fr_writes(env, "task")) == 1

    # What the office finds: the video kept, waiting for them, and one flag that says why.
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "overdue")
    (flag,) = await env.flags("overdue")
    assert flag.video_id == video_id and "Its email was 2 days late, so it was not sent by itself" in flag.detail
    # The flag used to end "Nobody was emailed." Here that was false: she was emailed two
    # days ago, and the record of it went with the restore. It says what the server knows.
    assert "Nobody was emailed" not in flag.detail
    assert flag.detail.endswith(LATE_FLAG_ENDS)
    assert (await env.http.get(f"/w/{video.token}")).status_code == 200, "the link in the email she has still plays"
    # And it stays that way: no later round, restart or day lets it go by itself.
    for _ in range(3):
        clock.forward(hours=5)
        await restart(env)
    assert len(env.mailer.sent) == 1 and len(fr_writes(env, "note")) == 1
    assert len(await env.flags("overdue", open_only=False)) == 1, "and the office is told once, not every round"


async def test_a_restore_with_the_switches_left_on_does_not_send_what_the_office_pulled(
    env: Env, clock: Clock
) -> None:
    headers = await ready(env)
    pulled = await film(env, headers, 502, recordedAt=just_now(clock))
    backup = await take_backup(env)
    await videos.pull(env.ctx, pulled, "owner@zest.test", "wrong house")
    clock.forward(minutes=11)
    await one_pass(env)
    assert env.mailer.sent == []

    clock.forward(days=2)
    await restore_backup(env, backup)
    await one_pass(env)
    assert env.mailer.sent == [], "a video the office pulled as the wrong house is not sent by a restore"
    assert fr_writes(env, "note") == [], "and its link is not put on that customer's account"

    # It is back, as the restore left it, and it waits. The office pulls it again.
    assert ((await env.video(pulled)).state, (await env.video(pulled)).email_skip) == ("no_email", "overdue")
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{pulled}/pull", data={"csrf": csrf, "reason": "wrong house"})
    assert r.status_code == 303 and (await env.video(pulled)).state == "pulled"
    await one_pass(env)
    assert await env.flags("overdue") == [], "its flag goes with it"
    assert [m.to for m in env.mailer.sent if m.tag == "video"] == [], "still no customer email"


# == two servers side by side, as at every deploy =======================================
#
# Railway starts the new container before it stops the old one, so for a moment two
# servers run the same job loops over the same database. Whatever they both reach for in
# that moment must still happen once: one email, one push, one flag, one FieldRoutes write.


@contextlib.asynccontextmanager
async def second_server(env: Env):
    """The new server at a deploy: its own context over the same database and bucket. The
    mail, text and push recorders and the fake FieldRoutes are the same outside world."""
    new = replace(env)
    new.ctx = build_ctx(
        replace(env.ctx.cfg), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
        texter=env.texter, pusher=env.pusher,
    )
    try:
        await video_app.start_up(new.ctx)
        yield new
    finally:
        await new.ctx.engine.dispose()


def outside_calls_take_a_moment(*servers: Env) -> None:
    """A real call to FieldRoutes, the mail service or Apple takes a moment, and that
    moment is when the other server gets its turn. Without this the fakes answer at once
    and the two loops would hardly ever meet."""
    def slowed(call: Any) -> Any:
        async def slow(*args: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.01)
            return await call(*args, **kwargs)

        return slow

    for server in servers:
        server.ctx.fr.client.call = slowed(server.ctx.fr.client.call)  # type: ignore[method-assign]
    shared = servers[0]
    shared.mailer.send = slowed(shared.mailer.send)  # type: ignore[method-assign]
    shared.pusher.send = slowed(shared.pusher.send)  # type: ignore[method-assign]


async def one_of_everything_due(env: Env, clock: Clock) -> tuple[str, str]:
    """One of everything the job loops do, all due in the same minute."""
    pat = await ready(env)
    sam = await login(env, "sam@example.com")
    await env.http.post("/v1/devices/push", json={"apnsToken": "abcd12", "sandbox": False}, headers=pat)

    # Jane: Pat's video has been stuck on his phone for four hours (a push, then a flag).
    stuck = video_body(501, recordedAt=just_now(clock))
    assert (await env.http.post("/v1/videos", json=stuck, headers=pat)).status_code == 200
    clock.forward(hours=4)
    # Bob: a video whose email is due, and the tech recommended something (email, note, task, flag).
    bobs = await film(env, pat, 502, recordedAt=just_now(clock), recommend=True)
    # Ann: Sam's video is due too, and the office has just cancelled her visit (held for the office, flag).
    anns = await film(env, sam, 504, recordedAt=just_now(clock))
    env.fake.data["appointment"][9006]["status"] = "-1"
    # Acme: finished in FieldRoutes two hours ago, and nobody filmed it (flag).
    env.fake.data["appointment"][9005].update(status="1", checkIn=minutes_ago(150), checkOut=minutes_ago(120))
    await sync_day(env.ctx, today(), force=True)
    await env.finished(503, ago=timedelta(hours=2), force=False)
    # And a customer's score that FieldRoutes has not been given yet (review).
    await videos.enqueue_fr(env.ctx, "review", "review:made-up", 501, None,
                            {"appointment_id": 9001, "stars": 5, "feedback": "Thank you", "at": clock.now().isoformat()})
    clock.forward(minutes=11)
    env.mailer.sent.clear()
    return bobs, anns


async def each_of_them_happened_once(env: Env, bobs: str, anns: str) -> None:
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"], "one email, and none for the cancelled visit"
    assert len(env.pusher.sent) == 1, "one nudge to the phone with the stuck video"
    assert (await env.video(bobs)).state == "sent"
    assert ((await env.video(anns)).state, (await env.video(anns)).email_skip) == ("no_email", "off_route")
    kinds = sorted(f.kind for f in await env.flags(open_only=False))
    assert kinds == ["no_video", "off_route", "recommend", "stuck_upload"], "each flag once"
    assert [(f.customer_id, f.tech_id) for f in await env.flags("no_video")] == [(503, 20)]
    assert (len(fr_writes(env, "note")), len(fr_writes(env, "task")), len(fr_writes(env, "review"))) == (1, 1, 1), (
        "each FieldRoutes write once"
    )
    rows = await env.all(sa.select(db.fr_outbox))
    assert len(rows) == 3 and all(r.done_at is not None and r.attempts == 0 for r in rows)
    actions = await env.actions()
    assert actions.count("email_sent") == 1 and actions.count("email_skipped") == 1


async def test_two_servers_side_by_side_do_every_job_once(env: Env, clock: Clock) -> None:
    """The two job loops as they really run: each going down its own list of jobs."""
    bobs, anns = await one_of_everything_due(env, clock)
    async with second_server(env) as new:
        outside_calls_take_a_moment(env, new)
        for first, second in ((env, new), (new, env), (env, new)):
            env.ctx.last_sync.clear(), new.ctx.last_sync.clear()  # both read the day again, too
            await one_pass(first, second)
            clock.forward(minutes=6)
    await each_of_them_happened_once(env, bobs, anns)


async def test_two_servers_starting_the_same_job_in_the_same_instant_do_it_once(env: Env, clock: Clock) -> None:
    """The worst timing there is: both servers start each job at the very same moment, so
    both have read the same rows before either has written anything."""
    bobs, anns = await one_of_everything_due(env, clock)
    async with second_server(env) as new:
        outside_calls_take_a_moment(env, new)
        for _ in range(3):
            for _name, _every, job in jobs.JOBS:
                env.ctx.last_sync.clear(), new.ctx.last_sync.clear()
                results = await asyncio.gather(job(env.ctx), job(new.ctx), return_exceptions=True)
                assert [r for r in results if isinstance(r, BaseException)] == [], _name
            clock.forward(minutes=6)

        await each_of_them_happened_once(env, bobs, anns)

        # And the step that sends the push, with both servers holding the very same row:
        # each read it before the other had marked it.
        await env.run(sa.update(db.videos).where(db.videos.c.state == "awaiting_upload").values(stuck_nudged_at=None))
        row = await env.one(sa.select(db.videos).where(db.videos.c.state == "awaiting_upload"))
        await asyncio.gather(jobs._nudge(env.ctx, row), jobs._nudge(new.ctx, row))
        assert len(env.pusher.sent) == 2, "one more push, not two more"


async def test_two_servers_meeting_a_new_day_a_new_hire_and_a_new_owner_in_the_same_second(env: Env) -> None:
    """Reading a day nobody has read yet saves its stops, and both servers may do that in
    the same second. The one that comes second used to fail the whole read, and a phone
    asking for its day at that moment got an error."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).astimezone(env.ctx.cfg.tz).date().isoformat()
    for row in env.fake.data["appointment"].values():
        row["date"] = tomorrow if row["date"] == today() else row["date"]
    for row in env.fake.data["route"].values():
        row["date"] = tomorrow
    await env.run(sa.delete(db.techs))
    await env.run(sa.delete(db.office_users))
    async with second_server(env) as new:
        await env.run(sa.delete(db.office_users))
        outside_calls_take_a_moment(env, new)
        results = await asyncio.gather(
            sync_day(env.ctx, tomorrow, force=True), sync_day(new.ctx, tomorrow, force=True),
            auth.sync_techs(env.ctx), auth.sync_techs(new.ctx),
            auth.ensure_owners(env.ctx), auth.ensure_owners(new.ctx),
            return_exceptions=True,
        )
    assert [r for r in results if isinstance(r, BaseException)] == [], "neither server tripped over the other"
    stops = await env.all(sa.select(db.stops).where(db.stops.c.day == tomorrow))
    assert sorted(s.customer_id for s in stops) == [501, 502, 503, 504] and all(s.name != "Customer" for s in stops)
    assert sorted(t.id for t in await env.all(sa.select(db.techs))) == [20, 21]
    assert [u.email for u in await env.all(sa.select(db.office_users))] == ["owner@zest.test"]


async def test_two_servers_side_by_side_write_every_waiting_fieldroutes_row_once(env: Env, clock: Clock) -> None:
    """Ten rows waiting, of all three kinds, and both servers run the outbox three times over."""
    headers = await ready(env)
    for n in range(4):
        await videos.enqueue_fr(env.ctx, "note", f"note:made-up-{n}", 501, None, {"text": f"link {n}", "day": today()})
        await videos.enqueue_fr(env.ctx, "task", f"task:made-up-{n}", 502, None, {"text": f"follow up {n}", "due": today()})
    for n, appointment in enumerate((9001, 9004)):
        await videos.enqueue_fr(env.ctx, "review", f"review:made-up-{n}", 501, None,
                                {"appointment_id": appointment, "stars": 5, "feedback": "", "at": clock.now().isoformat()})
    assert headers
    async with second_server(env) as new:
        outside_calls_take_a_moment(env, new)
        for _ in range(3):
            await asyncio.gather(videos.run_outbox(env.ctx), videos.run_outbox(new.ctx))
    assert (len(fr_writes(env, "note")), len(fr_writes(env, "task")), len(fr_writes(env, "review"))) == (4, 4, 2)
    assert sorted(w.one("notes") for w in fr_writes(env, "note")) == [f"link {n}" for n in range(4)], "each one, once"
    rows = await env.all(sa.select(db.fr_outbox))
    assert len(rows) == 10 and all(r.done_at is not None and r.last_error == "" for r in rows)
    assert await env.flags("fr_write_failed") == []


async def test_a_fieldroutes_write_cut_off_in_the_middle_is_never_made_a_second_time(env: Env, clock: Clock) -> None:
    """The server is stopped while a note is on its way to FieldRoutes. Whether FieldRoutes
    took it, nobody can say. Trying again could write it twice, so it is not tried again:
    the office is told to look."""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock), recommend=True)
    clock.forward(minutes=11)
    assert await videos.send_due(env.ctx) == 1

    took_it = env.ctx.fr.create_note

    async def taken_then_killed(*args: Any, **kwargs: Any) -> None:
        await took_it(*args, **kwargs)  # FieldRoutes has the note
        raise Killed  # and the server never hears so

    async def killed_on_the_way(*args: Any, **kwargs: Any) -> None:
        raise Killed  # FieldRoutes never got the task

    env.ctx.fr.create_note = taken_then_killed  # type: ignore[method-assign]
    with pytest.raises(Killed):
        await videos.run_outbox(env.ctx)
    await restart(env, jobs_too=False)
    env.ctx.fr.create_task = killed_on_the_way  # type: ignore[method-assign]
    with pytest.raises(Killed):
        await videos.run_outbox(env.ctx)
    assert len(fr_writes(env, "note")) == 1 and fr_writes(env, "task") == []

    for _ in range(4):  # the new server, for the next half hour and more
        clock.forward(minutes=9)
        await restart(env)
    assert len(fr_writes(env, "note")) == 1, "the note is not written a second time"
    assert fr_writes(env, "task") == [], "and the task is not tried again either: the server cannot tell the two apart"
    flags = {f.detail for f in await env.flags("fr_write_failed")}
    assert len(flags) == 2 and all("may or may not be there" in d and "add it by hand" in d for d in flags)
    token = (await env.video(video_id)).token
    assert all(f"{BASE}/w/{token}" in d for d in flags), "each flag carries what to add by hand"
    rows = await env.all(sa.select(db.fr_outbox))
    assert all(r.failed_at is not None and r.done_at is None for r in rows)
    clock.forward(days=2)
    await restart(env)
    assert len(fr_writes(env, "note")) == 1 and len(await env.flags("fr_write_failed", open_only=False)) == 2


# -- a slow pass: FieldRoutes is timing out, and a deploy starts a second server --------
#
# One write that gets no answer takes about a minute and a half to give up, and a pass
# goes through up to 25 rows. So a pass can be many minutes old when it reaches a row. A
# claim (and the emailer's lease) must run from the moment it is made, not from the start
# of the pass, or it is already over when it is made and the other server takes the row.


async def notes_waiting(env: Env, how_many: int) -> None:
    for n in range(1, how_many + 1):
        await videos.enqueue_fr(env.ctx, "note", f"note:made-up-{n}", 501, None, {"text": f"link {n}", "day": today()})


async def test_in_a_slow_pass_the_new_server_does_not_write_the_row_the_old_one_is_writing(env: Env, clock: Clock) -> None:
    """Eight notes wait. The old server gets no answer for the first seven, a minute and a
    half each, so its pass is eleven minutes old when it claims the eighth. The new server
    read its own list of waiting rows just before that. One note must be written once."""
    await ready(env)
    await notes_waiting(env, 8)
    async with second_server(env) as new:
        old_writes, new_writes = env.ctx.fr.create_note, new.ctx.fr.create_note
        old_is_on_the_last, new_has_its_list = asyncio.Event(), asyncio.Event()
        new_pass: list[asyncio.Task] = []

        async def old_servers_fieldroutes(customer_id: int, text: str, **kw: Any) -> None:
            if text != "link 8":
                clock.forward(seconds=95)
                if text == "link 7":
                    new_pass.append(asyncio.create_task(videos.run_outbox(new.ctx)))
                    await new_has_its_list.wait()
                raise FieldRoutesError("no answer in time")
            old_is_on_the_last.set()  # claimed a moment ago, and on its way to FieldRoutes
            await asyncio.gather(*new_pass)  # the new server goes through its whole list meanwhile
            await old_writes(customer_id, text, **kw)

        async def new_servers_fieldroutes(customer_id: int, text: str, **kw: Any) -> None:
            new_has_its_list.set()
            await old_is_on_the_last.wait()
            await new_writes(customer_id, text, **kw)

        env.ctx.fr.create_note = old_servers_fieldroutes  # type: ignore[method-assign]
        new.ctx.fr.create_note = new_servers_fieldroutes  # type: ignore[method-assign]
        await videos.run_outbox(env.ctx)
    notes = [w.one("notes") for w in fr_writes(env, "note")]
    assert notes.count("link 8") == 1, f"FieldRoutes got {sorted(notes)}"
    assert len(notes) == len(set(notes)), "and nothing else twice either"
    assert await env.flags("fr_write_failed") == []


async def test_in_a_slow_pass_a_row_that_is_being_written_is_not_taken_for_one_that_was_cut_off(env: Env, clock: Clock) -> None:
    """The other half. A row found marked "being written" after its claim ran out belongs
    to a server that was stopped in the middle, and is given up with a flag. A claim that
    was stamped with the start of a slow pass looked run out while the old server was
    still on the row: the new server gave the note up after one try and told the office
    "the server was stopped while it was saving this note". Nobody had been stopped."""
    await ready(env)
    await notes_waiting(env, 7)
    async with second_server(env) as new:
        old_writes = env.ctx.fr.create_note

        async def old_servers_fieldroutes(customer_id: int, text: str, **kw: Any) -> None:
            clock.forward(seconds=95)
            if text != "link 7":
                raise FieldRoutesError("no answer in time")
            await videos.run_outbox(new.ctx)  # the new server's whole pass, while this write is on its way
            await old_writes(customer_id, text, **kw)  # and this time FieldRoutes answers

        env.ctx.fr.create_note = old_servers_fieldroutes  # type: ignore[method-assign]
        await videos.run_outbox(env.ctx)
    assert await env.flags("fr_write_failed") == [], "nothing was cut off, so the office is told nothing"
    row = await env.one(sa.select(db.fr_outbox).where(db.fr_outbox.c.dedupe == "note:made-up-7"))
    assert row.done_at is not None and row.failed_at is None and row.last_error == ""
    notes = [w.one("notes") for w in fr_writes(env, "note")]
    assert notes.count("link 7") == 1 and len(notes) == len(set(notes))
    # The notes that got no answer are still waiting for their next try, not given up.
    waiting = await env.all(sa.select(db.fr_outbox).where(db.fr_outbox.c.done_at.is_(None)))
    assert waiting and all(r.failed_at is None and r.attempts == 1 for r in waiting)


async def test_in_a_slow_pass_the_new_server_does_not_email_the_video_the_old_one_is_sending(env: Env, clock: Clock) -> None:
    """The emailer's five-minute lease has the same shape. The pass is six minutes old when
    it reaches Bob's video (in life: a busy afternoon, and FieldRoutes slow to give each
    address). A lease stamped with the start of the pass is over as it is taken, and the
    new server hands the mail service the same email at the same moment."""
    headers = await ready(env)
    await film(env, headers, 501, recordedAt=stamp(clock.now() - timedelta(minutes=2)))
    bobs = await film(env, headers, 502, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    async with second_server(env) as new:
        deliver, handed = env.mailer.send, []

        async def slow(email: Any) -> str:
            handed.append(email.to)
            if len(handed) == 1:
                clock.forward(minutes=6)
            elif len(handed) == 2:
                await videos.send_due(new.ctx)  # the new server's pass, while Bob's email is on its way
            return await deliver(email)

        env.mailer.send = slow  # type: ignore[method-assign]
        await videos.send_due(env.ctx)
    assert handed == ["jane@example.com", "bob@example.com"], "each email was handed over once"
    assert (await env.video(bobs)).state == "sent" and (await env.actions()).count("email_sent") == 2


# == an email that is long overdue =======================================================
#
# After the server (or FieldRoutes) was down for hours, every email whose time came in
# between used to go out the moment things worked again, whatever the hour. After a
# restore the same rule sent second emails. Now a video whose email is more than a few
# hours late (the owner's setting, 6 unless changed; 0 switches it off) waits for the
# office: kept, playable, nobody emailed, nothing written to FieldRoutes, one flag.


# How the flag of a late video ends while customer emails are on. After a backup was put
# back the server cannot know that a video was already emailed, so it never says "nobody
# was emailed": it says what it knows, and where the office can look.
LATE_FLAG_ENDS = (
    "This server has no record of an email for it. If a backup was put back, the customer may already have it: "
    "look for the video note on the customer's account in FieldRoutes before you send. Press Send it anyway, or Pull."
)


async def test_after_hours_of_being_down_late_emails_wait_for_the_office(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    janes = await film(env, headers, 501, recordedAt=just_now(clock), recommend=True)
    bobs = await film(env, headers, 502, recordedAt=just_now(clock))
    acmes = await film(env, headers, 503, recordedAt=just_now(clock))  # no email on file

    clock.forward(hours=7)  # the server was down all that time; it comes back at night
    await restart(env)
    await restart(env)
    assert env.mailer.sent == [], "nobody is emailed at whatever hour the server came back"
    for video_id in (janes, bobs, acmes):
        video = await env.video(video_id)
        assert (video.state, video.email_skip) == ("no_email", "overdue")
        assert video.object_key in env.storage.objects and not video.object_deleted, "the video is kept"
    assert await env.all(sa.select(db.fr_outbox)) == [], "and nothing is written to FieldRoutes until the office decides"
    flags = {f.video_id: f for f in await env.flags("overdue")}
    assert set(flags) == {janes, bobs, acmes} and len(await env.flags(open_only=False)) == 3, "one flag each, no other"
    assert flags[janes].detail == (
        "Pat's video for Jane Smith, 123 Main St. Its email was 7 hours late, so it was not sent by itself. That "
        "happens when the server, FieldRoutes or the storage was down, or when a backup was put back. " + LATE_FLAG_ENDS
    )

    # The tech's phone treats it like any other video the office is holding.
    today = (await env.http.get("/v1/today", headers=headers)).json()
    assert {v["id"]: (v["state"], v.get("withOffice")) for v in today["videos"]} == {
        janes: ("no_email", True), bobs: ("no_email", True), acmes: ("no_email", True),
    }
    assert today["stops"][0]["video"] == {"id": janes, "state": "no_email", "byYou": True, "withOffice": True}
    r = await env.http.delete(f"/v1/videos/{janes}", headers=headers)
    assert r.status_code == 409 and r.json()["message"] == "The office is looking at this video. Call the office to change it."

    # The office sees why, and has both buttons.
    csrf = await office_login(env)
    page = (await env.http.get("/office")).text
    assert "Video held: its email was long overdue" in page
    assert "No email: its email was hours late, so it waits for the office" in page
    assert page.count(">Send it anyway</button>") == 3

    r = await env.http.post(f"/office/video/{janes}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    r = await env.http.post(f"/office/video/{bobs}/pull", data={"csrf": csrf, "reason": "too late to be of use"})
    assert r.status_code == 303
    env.mailer.sent.clear()  # the office's own sign-in code
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "the one the office sent goes, once"
    assert (await env.video(janes)).state == "sent" and (await env.video(bobs)).state == "pulled"
    assert len(fr_writes(env, "note")) == 1 and len(fr_writes(env, "task")) == 1, "and its link and follow-up are filed now"
    assert [f.video_id for f in await env.flags("overdue")] == [acmes], "each flag went with its decision"

    # The third stays as it is for as long as nobody decides. No round or restart sends it.
    for _ in range(3):
        clock.forward(hours=9)
        await restart(env)
    assert len(env.mailer.sent) == 1 and (await env.video(acmes)).email_skip == "overdue"
    assert len(await env.flags("overdue", open_only=False)) == 3, "and it is not flagged again every round"


async def test_an_email_a_few_hours_late_still_goes_by_itself(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(hours=5, minutes=50)
    await restart(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"] and (await env.video(video_id)).state == "sent"
    assert await env.flags("overdue") == []


async def test_the_owner_sets_how_late_is_too_late_and_zero_switches_the_rule_off(env: Env, clock: Clock) -> None:
    assert (await get_settings(env.ctx))["overdue_hours"] == 6, "the default"
    csrf = await office_login(env)
    page = (await env.http.get("/office/settings")).text
    assert 'name="overdue_hours" value="6"' in page and "more than this many hours late is not sent by itself" in page
    # The box says how low and how high it goes, so 169 or -1 is caught before Save and not after.
    assert 'name="overdue_hours" value="6" min="0" max="168"' in page and "From 0 to 168." in page
    assert 'name="hold_minutes" value="10" min="0" max="120"' in page, "every number box with a limit shows it"
    headers = await ready(env)

    await set_setting(env.ctx, "overdue_hours", "2", "owner@zest.test")
    held = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(hours=3)
    await restart(env)
    assert env.mailer.sent == [] and (await env.video(held)).email_skip == "overdue"
    (flag,) = await env.flags("overdue")
    assert "3 hours late" in flag.detail

    r = await env.http.post("/office/settings", data={"csrf": csrf, "overdue_hours": "0"})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    assert (await get_settings(env.ctx))["overdue_hours"] == 0
    late = await film(env, headers, 502, recordedAt=just_now(clock))
    clock.forward(days=3)
    await restart(env)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"], "switched off: a late email goes as it always did"
    assert (await env.video(late)).state == "sent"
    assert (await env.video(held)).email_skip == "overdue", "what was already waiting for the office still waits"

    for bad in ("-1", "200", "soon"):
        r = await env.http.post("/office/settings", data={"csrf": csrf, "overdue_hours": bad})
        assert "err=" in r.headers["location"], bad


async def test_fieldroutes_down_for_hours_does_the_same_as_the_server_being_down(env: Env, clock: Clock) -> None:
    """The server was up all day and FieldRoutes was not, so no address could be read and
    the email was tried every five minutes. When FieldRoutes is back at night, the email
    is hours late all the same."""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    env.fake.down = True
    for _ in range(6):
        clock.forward(minutes=55)
        await one_pass(env)
    assert env.mailer.sent == [] and (await env.video(video_id)).state == "held", "5 hours 30: still only waiting"
    env.fake.down = False
    clock.forward(minutes=55)
    await one_pass(env)
    assert env.mailer.sent == [], "FieldRoutes is back, and the email is 6 hours 15 late: it does not go by itself"
    assert (await env.video(video_id)).email_skip == "overdue" and len(await env.flags("overdue")) == 1
    for _ in range(2):
        clock.forward(minutes=6)
        await one_pass(env)
    assert env.mailer.sent == [] and len(await env.flags("overdue", open_only=False)) == 1


async def test_a_video_the_office_let_go_is_not_held_for_being_late_but_is_held_again_after_a_restore(
    env: Env, clock: Clock
) -> None:
    """Send it anyway is the office saying "now". The wait is counted from there, so the
    video goes. But the same say-so found in a backup that is put back days later is old
    news: it does not send the video a second time."""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(hours=8)
    await one_pass(env)
    assert (await env.video(video_id)).email_skip == "overdue"
    await videos.send_anyway(env.ctx, video_id, "owner@zest.test")
    backup = await take_backup(env)  # taken in the minute between the button and the email
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"]

    clock.forward(days=2)
    await restore_backup(env, backup)
    await one_pass(env)
    assert len(env.mailer.sent) == 1, "not a second email"
    assert (await env.video(video_id)).email_skip == "overdue"
    (flag,) = await env.flags("overdue")
    assert "2 days late" in flag.detail, "a new flag for the new wait; the one the office answered was put away"


async def test_a_video_the_office_matches_the_next_morning_is_not_held_for_being_late(env: Env, clock: Clock) -> None:
    """Filmed in the afternoon as Not On My List. The office picks its customer the next
    morning. That is the office saying "this one, now": it goes, however old the video is.
    The same for a video the office moves to the right customer a day later."""
    headers = await ready(env)
    loose = await film(env, headers, None, recordedAt=just_now(clock))
    wrong = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"]

    clock.forward(hours=16)
    csrf = await office_login(env)
    env.mailer.sent.clear()
    r = await env.http.post(f"/office/video/{loose}/move", data={"csrf": csrf, "customer_id": "502"})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"] and (await env.video(loose)).state == "sent"

    # Jane's video turns out to be Cy's house: moved a day late, it goes to Cy at once.
    clock.forward(hours=10)
    r = await env.http.post(f"/office/video/{wrong}/move", data={"csrf": csrf, "customer_id_other": "505"})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com", "cy@example.com"]
    assert await env.flags("overdue", open_only=False) == []


async def test_clearing_the_flag_of_a_late_video_sends_nothing(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(hours=7)
    await one_pass(env)
    (flag,) = await env.flags("overdue")
    csrf = await office_login(env)
    env.mailer.sent.clear()
    r = await env.http.post(f"/office/flag/{flag.id}/clear", data={"csrf": csrf, "note": "she already has it"})
    assert r.status_code == 303
    for _ in range(3):
        clock.forward(hours=2)
        await restart(env)
    video = await env.video(video_id)
    assert env.mailer.sent == [] and (video.state, video.email_skip) == ("no_email", "overdue")
    assert await env.flags() == [], "cleared means seen: it is not raised again"
    assert (await env.http.get(f"/w/{video.token}")).status_code == 200, "and the link still plays"
    page = (await env.http.get(f"/office?day={video.day}")).text
    assert f'action="/office/video/{video_id}/send"' in page, "the office can still send it or pull it from the day page"


async def test_with_customer_emails_off_a_late_video_still_waits_before_anything_is_written(
    env: Env, clock: Clock
) -> None:
    """Customer emails off, FieldRoutes writes on: nobody can be emailed late, but a link
    could still be written to an account a second time after a restore. So it waits."""
    env.reconfigure(email_mode="off")
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(hours=7)
    await restart(env)
    assert (await env.video(video_id)).email_skip == "overdue" and await env.all(sa.select(db.fr_outbox)) == []
    (flag,) = await env.flags("overdue")
    assert flag.detail.endswith(
        "This server has no record of an email or a saved link for it. If a backup was put back, the link may "
        "already be on the customer's account in FieldRoutes: look before you keep it. Press Keep it, or Pull."
    )
    csrf = await office_login(env)
    assert ">Keep it</button>" in (await env.http.get("/office")).text
    await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    env.mailer.sent.clear()
    await one_pass(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "email_off") and env.mailer.sent == []
    assert len(fr_writes(env, "note")) == 1, "kept: the link is on the account, once"


# == late, and the route changed too =====================================================
#
# The likely pair: the server or FieldRoutes is down for hours, and while it is down the
# office cancels a visit that was already filmed. The video is late and its visit is off
# the day. The office has to be told both before it presses anything, and "send it, late
# as it is" must not be taken for "send it, cancelled as it is".

BOBS_VISIT = 9004


def cancel_bobs_visit(env: Env) -> None:
    env.fake.data["appointment"][BOBS_VISIT]["status"] = "-1"


def put_bobs_visit_back(env: Env) -> None:
    env.fake.data["appointment"][BOBS_VISIT] = seed()["appointment"][BOBS_VISIT]


async def next_round(env: Env) -> None:
    """The server's next pass, with the saved list old enough to be read again."""
    env.ctx.last_sync.clear()
    await one_pass(env)


async def flags_about(env: Env, video_id: str, *, open_only: bool = True) -> list[Any]:
    """The flags about one video. (A long day also raises ones about other stops.)"""
    return [f for f in await env.flags(open_only=open_only) if f.video_id == video_id]


async def test_a_late_video_for_a_visit_cancelled_meanwhile_tells_the_office_both_things(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 502, recordedAt=just_now(clock))  # Bob, inside his ten minutes
    cancel_bobs_visit(env)  # while the server is down
    clock.forward(hours=7)
    await restart(env)  # it comes back, and its first pass reads the day again

    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "overdue") and env.mailer.sent == []
    (flag,) = await flags_about(env, video_id)
    assert flag.kind == "overdue"
    assert "Its email was 7 hours late" in flag.detail
    assert "FieldRoutes no longer has that visit on this day (cancelled, a no-show, or moved to another day)." in flag.detail, (
        "the office is told the visit left the day, on the same flag, before it presses anything"
    )
    assert flag.detail.endswith(LATE_FLAG_ENDS)
    # Told once for each thing: the flag that only said "late" was put away, not left open beside it.
    assert [f.cleared_by for f in await flags_about(env, video_id, open_only=False) if f.id != flag.id] == ["system"]

    # More rounds change nothing, and the visit coming back does not send it either:
    # a late video only ever goes on the office's word.
    await next_round(env)
    put_bobs_visit_back(env)
    await next_round(env)
    assert env.mailer.sent == [] and (await env.video(video_id)).email_skip == "overdue"
    (flag,) = await flags_about(env, video_id)
    assert "no longer has that visit" not in flag.detail, "and its flag says what is true now"

    cancel_bobs_visit(env)
    await next_round(env)
    (flag,) = await flags_about(env, video_id)
    assert "FieldRoutes no longer has that visit on this day" in flag.detail
    # The office knows both things and says send. One press is enough.
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    env.mailer.sent.clear()  # the office's own sign-in code
    await next_round(env)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"] and (await env.video(video_id)).state == "sent"
    await next_round(env)
    assert await flags_about(env, video_id) == [], "and nothing is raised about the route afterwards: the office decided knowing it"


async def test_a_late_video_whose_visit_is_already_known_to_be_off_the_day_says_so_at_once(env: Env, clock: Clock) -> None:
    """The cancellation was read before the server went down. When the late email is
    held, the saved list already knows, so the very first flag says both things: there is
    no moment in which the office sees "late" alone and could answer only that."""
    headers = await ready(env)
    video_id = await film(env, headers, 502, recordedAt=just_now(clock))
    cancel_bobs_visit(env)
    await next_round(env)  # still inside the ten minutes: the day is read, nothing is decided yet
    assert (await env.video(video_id)).state == "held" and await flags_about(env, video_id) == []
    clock.forward(hours=7)  # down for seven hours
    await videos.send_due(env.ctx)  # the first thing the server does when it is back

    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "overdue") and env.mailer.sent == []
    (flag,) = await flags_about(env, video_id, open_only=False)
    assert flag.kind == "overdue" and flag.cleared_at is None
    assert "Its email was 7 hours late" in flag.detail and "FieldRoutes no longer has that visit on this day" in flag.detail
    await next_round(env)
    assert [f.id for f in await flags_about(env, video_id, open_only=False)] == [flag.id], "and the rounds add nothing to it"


async def test_send_it_anyway_on_a_late_video_is_no_word_on_the_route(env: Env, clock: Clock) -> None:
    """The flag in front of the office says only that the email was late. The visit is
    cancelled, and nobody has been told yet (the day has not been read again). Send it
    anyway on that flag answers the lateness, nothing else: the route is still checked
    before the email goes, and the customer of a cancelled visit is not emailed."""
    headers = await ready(env)
    video_id = await film(env, headers, 502, recordedAt=just_now(clock))
    clock.forward(hours=7)
    await restart(env)
    (flag,) = await env.flags("overdue")
    assert "no longer has" not in flag.detail, "the visit was still on the day: the flag is about lateness alone"

    cancel_bobs_visit(env)
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    env.mailer.sent.clear()
    await next_round(env)

    video = await env.video(video_id)
    assert env.mailer.sent == [], "the customer of the cancelled visit is not emailed"
    assert (video.state, video.email_skip) == ("no_email", "off_route"), "it waits again, for the reason nobody had seen"
    assert await env.all(sa.select(db.fr_outbox)) == [], "and nothing is written to FieldRoutes"
    (flag,) = await flags_about(env, video_id)
    assert flag.kind == "off_route"
    assert "FieldRoutes no longer has that visit on this day" in flag.detail
    assert flag.detail.endswith("No email went out. Press Send it anyway, or Pull.")

    # Now the office has been told. A second press is its own choice, and it goes, once.
    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    await next_round(env)
    await next_round(env)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"] and (await env.video(video_id)).state == "sent"
    assert len(fr_writes(env, "note")) == 1 and await flags_about(env, video_id) == []


async def test_after_a_backup_is_put_back_the_late_flag_does_not_say_that_nobody_was_emailed(env: Env, clock: Clock) -> None:
    """After a restore the record of an email is gone with everything else since the
    backup. The flag used to read "Nobody was emailed. Press Send it anyway, or Pull."
    about a customer who had the email, and the button it named sent the second one. The
    server cannot know. So it says what it knows, and where the office can find out."""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock), recommend=True)
    backup = await take_backup(env)
    clock.forward(minutes=11)
    await one_pass(env)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"]  # Jane has her email
    token = (await env.video(video_id)).token

    clock.forward(days=2)
    await restore_backup(env, backup)
    await one_pass(env)
    (flag,) = await env.flags("overdue")
    assert "Nobody was emailed" not in flag.detail
    assert "This server has no record of an email for it." in flag.detail
    assert "If a backup was put back, the customer may already have it" in flag.detail
    assert "look for the video note on the customer's account in FieldRoutes before you send" in flag.detail
    # What it points at is really there: FieldRoutes was not put back, so the note from the
    # first time is still on her account, with this very link in it.
    (note,) = fr_writes(env, "note")
    assert f"{BASE}/w/{token}" in note.one("notes")
    # And the same warning stands beside the button itself, on the flag and on the day page.
    await office_login(env)
    page = (await env.http.get("/office")).text
    assert page.count("If a backup was put back, the customer may already have it") >= 2
    assert "before you press Send it anyway" in page


# == a backup put back while a video was waiting for the route ===========================
#
# A video the route was holding (its visit off the day) is let go by the route itself when
# the visit is back. Found waiting again after a restore, hours or days later, it must not
# go that way a second time: by then its email is long overdue, and the customer may have
# it, or the office may have pulled it.


async def held_for_the_route(env: Env, clock: Clock) -> str:
    headers = await ready(env)
    video_id = await film(env, headers, 502, recordedAt=just_now(clock))
    cancel_bobs_visit(env)
    clock.forward(minutes=11)
    await next_round(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "off_route") and env.mailer.sent == []
    return video_id


async def test_a_restore_while_a_video_waited_for_the_route_emails_nobody_twice(env: Env, clock: Clock) -> None:
    video_id = await held_for_the_route(env, clock)
    backup = await take_backup(env)
    put_bobs_visit_back(env)  # the office changes its mind: the visit is on again
    await next_round(env)
    await next_round(env)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"], "let go by the route, the same afternoon"
    assert len(fr_writes(env, "note")) == 1

    clock.forward(hours=9)
    await restore_backup(env, backup)
    for _ in range(3):
        await next_round(env)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"], "one email, not two"
    assert len(fr_writes(env, "note")) == 1, "and one note"
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "overdue"), "it waits for the office now"
    (flag,) = await flags_about(env, video_id)
    assert flag.kind == "overdue" and "Its email was 9 hours late" in flag.detail and flag.detail.endswith(LATE_FLAG_ENDS)


async def test_how_late_an_email_is_does_not_start_again_when_the_route_lets_a_video_go(env: Env, clock: Clock) -> None:
    """The route lets a video go five hours late, and then FieldRoutes cannot be read for
    more than another hour, so the email still cannot go. By the time it can, it is over
    six hours late, counted from when it was first due. Only a person's say-so (Send it
    anyway, a move) starts that count again; the route letting it go does not."""
    video_id = await held_for_the_route(env, clock)
    clock.forward(hours=5)
    put_bobs_visit_back(env)
    await next_round(env)  # the route lets it go: it is in line for the next pass
    assert (await env.video(video_id)).state == "held" and "video_released" in await env.actions()
    env.fake.down = True
    for _ in range(2):
        clock.forward(minutes=25)
        await next_round(env)
    assert env.mailer.sent == [] and (await env.video(video_id)).state == "held", "it waits while the day cannot be read"

    clock.forward(minutes=25)
    env.fake.down = False
    await next_round(env)
    video = await env.video(video_id)
    assert env.mailer.sent == [] and (video.state, video.email_skip) == ("no_email", "overdue")
    (flag,) = await flags_about(env, video_id)
    assert flag.kind == "overdue" and "Its email was 6 hours late" in flag.detail


async def test_a_restore_does_not_send_a_video_the_office_pulled_while_it_waited_for_the_route(
    env: Env, clock: Clock
) -> None:
    video_id = await held_for_the_route(env, clock)
    backup = await take_backup(env)
    await videos.pull(env.ctx, video_id, "owner@zest.test", "the visit never happened")
    put_bobs_visit_back(env)  # booked again for later the same day
    clock.forward(hours=9)
    await restore_backup(env, backup)
    for _ in range(3):
        await next_round(env)
    assert env.mailer.sent == [], "the pulled video is not emailed"
    assert fr_writes(env, "note") == [], "and its link is not put on the account"
    assert (await env.video(video_id)).email_skip == "overdue" and len(await env.flags("overdue")) == 1


# == killed between a record and what follows from it ===================================
#
# These were known gaps: a record was saved, then what follows from it in a second save,
# and a server killed between the two lost the second. Each is closed in videos.py: the
# record and its follow-ups are now one save. The first two used to stop the server by
# breaking the one function that made the second save. That function is gone, so they
# now stop it the plain way: at the save itself, the moment the record is written down.


async def test_killed_just_after_the_email_was_written_down_the_link_is_still_filed(
    env: Env, clock: Clock
) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock), recommend=True)
    clock.forward(minutes=11)

    async def the_email_is_written_down() -> bool:
        return (await env.video(video_id)).state == "sent"

    switch = kill_switch(env, when=the_email_is_written_down)
    await one_pass(env)
    assert switch.dead, "the server was stopped right after that save"
    assert len(env.mailer.sent) == 1

    clock.forward(minutes=10)
    await restart(env)
    assert len(env.mailer.sent) == 1, "never a second email"
    assert len(fr_writes(env, "note")) == 1, "the link reaches the customer's account"
    assert len(fr_writes(env, "task")) == 1 and len(await env.flags("recommend")) == 1, "and so does the follow-up"


async def test_killed_just_after_no_email_was_written_down_the_office_is_still_told(
    env: Env, clock: Clock
) -> None:
    """Acme has no email on file. The office is meant to be flagged, and the link saved as a
    note the customer can see."""
    headers = await ready(env)
    video_id = await film(env, headers, 503, recordedAt=just_now(clock))
    clock.forward(minutes=11)

    async def no_email_is_written_down() -> bool:
        return (await env.video(video_id)).state == "no_email"

    switch = kill_switch(env, when=no_email_is_written_down)
    await one_pass(env)
    assert switch.dead, "the server was stopped right after that save"
    assert (await env.video(video_id)).state == "no_email"

    clock.forward(minutes=10)
    await restart(env)
    assert len(await env.flags("no_email")) == 1, "the office is told no email went"
    assert len(fr_writes(env, "note")) == 1, "and the link is on the account"


async def test_killed_just_after_a_video_not_on_the_list_was_confirmed_the_office_is_still_told(
    env: Env, clock: Clock, monkeypatch
) -> None:
    headers = await ready(env)
    body = video_body(None, recordedAt=just_now(clock))
    assert (await env.http.post("/v1/videos", json=body, headers=headers)).status_code == 200
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    device = await auth.authenticate(env.ctx, headers["Authorization"].removeprefix("Bearer "))

    async def die(*args: Any, **kwargs: Any) -> None:
        raise Killed

    with monkeypatch.context() as patch:
        patch.setattr(videos, "_check_format", die)  # the first step after the state is saved
        with pytest.raises(Killed):
            await videos.complete(env.ctx, device, body["id"], {"sizeBytes": body["sizeBytes"]})
    assert (await env.video(body["id"])).state == "unmatched"

    await restart(env)
    # The phone never got its answer, so it confirms again.
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "unmatched"
    assert len(await env.flags("unmatched")) == 1, "the office is told there is a video to match"


async def test_two_servers_at_a_deploy_write_one_note_not_two(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    assert await videos.send_due(env.ctx) == 1

    new = replace(env)
    new.ctx = build_ctx(
        replace(env.ctx.cfg), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
        texter=env.texter, pusher=env.pusher,
    )
    for server in (env, new):  # a real FieldRoutes call takes a moment; so does this one
        write = server.ctx.fr.create_note

        async def slow(*args: Any, _write: Any = write, **kwargs: Any) -> None:
            await asyncio.sleep(0.05)
            await _write(*args, **kwargs)

        server.ctx.fr.create_note = slow  # type: ignore[method-assign]
    try:
        await asyncio.gather(videos.run_outbox(env.ctx), videos.run_outbox(new.ctx))
    finally:
        await new.ctx.engine.dispose()
    assert len(fr_writes(env, "note")) == 1


# == killed at every save in a video's life =============================================
#
# The tests above stop the server at chosen moments. This one stops it at every moment
# there is: before and after each save the server makes while one video is registered,
# confirmed, emailed, filed in FieldRoutes and scored. After each kill the server is
# started again, and whoever was waiting for an answer asks again, as the phone, the job
# loop and a customer's second press do.
#
# It is lived twice. Once as it has always been, with our own email. And once with the
# owner's "one email" setting on (`fr_email=True`): no email of ours goes, and the video's
# line for FieldRoutes' own email is one more follow-up that must be queued with the
# video's settling, written exactly once, and looked at once more before FieldRoutes
# sends. Each of those has a claim and an answer of its own, and each is a save the
# server is killed at.


class Life:
    """One video's whole life, with a server that can be killed once along the way."""

    def __init__(self, env: Env, clock: Clock, service: MailService):
        self.env, self.clock, self.service = env, clock, service
        self.kills = 0

    def mailer(self) -> ResendMailer:
        return ResendMailer(self.env.ctx.cfg, transport=httpx.MockTransport(self.service.handler))

    async def started_again(self) -> None:
        """The dead server is replaced. Time moves on first, past any claim the dead one held
        (5 minutes on an email, 10 on a FieldRoutes write)."""
        self.kills += 1
        self.clock.forward(minutes=11)
        await restart(self.env, mailer=self.mailer())

    async def asks(self, what: Any) -> Any:
        """The phone, or a customer, asks the server for something. If the server dies in
        the middle there is no answer, so they ask again once it is back."""
        try:
            return await what()
        except Killed:
            await self.started_again()
            return await what()

    async def minutes_go_by(self, rounds: int = 3) -> None:
        """The emailer and the FieldRoutes outbox, once a minute as in the job loop. A
        server that dies in one is started again and carries on."""
        for _ in range(rounds):
            for job in (videos.send_due, videos.run_outbox):
                try:
                    await job(self.env.ctx)
                except Killed:
                    await self.started_again()
            self.clock.forward(minutes=11)


async def live_one_life(
    env: Env, clock: Clock, *, at: int = 0, before: bool = False, fr_email: bool = False
) -> tuple[Life, DyingDatabase, str]:
    if fr_email:
        # The owner has FieldRoutes' own email carry the link, an hour after completion,
        # and Pat completes Jane's visit in FieldRoutes just as he films it.
        for appointment_id in (9001, 9002, 9003):
            env.fake.data["appointment"][appointment_id].update(status="1", dateCompleted=fr_time(clock.now()))
        await set_setting(env.ctx, "fr_email_minutes", "60", "owner@zest.test")
    headers = await ready(env)
    token = headers["Authorization"].removeprefix("Bearer ")
    life = Life(env, clock, MailService())
    env.ctx.mailer = life.mailer()
    body = video_body(501, recordedAt=just_now(clock), recommend=True, override=True)
    switch = kill_switch(env, at=at, before=before)

    async def register() -> Any:
        return await videos.register(env.ctx, await auth.authenticate(env.ctx, token), body)

    async def complete() -> Any:
        return await videos.complete(env.ctx, await auth.authenticate(env.ctx, token), body["id"], {"sizeBytes": 5000})

    async def score() -> None:
        await videos.save_score(env.ctx, await env.video(body["id"]), 2, "Missed a spot")

    assert (await life.asks(register))["id"] == body["id"]
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    # A try that was cut off is finished by the restart's own first pass, eleven minutes on:
    # by then the video has gone out, as our email or (`fr_email`) towards FieldRoutes' own.
    gone_out = "no_email" if fr_email else "sent"
    assert (await life.asks(complete))["state"] in ("held", gone_out), "confirmed, whichever try got through"
    clock.forward(minutes=11)
    await life.minutes_go_by()
    await life.asks(score)
    await life.minutes_go_by()
    return life, switch, body["id"]


async def what_is_left_after(env: Env, life: Life, video_id: str, *, fr_email: bool = False) -> list[str]:
    """Everything that must be true of the one video, however its life went. A list of
    what is wrong, in plain words; empty when nothing is."""
    wrong: list[str] = []
    video = await env.video(video_id)
    if len(await env.all(sa.select(db.videos))) != 1:
        wrong.append("there is not exactly one video")
    actions = await env.actions()
    if fr_email:
        # It went out in FieldRoutes' own email: no email of ours, and one line in the
        # technician's comments on his completed appointment.
        if (video.state, video.email_skip) != ("no_email", "fr_email") or video.emailed_at:
            wrong.append(f"the video is {video.state} ({video.email_skip}), not in FieldRoutes' email")
        if life.service.inbox:
            wrong.append(f"the customer got {len(life.service.inbox)} emails of ours")
        link = f"{BASE}/w/{video.token}"
        if env.fake.comments(9001).count(link) != 1 or link in env.fake.comments(9002) + env.fake.comments(9003):
            wrong.append("the line is not in the technician's comments exactly once, on one appointment")
        if env.fake.completed_by_api or env.fake.invoices:
            wrong.append("an appointment was completed by the server")
        once_each = ("video_registered", "video_uploaded", "email_skipped", "fr_email_line")
        if actions.count("email_sent"):
            wrong.append("the audit trail says an email of ours was sent")
    else:
        if video.state != "sent" or not video.emailed_at:
            wrong.append(f"the video is {video.state}, not sent")
        if [m["to"] for m in life.service.inbox] != [["jane@example.com"]]:
            wrong.append(f"the customer got {len(life.service.inbox)} emails")
        if env.fake.completions:
            wrong.append("appointment/complete was called with the setting off")
        once_each = ("video_registered", "video_uploaded", "email_sent")
    for once in once_each:
        if actions.count(once) != 1:
            wrong.append(f"the audit trail has {actions.count(once)} entries for {once}")
    saved = [e.detail for e in await env.all(sa.select(db.events).where(db.events.c.action == "score_saved"))]
    if not saved or sum(1 for d in saved if d["first"]) != 1:
        wrong.append(f"the audit trail has the score as {saved}")
    score = await videos.get_score(env.ctx, video_id)
    if score is None or (score.stars, score.comment) != (2, "Missed a spot"):
        wrong.append("the customer's score is not there")

    # Every FieldRoutes write is queued exactly once. Each was then either made, once, or
    # (if the server died in the middle of making it) left alone with the office told.
    rows = await env.all(sa.select(db.fr_outbox).order_by(db.fr_outbox.c.id))
    if sorted(r.kind for r in rows) != [*(["comment"] if fr_email else []), "note", "review", "task"]:
        wrong.append(f"queued for FieldRoutes: {sorted(r.kind for r in rows)}")
    flagged = {f.dedupe for f in await env.flags("fr_write_failed")}
    for row in rows:
        made = len(env.fake.completions) if row.kind == "comment" else len(fr_writes(env, row.kind))
        if made > 1:
            wrong.append(f"the {row.kind} was written to FieldRoutes {made} times")
        if row.done_at is not None and made != 1:
            wrong.append(f"the {row.kind} is marked written and FieldRoutes has {made}")
        if row.kind == "comment":
            # The line may be tried again after a kill (it reads before it writes), and
            # the hour leaves time for it: so it is always written, never given up on.
            # And it is not finished until the server has read the visit once more,
            # shortly before FieldRoutes sends, and seen the line still there.
            if not (row.payload.get("written_at") or row.payload.get("found_at")):
                wrong.append("the line for FieldRoutes' email was not written")
            elif row.done_at is None or not (row.payload.get("looked_at") or row.payload.get("found_at")):
                wrong.append("the line was written and never looked at again before FieldRoutes' email was due")
        elif row.done_at is None and f"fr:{row.id}" not in flagged:
            wrong.append(f"the {row.kind} was not written and the office was not told")
    if len(flagged) > life.kills:
        wrong.append("more writes were given up on than the server was killed")

    # The office's flags: each one that is owed, once.
    kinds = sorted(f.kind for f in await env.flags(open_only=False) if f.kind != "fr_write_failed")
    if kinds != ["low_score", "recommend", "wrong_house"]:
        wrong.append(f"the office's flags are {kinds}")
    return wrong


async def test_a_server_killed_at_any_save_in_a_videos_life_loses_nothing_and_doubles_nothing(
    tmp_path, clock: Clock
) -> None:
    async def one_life(name: str, *, fr_email: bool = False, **how: Any) -> tuple[DyingDatabase, list[str]]:
        folder = tmp_path / name
        folder.mkdir()
        clock.ahead = timedelta()
        env = await make_env(folder)
        try:
            life, switch, video_id = await live_one_life(env, clock, fr_email=fr_email, **how)
            wrong = await what_is_left_after(env, life, video_id, fr_email=fr_email)
            assert life.kills == (1 if switch.dead else 0)
            return switch, wrong
        finally:
            await env.http.aclose()
            await env.ctx.engine.dispose()

    saves: dict[bool, int] = {}
    for fr_email in (False, True):
        way = "the link in FieldRoutes' email: " if fr_email else ""
        mark = "fr-" if fr_email else ""
        # First with nobody killed: how many saves there are, and that the list above is right.
        quiet, wrong = await one_life(f"{mark}quiet", fr_email=fr_email)
        assert wrong == [] and not quiet.dead and quiet.saves >= 10, way
        saves[fr_email] = quiet.saves
        problems: list[str] = []
        killed = 0
        for at in range(1, quiet.saves + 1):
            for before in (True, False):
                switch, wrong = await one_life(
                    f"{mark}{'before' if before else 'after'}-{at}", fr_email=fr_email, at=at, before=before
                )
                killed += switch.dead
                problems += [f"{way}killed {'before' if before else 'after'} save {at}: {w}" for w in wrong]
        assert problems == [], "\n" + "\n".join(problems)
        assert killed >= 2 * quiet.saves - 4, "the server really was killed at (nearly) every one of them"
    assert saves[True] >= saves[False] + 4, (
        "the line's own saves are in the list: the claim and the answer of its write, and of the last look"
    )


# == a repeat finishes what a killed first try left undone ===============================
#
# Three steps are not part of the save they follow, because each has to look at
# something outside the database or outside the one video. They are safe to run again,
# and the repeat of the call runs them again.


async def test_a_register_that_was_cut_off_is_finished_when_the_phone_asks_again(env: Env, clock: Clock) -> None:
    """Jane's stop has been flagged "finished with no video". Pat's video for it registers,
    past a warning. The server dies the moment the video is saved, before that flag is
    cleared. The phone got no answer, so it registers again."""
    headers = await ready(env)
    await env.finished(501, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert [(f.customer_id, f.tech_id) for f in await env.flags("no_video")] == [(501, 20)]
    token = headers["Authorization"].removeprefix("Bearer ")
    body = video_body(501, recordedAt=just_now(clock), override=True)

    async def the_video_is_saved() -> bool:
        return await env.video(body["id"]) is not None

    switch = kill_switch(env, when=the_video_is_saved)
    with pytest.raises(Killed):
        await videos.register(env.ctx, await auth.authenticate(env.ctx, token), body)
    assert switch.dead and len(await env.flags("no_video")) == 1, "killed before the flag was cleared"
    # What belongs to the video itself was saved with it: the audit entry and the office's flag.
    assert (await env.actions()).count("video_registered") == 1
    assert [f.video_id for f in await env.flags("wrong_house")] == [body["id"]]

    await restart(env, jobs_too=False)
    r = await env.http.post("/v1/videos", json=body, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "awaiting_upload" and r.json()["upload"]["method"] == "PUT"
    assert await env.flags("no_video") == [], "the repeat cleared it: the stop has a video on the way"
    assert len(await env.all(sa.select(db.videos))) == 1 and (await env.actions()).count("video_registered") == 1
    assert len(await env.flags("wrong_house", open_only=False)) == 1


async def test_a_confirm_that_was_cut_off_still_gets_its_file_looked_at(env: Env, clock: Clock, monkeypatch) -> None:
    """The phone sent HEVC, which many customers cannot play. The server dies after the
    video is confirmed and before its file is looked at. The phone confirms again."""
    def hevc(pad: int = 4000) -> bytes:  # just enough of an MP4 for the format check
        def table(fmt: str) -> bytes:
            return b"\x00\x00\x00\x30stsd" + b"\x00" * 4 + b"\x00\x00\x00\x01" + b"\x00\x00\x00\x20" + fmt.encode() + b"\x00" * 16
        return b"\x00\x00\x00\x18ftypmp42" + b"moov" + table("mp4a") + table("hvc1") + b"\x01\x02\x03\x04" * (pad // 4)

    headers = await ready(env)
    data = hevc()
    body = video_body(501, recordedAt=just_now(clock), sizeBytes=len(data))
    assert (await env.http.post("/v1/videos", json=body, headers=headers)).status_code == 200
    env.storage.objects[(await env.video(body["id"])).object_key] = data
    device = await auth.authenticate(env.ctx, headers["Authorization"].removeprefix("Bearer "))

    async def die(*args: Any, **kwargs: Any) -> None:
        raise Killed

    with monkeypatch.context() as patch:
        patch.setattr(videos, "_check_format", die)
        with pytest.raises(Killed):
            await videos.complete(env.ctx, device, body["id"], {"sizeBytes": len(data)})
    assert (await env.video(body["id"])).state == "held" and await env.flags("bad_format") == []

    await restart(env, jobs_too=False)
    for _ in range(2):  # and once more for good measure: still one flag
        r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": len(data)}, headers=headers)
        assert r.status_code == 200 and r.json()["state"] == "held"
    (flag,) = await env.flags("bad_format")
    assert flag.video_id == body["id"] and "HEVC" in flag.detail
    assert (await env.actions()).count("video_uploaded") == 1


async def test_a_score_saved_without_its_review_gets_the_review_at_the_next_press(env: Env, clock: Clock) -> None:
    """How an older server could leave things when it was killed between saving a score
    and queueing it for FieldRoutes: the score is there and no review is waiting. The
    review used to be queued only with the very first save, so it was lost for good."""
    headers = await ready(env)
    video_id = await film(env, headers, 501, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await one_pass(env)
    video = await env.video(video_id)
    await env.run(sa.insert(db.scores).values(video_id=video_id, stars=4, comment="", created_at=db.now()))
    assert [r.kind for r in await env.all(sa.select(db.fr_outbox))] == ["note"]

    r = await env.http.post(f"/w/{video.token}/score", data={"stars": "4", "comment": "Good work"})
    assert r.status_code == 303
    await one_pass(env)
    (review,) = fr_writes(env, "review")
    assert review.one("starRating") == "4" and review.one("feedback") == "Good work"

    # A changed score after that is kept here and not sent again: FieldRoutes takes one.
    await env.http.post(f"/w/{video.token}/score", data={"stars": "5", "comment": "Even better"})
    clock.forward(minutes=11)
    await one_pass(env)
    assert len(fr_writes(env, "review")) == 1 and (await videos.get_score(env.ctx, video_id)).stars == 5
