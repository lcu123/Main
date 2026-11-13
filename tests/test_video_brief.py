"""Rules from the owner's build brief whose proof was thin.

An independent read of the brief against the tests (Nov 13, 2026) found rules that were
implemented but only weakly tested, or not at all. Each test here pins one of them, in
the order of what it would cost if it broke: the wrong customer getting a video, a video
lost, an email sent twice, access not cut off, staff doing owner-only things.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa

from video_helpers import OWNER, Env, film, login, make_env, minutes_ago, office_login, today, video_body

from zest_video import auth, db, jobs, security, videos
from zest_video.core import DEFAULT_SETTINGS, set_setting
from zest_video.notify import DeliveryError, Email, Pusher, ResendMailer, Texter
from zest_video.storage import S3Storage

STAFF = "staff@zest.test"
ROOT = Path(__file__).resolve().parents[1]


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


async def send(env: Env, *video_ids: str) -> None:
    for video_id in video_ids:
        await env.window_closed(video_id)
    while await videos.send_due(env.ctx):
        pass


def iso_minutes_ago(minutes: float) -> str:
    return (db.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def staff_session(env: Env) -> str:
    """Add a staff user as the owner, then sign in as that staff user. Returns staff's CSRF token."""
    csrf = await office_login(env)
    await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff"})
    await env.http.post("/office/logout", data={"csrf": csrf})
    return await office_login(env, STAFF)


# == the wrong customer gets a video ===================================================


async def test_the_file_a_customer_is_sent_is_the_file_that_was_confirmed(env: Env) -> None:
    """The phone's upload link lives for a day so a slow upload can finish. Different
    bytes put over a confirmed video with that link must never reach a customer."""
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    key = (await env.video(video_id)).object_key
    env.storage.objects[key] = b"something else entirely"

    await send(env, video_id)
    video = await env.video(video_id)
    assert video.state == "pulled" and "changed after it was uploaded" in video.ended_reason
    assert env.mailer.sent == [], "no email for a file that is not the one confirmed"
    (flag,) = await env.flags("file_changed")
    assert flag.video_id == video_id and "pulled" in flag.detail
    assert (await env.http.get(f"/w/{video.token}")).status_code == 410


async def test_a_file_swapped_after_the_email_went_is_never_played(env: Env) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    await send(env, video_id)
    video = await env.video(video_id)
    assert video.state == "sent"
    assert (await env.http.get(f"/w/{video.token}/video")).status_code == 302, "the honest file plays"

    env.storage.objects[video.object_key] = b"x" * video.size_bytes  # same size, different bytes
    r = await env.http.get(f"/w/{video.token}/video")
    assert r.status_code == 410 and "location" not in r.headers
    assert (await env.video(video_id)).state == "pulled"
    assert len(await env.flags("file_changed")) == 1
    assert (await env.http.get(f"/w/{video.token}")).status_code == 410, "and the link is dead from then on"


async def test_a_storage_outage_is_not_taken_for_a_changed_file(env: Env) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    await send(env, video_id)
    video = await env.video(video_id)

    async def down(key: str):
        raise httpx.ConnectError("bucket unreachable")

    env.storage.head = down  # type: ignore[method-assign]
    assert (await env.http.get(f"/w/{video.token}/video")).status_code == 302
    assert (await env.video(video_id)).state == "sent" and await env.flags("file_changed") == []


async def test_at_a_shared_property_the_time_check_uses_each_techs_own_check_in(env: Env) -> None:
    # Sam was there 90 to 70 minutes ago. Pat checked in 5 minutes ago and is still there.
    env.fake.data["appointment"][9010] = {
        "appointmentID": "9010", "officeID": "7", "customerID": "502", "routeID": "71", "type": "80", "status": "1",
        "assignedTech": "21", "servicedBy": "21", "employeeID": "22", "date": today(), "start": "07:00:00",
        "sequence": "1", "checkIn": minutes_ago(90), "checkOut": minutes_ago(70),
    }
    pat = await ready(env)
    sam = await login(env, "sam@example.com")
    sams = await film(env, sam, 502, recordedAt=iso_minutes_ago(80))  # inside Sam's own visit
    pats = await film(env, pat, 502, recordedAt=iso_minutes_ago(80))  # inside Sam's visit, long before Pat's
    assert (await env.video(sams)).time_check == "match"
    assert (await env.video(pats)).time_check == "mismatch", "Sam's check-in does not vouch for Pat's video"
    await send(env, sams, pats)
    flagged = {f.video_id for f in await env.flags("wrong_house")}
    assert flagged == {pats}, "flagged for the office, and only the one that is out of line"
    assert (await env.video(pats)).state == "sent", "a flag, never a block"


