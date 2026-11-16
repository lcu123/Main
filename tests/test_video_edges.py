"""Edge cases the owner asked about by name: restarts, kills in the middle of an upload,
two things happening at once. From the server's side each one comes down to the same
promise: nothing is lost, and nothing happens twice."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
import sqlalchemy as sa

from video_helpers import Env, film, login, make_env, video_body

from zest_video import db, jobs, security, videos
from zest_video.app import build_app, build_ctx


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


async def ready(env: Env) -> dict[str, str]:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.mailer.sent.clear()
    return headers


# -- the phone was killed, or its request arrived twice -----------------------------


async def test_killed_after_the_upload_landed_but_before_confirming(env: Env) -> None:
    headers = await ready(env)
    body = video_body(501)
    first = (await env.http.post("/v1/videos", json=body, headers=headers)).json()
    key = (await env.video(body["id"])).object_key
    env.storage.objects[key] = b"v" * body["sizeBytes"]  # the bucket has it; the app died here

    # Relaunch: the app registers the same video again, then confirms.
    again = await env.http.post("/v1/videos", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["state"] == "awaiting_upload"
    assert key in again.json()["upload"]["url"] and key in first["upload"]["url"], "same file, not a second one"
    done = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert done.status_code == 200 and done.json()["state"] == "held"
    assert len(await env.all(sa.select(db.videos))) == 1


async def test_the_same_registration_arriving_twice_at_once_makes_one_video(env: Env, monkeypatch) -> None:
    headers = await ready(env)
    body = video_body(501)
    assert (await env.http.post("/v1/videos", json=body, headers=headers)).status_code == 200

    # A slow first request and the phone's retry overlap: the retry looks before the
    # first has written, so its own write collides. Make that lookup miss once.
    real = videos.get_video
    calls = {"n": 0}

    async def stale_once(ctx, video_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(ctx, video_id)

    monkeypatch.setattr(videos, "get_video", stale_once)
    r = await env.http.post("/v1/videos", json=body, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "awaiting_upload" and r.json()["upload"]["url"], "the retry must not retire its own twin"
    rows = await env.all(sa.select(db.videos))
    assert [(v.id, v.state) for v in rows] == [(body["id"], "awaiting_upload")]


async def test_really_simultaneous_requests_for_one_video(env: Env) -> None:
    headers = await ready(env)
    body = video_body(501)
    answers = await asyncio.gather(*(env.http.post("/v1/videos", json=body, headers=headers) for _ in range(6)))
    assert {a.status_code for a in answers} == {200}
    assert len(await env.all(sa.select(db.videos))) == 1

    key = (await env.video(body["id"])).object_key
    env.storage.objects[key] = b"v" * body["sizeBytes"]
    done = await asyncio.gather(
        *(env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
          for _ in range(6))
    )
    assert {d.status_code for d in done} == {200} and {d.json()["state"] for d in done} == {"held"}
    uploaded = [a for a in await env.actions() if a == "video_uploaded"]
    assert len(uploaded) == 1, "confirmed once, however many times it was asked"


async def test_a_deleted_or_replaced_video_is_never_brought_back_by_a_late_retry(env: Env) -> None:
    headers = await ready(env)
    body = video_body(501)
    await env.http.post("/v1/videos", json=body, headers=headers)
    key = (await env.video(body["id"])).object_key
    assert (await env.http.delete(f"/v1/videos/{body['id']}", headers=headers)).json()["state"] == "deleted"

    # The upload was already in the air and lands after the delete.
    env.storage.objects[key] = b"v" * body["sizeBytes"]
    late = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert late.status_code == 200 and late.json()["state"] == "deleted"
    assert key not in env.storage.objects, "the late file is removed, not left behind"

    again = await env.http.post("/v1/videos", json=body, headers=headers)
    assert again.json()["state"] == "deleted" and again.json()["upload"] is None
    assert await videos.send_due(env.ctx) == 0 and env.mailer.sent == []


async def test_a_file_that_lands_after_its_video_was_replaced_is_swept_up(env: Env) -> None:
    headers = await ready(env)
    first = video_body(501)
    await env.http.post("/v1/videos", json=first, headers=headers)
    old_key = (await env.video(first["id"])).object_key
    second = await film(env, headers, 501)  # recorded again; the first is retired
    assert (await env.video(first["id"])).state == "replaced"

    env.storage.objects[old_key] = b"late"  # the first upload finishes anyway; the app never confirms it
    await jobs.daily(env.ctx)
    assert old_key not in env.storage.objects
    assert (await env.video(second)).object_key in env.storage.objects, "the live video's file is untouched"


async def test_killed_right_after_tapping_save_the_video_still_goes_once(env: Env) -> None:
    headers = await ready(env)
    body = video_body(501)
    # First launch: register went out, then the app died. Second launch: the whole thing again.
    for _ in range(2):
        r = await env.http.post("/v1/videos", json=body, headers=headers)
        assert r.status_code == 200
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    for _ in range(2):
        await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    await env.window_closed(body["id"])
    assert await videos.send_due(env.ctx) == 1
    assert await videos.send_due(env.ctx) == 0
    assert len(env.mailer.sent) == 1


# -- the server was restarted, or did two things at once --------------------------------


async def test_a_server_restart_keeps_every_phone_signed_in_and_every_video(env: Env) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    from zest_video.core import set_setting

    await set_setting(env.ctx, "hold_minutes", 7, "owner@zest.test")

    # A new process on the same database: fresh memory, same data.
    ctx2 = build_ctx(
        replace(env.ctx.cfg), fr_client=env.ctx.fr.client, storage=env.storage, mailer=env.mailer,
        texter=env.texter, pusher=env.pusher, engine=db.make_engine(env.ctx.cfg.database_url),
    )
    http2 = httpx.AsyncClient(transport=httpx.ASGITransport(app=build_app(ctx2, run_jobs=False)), base_url="https://video.test")
    try:
        today = await http2.get("/v1/today", headers=headers)
        assert today.status_code == 200, "the phone's sign-in survives a restart"
        assert today.json()["config"]["holdMinutes"] == 7, "so do the owner's settings"
        assert today.json()["stops"][0]["video"]["id"] == video_id

        await env.window_closed(video_id)
        assert await videos.send_due(ctx2) == 1, "a video held before the restart is still emailed after it"
        assert len(env.mailer.sent) == 1
    finally:
        await http2.aclose()
        await ctx2.engine.dispose()


async def test_two_passes_of_the_emailer_at_once_send_one_email(env: Env) -> None:
    headers = await ready(env)
    for customer in (501, 502):
        await env.window_closed(await film(env, headers, customer))
    sent = await asyncio.gather(*(videos.send_due(env.ctx) for _ in range(4)))
    assert sum(sent) == 2
    assert sorted(m.to for m in env.mailer.sent) == ["bob@example.com", "jane@example.com"]


async def test_a_crash_between_sending_and_recording_it_cannot_double_the_email(env: Env, monkeypatch) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    await env.window_closed(video_id)

    # The provider accepted the email, then the server died before writing "sent".
    async def dies(*args, **kwargs):
        return False

    monkeypatch.setattr(videos, "_finish", dies)
    await videos.send_due(env.ctx)
    monkeypatch.undo()
    video = await env.video(video_id)
    assert video.state == "held" and video.email_due_at > db.now(), "left for a later pass, not hammered"

    await env.window_closed(video_id)
    assert await videos.send_due(env.ctx) == 1
    first, second = env.mailer.sent
    assert first.idempotency_key == second.idempotency_key != "", (
        "both attempts carry one key, so the mail provider delivers a single email"
    )
    assert (await env.video(video_id)).state == "sent"


async def test_two_videos_for_one_visit_that_slipped_in_together_still_mean_one_email(env: Env) -> None:
    headers = await ready(env)
    first = await film(env, headers, 501)
    row = await env.video(first)
    twin = dict(row._mapping)
    twin.update(id=str(uuid.uuid4()), token=security.new_watch_token(), object_key=row.object_key + ".twin",
                recorded_at=row.recorded_at + timedelta(seconds=40))
    await env.run(sa.insert(db.videos).values(**twin))  # as if both registrations got in at the same instant
    env.storage.objects[twin["object_key"]] = b"v" * row.size_bytes
    for vid in (first, twin["id"]):
        await env.window_closed(vid)

    await videos.send_due(env.ctx)
    await videos.send_due(env.ctx)
    assert len(env.mailer.sent) == 1, "one email for one tech's visit, whatever happened"
    older, newer = await env.video(first), await env.video(twin["id"])
    assert (older.state, newer.state) == ("replaced", "sent"), "the newest recording is the one that goes"
    assert row.object_key not in env.storage.objects


async def test_many_phones_opening_at_once_cost_one_fieldroutes_refresh(env: Env) -> None:
    pat = await login(env)
    sam = await login(env, "sam@example.com")
    before = len(env.fake.requests)
    answers = await asyncio.gather(*(env.http.get("/v1/today", headers=h) for h in (pat, sam) * 5))
    assert {a.status_code for a in answers} == {200}
    searches = [r for r in env.fake.requests[before:] if (r.entity, r.action) == ("appointment", "search")]
    assert len(searches) == 1, "ten phones, one read against the shared quota"


async def test_a_phone_cut_off_while_its_video_waits_can_finish_after_signing_in_again(env: Env) -> None:
    headers = await ready(env)
    body = video_body(501)
    await env.http.post("/v1/videos", json=body, headers=headers)
    device = await env.one(sa.select(db.devices))
    await env.run(sa.update(db.devices).where(db.devices.c.id == device.id).values(revoked_at=db.now()))
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": 1}, headers=headers)
    assert r.status_code == 401, "the app drops to sign-in and keeps its queue"

    again = await login(env)  # same tech, new token
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    r = await env.http.post("/v1/videos", json=body, headers=again)
    assert r.status_code == 200 and r.json()["state"] == "awaiting_upload"
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=again)
    assert r.status_code == 200 and r.json()["state"] == "held"