async def test_two_videos_at_one_property_file_two_notes_and_pulling_one_leaves_the_other(env: Env) -> None:
    env.fake.data["appointment"][9010] = {
        "appointmentID": "9010", "officeID": "7", "customerID": "502", "routeID": "71", "type": "80", "status": "1",
        "assignedTech": "21", "servicedBy": "21", "employeeID": "22", "date": today(), "start": "07:00:00",
        "sequence": "1", "checkIn": minutes_ago(90), "checkOut": minutes_ago(70),
    }
    pat = await ready(env)
    sam = await login(env, "sam@example.com")
    pats, sams = await film(env, pat, 502), await film(env, sam, 502)
    await send(env, pats, sams)
    notes = [r for r in await env.all(sa.select(db.fr_outbox)) if r.kind == "note"]
    assert len(notes) == 2 and len({n.payload["text"] for n in notes}) == 2, "one link note per video"

    sam_video, pat_video = await env.video(sams), await env.video(pats)
    r = await env.http.post(f"/w/{sam_video.token}/not-my-home")
    assert r.status_code in (200, 303)
    assert (await env.video(sams)).state == "pulled"
    assert (await env.video(pats)).state == "sent", "the other tech's video is untouched"
    assert (await env.http.get(f"/w/{pat_video.token}")).status_code == 200


# == a video lost, or missing with nobody told =========================================


async def test_the_job_loop_itself_sends_the_email_and_runs_the_daily_check(env: Env) -> None:
    """Every other test calls a job by hand. This one runs the loop the server runs, so a
    mis-wired loop (no emails, no flags, no daily check) cannot hide."""
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    await env.window_closed(video_id)
    env.fake.data["employee"][21]["active"] = "0"  # Sam is let go today
    sam_before = await env.one(sa.select(db.techs).where(db.techs.c.id == 21))
    assert sam_before.active is True

    loop = asyncio.create_task(jobs.run_forever(env.ctx, tick=0.02))
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if (await env.video(video_id)).state == "sent":
                break
        await asyncio.sleep(0.2)  # several more ticks: nothing may happen twice
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)

    assert (await env.video(video_id)).state == "sent"
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "one email, however many ticks went by"
    assert (await env.one(sa.select(db.techs).where(db.techs.c.id == 21))).active is False, "the daily check ran"
    assert {name for name, _, _ in jobs.JOBS} >= {"send_due", "daily"}


async def test_a_job_that_blows_up_does_not_stop_the_others(env: Env, monkeypatch, capsys) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    await env.window_closed(video_id)

    async def boom(ctx):
        raise RuntimeError("this job is broken")

    monkeypatch.setattr(jobs, "JOBS", [("broken", 1, boom), *jobs.JOBS])
    loop = asyncio.create_task(jobs.run_forever(env.ctx, tick=0.02))
    try:
        for _ in range(200):
            await asyncio.sleep(0.02)
            if (await env.video(video_id)).state == "sent":
                break
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
    assert (await env.video(video_id)).state == "sent"
    assert "this job is broken" in capsys.readouterr().err, "and the failure is in the log, not swallowed"


async def test_a_video_not_on_the_list_can_be_deleted_for_ten_minutes_and_not_after(env: Env) -> None:
    headers = await ready(env)
    fresh = await film(env, headers, None, recordedAt=iso_minutes_ago(2))
    assert (await env.http.delete(f"/v1/videos/{fresh}", headers=headers)).status_code == 200

    old = await film(env, headers, None, recordedAt=iso_minutes_ago(2))
    await env.run(sa.update(db.videos).where(db.videos.c.id == old).values(
        recorded_at=db.now() - timedelta(minutes=40), uploaded_at=db.now() - timedelta(minutes=30)))
    r = await env.http.delete(f"/v1/videos/{old}", headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "too_late" and "office" in r.json()["message"]
    assert (await env.video(old)).state == "unmatched", "it is the office's to match or pull now"


# == an email sent twice: the real clients' requests ===================================


def recorder(status: int = 200, body: dict | None = None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"id": "msg_123"})

    return seen, httpx.MockTransport(handler)


async def test_the_email_request_carries_the_key_that_stops_a_second_copy(env: Env) -> None:
    cfg = replace(env.ctx.cfg, resend_api_key="re_test", mail_from="Zest <video@zest.test>")
    seen, transport = recorder()
    mailer = ResendMailer(cfg, transport=transport)
    email = Email(to="jane@example.com", subject="Your video", html="<p>hi</p>", text="hi", reply_to="office@zest.test",
                  headers={"List-Unsubscribe": "<https://video.test/u/abc>"}, idempotency_key="video-1-abcd", tag="video")
    assert await mailer.send(email) == "msg_123"
    assert await mailer.send(email) == "msg_123"
    first, second = seen
    assert str(first.url) == "https://api.resend.com/emails" and first.method == "POST"
    assert first.headers["authorization"] == "Bearer re_test"
    assert first.headers["idempotency-key"] == second.headers["idempotency-key"] == "video-1-abcd"
    sent = json.loads(first.content)
    assert sent["from"] == "Zest <video@zest.test>" and sent["to"] == ["jane@example.com"]
    assert sent["reply_to"] == "office@zest.test" and sent["tags"] == [{"name": "kind", "value": "video"}]
    assert sent["headers"]["List-Unsubscribe"] == "<https://video.test/u/abc>"
    assert sent["html"] == "<p>hi</p>" and sent["text"] == "hi"


@pytest.mark.parametrize("status", [400, 401, 422, 429, 500, 503])
async def test_an_email_the_provider_refuses_is_a_failure_not_a_sent_email(env: Env, status: int) -> None:
    seen, transport = recorder(status, {"message": "no"})
    mailer = ResendMailer(replace(env.ctx.cfg, resend_api_key="re_test"), transport=transport)
    with pytest.raises(DeliveryError):
        await mailer.send(Email(to="a@b.co", subject="s", html="h", text="t"))


async def test_an_unreachable_email_provider_is_a_failure_the_server_retries(env: Env) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    mailer = ResendMailer(replace(env.ctx.cfg, resend_api_key="re_test"), transport=httpx.MockTransport(handler))
    with pytest.raises(DeliveryError):
        await mailer.send(Email(to="a@b.co", subject="s", html="h", text="t"))


async def test_the_text_request_goes_to_the_techs_number_from_the_office_line(env: Env) -> None:
    cfg = replace(env.ctx.cfg, quo_api_key="quo_test", quo_from="+19165550100")
    seen, transport = recorder(202, {"data": {"id": "AC1"}})
    texter = Texter(cfg, transport=transport)
    assert texter.configured
    await texter.send("9165550101", "123456 is your Zest Video code.")
    (request,) = seen
    assert str(request.url) == "https://api.quo.com/v1/messages" and request.method == "POST"
    assert request.headers["authorization"] == "quo_test", "Quo takes the bare key, no Bearer"
    assert json.loads(request.content) == {
        "content": "123456 is your Zest Video code.", "from": "+19165550100", "to": ["+19165550101"]}

    refused, transport = recorder(402, {"message": "out of credits"})
    with pytest.raises(DeliveryError):
        await Texter(cfg, transport=transport).send("9165550101", "x")
    assert not Texter(replace(cfg, quo_api_key="")).configured
    with pytest.raises(DeliveryError):
        await Texter(replace(cfg, quo_api_key="")).send("9165550101", "x")


async def test_the_push_request_is_signed_and_a_dead_phone_token_is_reported(env: Env) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import jwt

    key = ec.generate_private_key(ec.SECP256R1())
    p8 = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    cfg = replace(env.ctx.cfg, apns_key_p8=p8, apns_key_id="KEY123", apns_team_id="TEAM456", apns_bundle_id="com.zestlawnpest.video")
    answers = iter([200, 410, 400, 500])
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(next(answers), json={})

    pusher = Pusher(cfg)
    pusher._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await pusher.send("ab" * 32, title="A video is waiting", body="Open the app.", sandbox=False) is True
    assert await pusher.send("ab" * 32, title="t", body="b", sandbox=True) is False, "410: the phone is gone"
    assert await pusher.send("ab" * 32, title="t", body="b", sandbox=False) is False, "400: a bad token"
    with pytest.raises(DeliveryError):
        await pusher.send("ab" * 32, title="t", body="b", sandbox=False)

    live, sandbox = seen[0], seen[1]
    assert str(live.url) == f"https://api.push.apple.com/3/device/{'ab' * 32}"
    assert sandbox.url.host == "api.sandbox.push.apple.com"
    assert live.headers["apns-topic"] == "com.zestlawnpest.video" and live.headers["apns-push-type"] == "alert"
    token = live.headers["authorization"].removeprefix("bearer ")
    claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], options={"verify_iat": False})
    assert claims["iss"] == "TEAM456" and jwt.get_unverified_header(token)["kid"] == "KEY123"
    assert json.loads(live.content)["aps"]["alert"] == {"title": "A video is waiting", "body": "Open the app."}
    assert seen[1].headers["authorization"] == live.headers["authorization"], "the signed token is reused, not re-made per push"


async def test_the_bucket_client_reads_size_and_etag_and_treats_missing_as_missing(env: Env) -> None:
    cfg = replace(env.ctx.cfg, s3_endpoint="https://t3.example.dev", s3_bucket="zest-videos", s3_region="auto",
                  s3_access_key="AKIDEXAMPLE", s3_secret_key="secret", s3_path_style=False)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "HEAD" and "there.mp4" in request.url.path:
            return httpx.Response(200, headers={"content-length": "5000", "etag": '"abc123"'})
        if request.method == "HEAD" and "boom.mp4" in request.url.path:
            return httpx.Response(503)
        if request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404)

    storage = S3Storage(cfg, transport=httpx.MockTransport(handler))
    stored = await storage.head("videos/2026-11-13/there.mp4")
    assert (stored.size, stored.etag) == (5000, "abc123")
    assert await storage.head("videos/2026-11-13/gone.mp4") is None
    with pytest.raises(httpx.HTTPStatusError):
        await storage.head("videos/2026-11-13/boom.mp4")
    await storage.delete("videos/2026-11-13/there.mp4")
    await storage.delete("videos/2026-11-13/gone.mp4")  # already gone is fine
    assert all(r.url.host == "zest-videos.t3.example.dev" for r in seen), "virtual-host style, as the live bucket needs"
    assert all("X-Amz-Signature=" in str(r.url) and "secret" not in str(r.url) for r in seen)
    put = storage.put_url("videos/2026-11-13/a.mp4", "video/mp4", 86400)
    assert "X-Amz-SignedHeaders=content-type%3Bhost" in put and "X-Amz-Expires=86400" in put
    await storage.aclose()


# == access not cut off ================================================================


async def test_a_tech_let_go_in_fieldroutes_is_locked_out_by_the_daily_check_itself(env: Env) -> None:
    headers = await ready(env)
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
    env.fake.data["employee"][20]["active"] = "0"
    await jobs.daily(env.ctx)
    r = await env.http.get("/v1/today", headers=headers)
    assert r.status_code == 401, "his phone is signed out within a day, with no office step"
    env.texter.sent.clear()
    r = await env.http.post("/v1/auth/request-code", json={"contact": "916-555-0101"})
    assert r.status_code == 200 and env.texter.sent == [] and env.mailer.sent == [], "and he cannot get a new code"


async def test_a_login_code_lives_for_exactly_fifteen_minutes(env: Env) -> None:
    assert security.CODE_MINUTES == 15 and security.CODE_MAX_TRIES == 5
    await env.http.post("/v1/auth/request-code", json={"contact": "916-555-0101"})
    row = await env.one(sa.select(db.login_codes).order_by(db.login_codes.c.id.desc()))
    assert row.expires_at - row.created_at == timedelta(minutes=15)

    code = await auth._new_code(env.ctx, "tech", "20")
    await env.run(sa.update(db.login_codes).where(db.login_codes.c.used_at.is_(None)).values(
        created_at=db.now() - timedelta(minutes=14, seconds=50), expires_at=db.now() + timedelta(seconds=10)))
    ok = await env.http.post("/v1/auth/verify", json={"contact": "916-555-0101", "code": code})
    assert ok.status_code == 200, "ten seconds before the end it still works"

    code = await auth._new_code(env.ctx, "tech", "20")
    await env.run(sa.update(db.login_codes).where(db.login_codes.c.used_at.is_(None)).values(
        expires_at=db.now() - timedelta(seconds=1)))
    late = await env.http.post("/v1/auth/verify", json={"contact": "916-555-0101", "code": code})
    assert late.status_code == 401, "a second after, it does not"


async def test_no_server_key_is_shipped_in_the_app_or_sent_to_a_phone(env: Env) -> None:
    """FieldRoutes keys and storage keys live on the server only."""
    names = re.compile(
        r"FR_AUTH_(KEY|TOKEN)|authenticationKey|authenticationToken|VIDEO_SECRET|S3_SECRET|SECRET_ACCESS_KEY"
        r"|RESEND_API_KEY|QUO_API_KEY|APNS_KEY|BEGIN (EC |RSA )?PRIVATE KEY|AKIA[0-9A-Z]{16}|re_[A-Za-z0-9]{20,}"
    )
    offenders = []
    for path in (ROOT / "ios").rglob("*"):
        if not path.is_file() or path.suffix.lower() in {".png", ".jpg", ".woff2", ".mp4", ".mov"} or ".build" in path.parts:
            continue
        text = path.read_text(errors="ignore")
        for match in names.finditer(text):
            line = text[: match.start()].count("\n") + 1
            context = text.splitlines()[line - 1]
            # A workflow or doc may NAME a secret it reads from the CI vault; it may not hold one.
            if "secrets." in context or "${{" in context:
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group(0)}")
    assert offenders == [], offenders

    # And nothing the phone is sent carries one: only a signed, expiring upload link.
    cfg = env.ctx.cfg
    env.reconfigure(s3_secret_key="s3-SECRET-value", s3_access_key="AKIDEXAMPLEKEY", resend_api_key="re_SECRET",
                    quo_api_key="quo_SECRET", secret="server-SECRET")
    headers = await login(env)
    bodies = [(await env.http.get("/v1/today", headers=headers)).text]
    r = await env.http.post("/v1/videos", json=video_body(501), headers=headers)
    bodies.append(r.text)
    for body in bodies:
        for secret in ("s3-SECRET-value", "re_SECRET", "quo_SECRET", "server-SECRET", cfg.fr_auth_key if hasattr(cfg, "fr_auth_key") else "\x00"):
            assert secret not in body
    assert set(r.json()["upload"]) == {"method", "url", "headers", "expiresAt"}


async def test_guessing_codes_from_one_address_is_cut_off_for_techs_and_office_alike(env: Env) -> None:
    for attempt in range(30):
        r = await env.http.post("/v1/auth/verify", json={"contact": f"916-555-{attempt:04d}", "code": "000000"})
        assert r.status_code == 401
    r = await env.http.post("/v1/auth/verify", json={"contact": "916-555-0101", "code": "000000"})
    assert r.status_code == 429 and r.json()["error"] == "slow_down" and int(r.headers["retry-after"]) > 0

    env.ctx.limiter.reset()
    for _ in range(5):
        assert (await env.http.post("/office/login", data={"email": OWNER})).status_code == 200
    r = await env.http.post("/office/login", data={"email": OWNER})
    assert r.status_code == 429 or "Too many" in r.text, "a sixth code for one address in 15 minutes is refused"

    env.ctx.limiter.reset()
    for _ in range(30):
        await env.http.post("/office/verify", data={"email": OWNER, "code": "000000"})
    r = await env.http.post("/office/verify", data={"email": OWNER, "code": "000000"})
    assert r.status_code == 429 or "Too many" in r.text


# == staff doing owner-only things =====================================================


async def test_staff_are_refused_every_owner_only_change_even_with_a_valid_form(env: Env) -> None:
    csrf = await staff_session(env)
    before = (await env.http.get("/v1/today", headers=await login(env))).json()["config"]

    r = await env.http.post("/office/settings", data={"csrf": csrf, "hold_minutes": "0", "max_seconds": "60", "require_video": ""})
    assert r.status_code == 403 and "Owners only" in r.text
    r = await env.http.post("/office/users", data={"csrf": csrf, "email": "friend@example.com", "role": "staff"})
    assert r.status_code == 403
    r = await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "owner"})
    assert r.status_code == 403, "staff cannot make themselves an owner"
    r = await env.http.post("/office/users", data={"csrf": csrf, "email": OWNER, "role": "owner", "remove": "1"})
    assert r.status_code == 403

    after = (await env.http.get("/v1/today", headers=await login(env))).json()["config"]
    assert after == before, "no setting moved"
    users = {u.email: (u.role, u.active) for u in await env.all(sa.select(db.office_users))}
    assert users == {OWNER: ("owner", True), STAFF: ("staff", True)}
    for path in ("/office/settings", "/office/users", "/office/stats", "/office/audit"):
        assert (await env.http.get(path)).status_code == 403


async def test_staff_can_do_everything_the_brief_gives_them_and_it_is_logged_under_their_name(env: Env) -> None:
    headers = await ready(env)
    await film(env, headers, 502, override=True)  # raises a flag
    wrong = await film(env, headers, 501)
    loose = await film(env, headers, None)
    csrf = await staff_session(env)

    # See the day
    page = await env.http.get("/office")
    assert page.status_code == 200 and "Jane Smith" in page.text and "Pat Rivera" in page.text
    # Clear a flag with a note
    (flag,) = await env.flags("wrong_house")
    r = await env.http.post(f"/office/flag/{flag.id}/clear", data={"csrf": csrf, "note": "Spoke to Pat."})
    assert r.status_code == 303 and await env.flags("wrong_house") == []
    # Move a video to the right customer
    r = await env.http.post(f"/office/video/{loose}/move", data={"csrf": csrf, "customer_id": "503"})
    assert r.status_code == 303 and (await env.video(loose)).customer_id == 503
    # Pull a video
    r = await env.http.post(f"/office/video/{wrong}/pull", data={"csrf": csrf, "reason": "Blurry"})
    assert r.status_code == 303 and (await env.video(wrong)).state == "pulled"
    # Issue a setup code, refresh the tech list, cut off a phone
    r = await env.http.post("/office/techs/21/code", data={"csrf": csrf})
    assert r.status_code == 303 and re.search(r"code=\d{6}", r.headers["location"])
    before = len(env.fr_calls("employee", "search"))
    r = await env.http.post("/office/techs/refresh", data={"csrf": csrf})
    assert r.status_code == 303 and len(env.fr_calls("employee", "search")) == before + 1
    device = await env.one(sa.select(db.devices).where(db.devices.c.tech_id == 20))
    r = await env.http.post(f"/office/devices/{device.id}/cut", data={"csrf": csrf})
    assert r.status_code == 303
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 401, "that phone is out at once"

    events = await env.all(sa.select(db.events).where(db.events.c.actor == STAFF))
    assert {"flag_cleared", "video_moved", "video_pulled"} <= {e.action for e in events}, {e.action for e in events}


async def test_refreshing_techs_and_cutting_a_phone_need_a_session_too(env: Env) -> None:
    headers = await ready(env)
    device = await env.one(sa.select(db.devices).where(db.devices.c.tech_id == 20))
    env.http.cookies.clear()
    for path in ("/office/techs/refresh", f"/office/devices/{device.id}/cut", "/office/note"):
        r = await env.http.post(path, data={"csrf": "x", "day": today(), "key": "501:20", "note": "hi"})
        assert r.status_code == 303 and r.headers["location"] == "/office/login", path
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200, "the phone was not cut off"

    # With a session but a wrong form token, nothing happens either.
    await office_login(env)
    r = await env.http.post(f"/office/devices/{device.id}/cut", data={"csrf": "0" * 64})
    assert r.status_code in (400, 403)
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200


# == notes on a row (office page: "pull, move and note actions on each row") ============


async def test_the_office_can_leave_a_note_on_any_row_and_it_is_kept_with_who_and_when(env: Env) -> None:
    headers = await ready(env)
    loose = await film(env, headers, None)
    await env.finished(503, ago=timedelta(hours=2))
    csrf = await staff_session(env)

    # On a stop with no video at all...
    r = await env.http.post("/office/note", data={"csrf": csrf, "day": today(), "key": "503:20", "note": "Pat's phone died.  Refilm Monday."})
    assert r.status_code == 303 and "Note%20saved" in r.headers["location"]
    # ...and on a video that is not tied to a stop.
    await env.http.post("/office/note", data={"csrf": csrf, "day": today(), "key": f"v:{loose}", "note": "Looks like the Lee house."})
    page = (await env.http.get("/office")).text
    assert "Pat&#39;s phone died. Refilm Monday." in page and "Looks like the Lee house." in page
    assert page.count(STAFF) >= 2, "each note shows who wrote it"

    # A second note adds to the trail; the row shows the newest.
    await env.http.post("/office/note", data={"csrf": csrf, "day": today(), "key": "503:20", "note": "Refilmed."})
    page = (await env.http.get("/office")).text
    assert "Refilmed." in page and "phone died" not in page
    kept = [e.detail["note"] for e in await env.all(sa.select(db.events).where(db.events.c.action == "office_note"))]
    assert "Pat's phone died. Refilm Monday." in kept and "Refilmed." in kept, "nothing is overwritten"

    # Junk is refused, markup is shown as text, and a note for another day stays on that day.
    for bad in ({"key": "503:20", "note": "   "}, {"key": "../../etc", "note": "x"}, {"key": "", "note": "x"}):
        r = await env.http.post("/office/note", data={"csrf": csrf, "day": today(), **bad})
        assert "err=" in r.headers["location"]
    await env.http.post("/office/note", data={"csrf": csrf, "day": today(), "key": "501:20", "note": "<script>alert(1)</script>"})
    page = (await env.http.get("/office")).text
    assert "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page
    await env.http.post("/office/note", data={"csrf": csrf, "day": "2026-01-05", "key": "502:20", "note": "Old day only."})
    assert "Old day only." not in (await env.http.get("/office")).text


# == lower risk ========================================================================


async def test_the_audit_trail_has_a_row_for_every_kind_of_thing_the_brief_lists(env: Env) -> None:
    headers = await ready(env)
    first = await film(env, headers, 501)
    second = await film(env, headers, 501)  # replaces the first
    gone = await film(env, headers, 502, override=True)
    await env.http.delete(f"/v1/videos/{gone}", headers=headers)
    await send(env, second)
    video = await env.video(second)
    await env.http.get(f"/w/{video.token}")
    await env.http.post(f"/w/{video.token}/played")
    await env.http.post(f"/w/{video.token}/score", data={"stars": "5"})
    loose = await film(env, headers, None)
    csrf = await office_login(env)
    (flag, *_) = await env.flags()
    await env.http.post(f"/office/flag/{flag.id}/clear", data={"csrf": csrf, "note": "ok"})
    await env.http.post(f"/office/video/{loose}/move", data={"csrf": csrf, "customer_id": "503"})
    await env.http.post(f"/office/video/{loose}/pull", data={"csrf": csrf, "reason": "test"})
    await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff"})
    await env.http.post("/office/settings", data={"csrf": csrf, "hold_minutes": "12"})
    await env.http.post("/office/techs/21/code", data={"csrf": csrf})

    rows = await env.all(sa.select(db.events).order_by(db.events.c.id))
    actions = {e.action for e in rows}
    wanted = {
        "login",  # tech and office sign-in
        "video_registered", "video_uploaded",  # record, upload
        "video_replaced", "video_deleted",  # the tech's own undo
        "email_sent", "video_played", "score_saved",  # delivery, view, feedback
        "video_pulled", "video_moved", "flag_cleared",  # office actions
        "office_user_changed", "setting_changed", "code_issued",  # admin
    }
    assert wanted <= actions, sorted(wanted - actions)
    assert all(e.at is not None and e.actor for e in rows), "every row says who and when"
    by_action = {e.action: e for e in rows}
    assert by_action["video_pulled"].actor == OWNER and by_action["video_played"].video_id == second
    assert by_action["video_replaced"].video_id == first and by_action["video_deleted"].video_id == gone


async def test_owner_settings_change_what_actually_happens_not_just_what_is_saved(env: Env) -> None:
    headers = await ready(env)
    # Email wording
    await set_setting(env.ctx, "email_subject", "{tech} filmed your {service}", OWNER)
    await set_setting(env.ctx, "email_intro", "Hello from {tech} at {address}. Service: {service}.", OWNER)
    await send(env, await film(env, headers, 501))
    (email,) = env.mailer.sent
    assert email.subject == "Pat filmed your Bi-Weekly Yard Maintenance and Leaf Pick-up Service"
    assert "Hello from Pat at 123 Main St. Service: Bi-Weekly Yard Maintenance and Leaf Pick-up Service." in email.text
    assert "{" not in email.subject and "{tech}" not in email.html and "{service}" not in email.html

    # Alert timing: a stop finished 45 minutes ago is quiet at the default hour, flagged at 30 minutes.
    await env.finished(502, 503, ago=timedelta(minutes=45))
    assert await jobs.flag_missing(env.ctx) == 0
    await set_setting(env.ctx, "missing_alert_minutes", 30, OWNER)
    assert await jobs.flag_missing(env.ctx) == 2

    # Retention: a 40-day-old video survives 12 months, and is cleared at 1 month.
    old = await film(env, headers, 502)
    await env.run(sa.update(db.videos).where(db.videos.c.id == old).values(created_at=db.now() - timedelta(days=40)))
    await jobs.daily(env.ctx)
    assert (await env.video(old)).object_deleted is False
    await set_setting(env.ctx, "retention_months", 1, OWNER)
    await jobs.daily(env.ctx)
    kept = await env.video(old)
    assert kept.object_deleted is True and kept.object_key in env.storage.deleted


async def test_the_owners_latest_decisions_are_the_servers_defaults(env: Env) -> None:
    assert DEFAULT_SETTINGS["prompts"] == ["Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate"]
    headers = await login(env)
    config = (await env.http.get("/v1/today", headers=headers)).json()["config"]
    assert config["prompts"] == DEFAULT_SETTINGS["prompts"], "the name only; nothing asks for the street"
    assert config["maxSeconds"] == 15 and config["holdMinutes"] == 10
    assert config["leaveReminders"] is False, "While Using location only in version one"
    assert config["farMeters"] == 150 and config["requireVideo"] is True and config["recommendButton"] is True
    assert DEFAULT_SETTINGS["retention_months"] == 12 and DEFAULT_SETTINGS["stuck_hours"] == 3
    assert env.ctx.cfg.email_mode in ("off", "test", "live")
    from zest_video import config as config_module

    assert config_module.load().email_mode == "off", "customer email stays off until the owner turns it on"


async def test_customers_are_never_texted_and_no_file_is_pushed_into_fieldroutes(env: Env) -> None:
    headers = await login(env)  # the only texts ever sent are login codes to techs
    await env.http.get("/v1/today", headers=headers)
    texts_at_login = list(env.texter.sent)
    video_id = await film(env, headers, 501, recommend=True)
    await send(env, video_id)
    video = await env.video(video_id)
    await env.http.post(f"/w/{video.token}/score", data={"stars": "2", "comment": "Missed a spot"})
    await videos.run_outbox(env.ctx)

    assert env.texter.sent == texts_at_login, "delivery is one email; nothing is texted to a customer"
    assert all(to == "9165550101" for to, _ in env.texter.sent), "and codes go to the tech's own number only"
    writes = {(r.entity, r.action) for r in env.fake.requests if r.action not in ("search", "get")}
    assert writes <= {("note", "create"), ("review", "create"), ("task", "create")}, writes
    assert not any(r.entity.startswith("document") for r in env.fake.requests), "just a link, never the video file"
    (note,) = [r for r in env.fake.requests if (r.entity, r.action) == ("note", "create")]
    assert f"/w/{video.token}" in (note.one("notes") or "")


async def test_the_watch_page_leads_with_the_logo_the_tech_the_address_and_the_date(env: Env) -> None:
    headers = await ready(env)
    video_id = await film(env, headers, 501)
    await send(env, video_id)
    video = await env.video(video_id)
    page = (await env.http.get(f"/w/{video.token}")).text
    top = page[page.index('<header class="top">'): page.index("</header>")]
    assert "/static/logo.png" in top and 'alt="Zest Lawn &amp; Pest"' in top
    assert "A video from Pat at 123 Main St" in top
    assert env.ctx.local(video.recorded_at).strftime("%A, %B %-d") in top
    assert page.index("</header>") < page.index("<video"), "nothing between the heading and the player to scroll past"
    assert page.index("<video") < page.index('name="stars"') < page.index("tel:"), "player, then score, then the office button"
    assert (await env.http.get("/static/logo.png")).status_code == 200


# == seen on a real week ===============================================================


async def test_a_stop_with_the_main_job_done_and_an_add_on_left_open_still_owes_a_video(env: Env) -> None:
    """On a real week about one stop in ten had its main appointment completed and an
    add-on (a leaf pick-up with no tech of its own) left pending for days. The tech has
    been and worked, so the stop is finished for him: his phone says so, and a missing
    video is chased, without waiting for the add-on that may never be closed."""
    from zest_video.http_office import board, stats

    env.fake.data["appointment"][9004].update(status="1", checkIn=minutes_ago(120), checkOut=minutes_ago(100))
    env.fake.data["appointment"][9011] = {
        "appointmentID": "9011", "officeID": "7", "customerID": "502", "routeID": "70", "type": "95", "status": "0",
        "assignedTech": "0", "servicedBy": "0", "employeeID": "22", "date": today(), "start": "09:00:00", "sequence": "2",
    }
    headers = await ready(env)
    bob = next(s for s in (await env.http.get("/v1/today", headers=headers)).json()["stops"] if s["customerId"] == 502)
    assert bob["appointmentIds"] == [9004, 9011] and bob["done"] is True

    await env.finished(502, ago=timedelta(hours=2), force=False)
    # Mowing (89) switched off for videos: the only thing he did there needs none.
    await set_setting(env.ctx, "skip_service_ids", "89", OWNER)
    assert await jobs.flag_missing(env.ctx) == 0
    await set_setting(env.ctx, "skip_service_ids", "999", OWNER)
    assert await jobs.flag_missing(env.ctx) == 1
    (flag,) = await env.flags("no_video")
    assert (flag.customer_id, flag.tech_id) == (502, 20)

    rows = {r["stop"].customer_id: r for _, group in (await board(env.ctx, today()))["groups"] for r in group}
    assert rows[502]["label"] == "No video", "the office sees it as finished with no video, not as 'not done yet'"
    pat = next(r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera")
    assert pat["missed"] >= 1

    # He films it: the video carries both appointments, and the flag goes.
    video_id = await film(env, headers, 502)
    assert (await env.video(video_id)).appointment_ids == [9004, 9011]
    assert await env.flags("no_video") == []
