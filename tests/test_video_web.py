"""The browser-facing half: the customer's watch page and the office page."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time

import pytest
import sqlalchemy as sa

from video_helpers import OWNER, STAFF, Env, code_from, film, login, make_env, office_login, video_body

from zest_video import auth, db, videos
from zest_video.http_web import verify_svix


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


async def sent_video(env: Env, customer_id: int = 501):
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, customer_id)
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    return await env.video(video_id), headers


# -- the customer's page ---------------------------------------------------------


async def test_watch_page_plays_the_video_with_no_login_and_tells_search_engines_nothing(env: Env) -> None:
    video, _ = await sent_video(env)
    r = await env.http.get(f"/w/{video.token}")
    assert r.status_code == 200
    assert "A video from Pat" in r.text and "at 123 Main St" in r.text
    assert f'src="/w/{video.token}/video#t=0.1"' in r.text and "playsinline" in r.text and "controls" in r.text
    assert "How did we do?" in r.text and 'href="tel:+19165550100"' in r.text
    assert "This is not my home" in r.text

    assert r.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    assert r.headers["cache-control"] == "no-store" and r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["strict-transport-security"].startswith("max-age=")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert '<meta name="robots" content="noindex' in r.text

    # Nothing about the customer beyond what they already know: no email, phone or ID.
    for private in ("jane@example.com", "9165550001", ">501<", video.id):
        assert private not in r.text


async def test_the_player_gets_a_fresh_signed_url_and_the_bucket_stays_private(env: Env) -> None:
    video, _ = await sent_video(env)
    r = await env.http.get(f"/w/{video.token}/video")
    assert r.status_code == 302
    assert r.headers["location"] == f"https://bucket.invalid/{video.object_key}?get=1&expires=900"
    assert r.headers["cache-control"] == "no-store"


async def test_watched_means_play_was_pressed_not_that_the_link_was_opened(env: Env) -> None:
    video, headers = await sent_video(env)
    await env.http.get(f"/w/{video.token}")  # a mail scanner opening the link
    assert (await env.video(video.id)).first_viewed_at is None

    assert (await env.http.post(f"/w/{video.token}/played")).status_code == 204
    await env.http.post(f"/w/{video.token}/played")
    after = await env.video(video.id)
    assert after.first_viewed_at is not None and after.view_count == 2
    stop = (await env.http.get("/v1/today", headers=headers)).json()["stops"][0]
    assert stop["video"]["state"] == "watched"


async def test_a_score_saves_once_to_fieldroutes_and_a_low_one_flags_the_office(env: Env) -> None:
    video, _ = await sent_video(env)
    r = await env.http.post(f"/w/{video.token}/score", data={"stars": "2", "comment": "  Missed the side yard.  "})
    assert r.status_code == 303 and r.headers["location"] == f"/w/{video.token}?thanks=1#score"
    page = await env.http.get(f"/w/{video.token}?thanks=1")
    assert "Thank you! You gave us 2 of 5 stars." in page.text and "How did we do?" not in page.text

    await videos.run_outbox(env.ctx)
    (review,) = env.fr_calls("review", "create")
    assert review.one("customerID") == "501" and review.one("appointmentID") == "9001"
    assert review.one("starRating") == "2" and review.one("feedback") == "Missed the side yard."
    assert re.fullmatch(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", review.one("time"))
    (flag,) = await env.flags("low_score")
    assert "2 of 5 stars" in flag.detail and "Missed the side yard." in flag.detail

    # Changing their mind updates our copy; FieldRoutes is not sent a second review.
    await env.http.post(f"/w/{video.token}/score", data={"stars": "5", "comment": ""})
    await videos.run_outbox(env.ctx)
    assert (await videos.get_score(env.ctx, video.id)).stars == 5
    assert len(env.fr_calls("review", "create")) == 1


@pytest.mark.parametrize("stars", ["0", "6", "", "five"])
async def test_a_bad_score_is_ignored(env: Env, stars: str) -> None:
    video, _ = await sent_video(env)
    r = await env.http.post(f"/w/{video.token}/score", data={"stars": stars})
    assert r.status_code == 303 and await videos.get_score(env.ctx, video.id) is None


async def test_not_my_home_kills_the_link_and_tells_the_office(env: Env) -> None:
    video, _ = await sent_video(env)
    r = await env.http.post(f"/w/{video.token}/not-my-home")
    assert r.status_code == 200 and "We took this video down" in r.text

    dead = await env.http.get(f"/w/{video.token}")
    assert dead.status_code == 410 and "This video is no longer available." in dead.text
    assert (await env.http.get(f"/w/{video.token}/video")).status_code == 410
    after = await env.video(video.id)
    assert after.state == "pulled" and after.object_key in env.storage.objects, "kept so the office can move it"
    (flag,) = await env.flags("not_my_home")
    assert flag.video_id == video.id and "Jane Smith" in flag.detail


async def test_unknown_and_malformed_links_get_the_same_dead_page(env: Env) -> None:
    for token in ("0" * 32, "abc", "Z" * 32, "0" * 31 + "'"):
        r = await env.http.get(f"/w/{token}")
        assert r.status_code == 404 and "no longer available" in r.text
    # A path that is not a link at all is a plain not-found, with nothing leaked.
    r = await env.http.get("/w/../../etc/passwd")
    assert r.status_code == 404 and "root:" not in r.text


async def test_guessing_links_gets_slow(env: Env) -> None:
    codes = [(await env.http.get(f"/w/{i:032x}")).status_code for i in range(22)]
    assert codes[:20] == [404] * 20 and codes[20:] == [429, 429]


async def test_a_video_still_inside_the_window_can_be_previewed_but_a_waiting_one_cannot(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    held = await env.video(await film(env, headers, 501))
    assert (await env.http.get(f"/w/{held.token}")).status_code == 200
    body = video_body(502)
    await env.http.post("/v1/videos", json=body, headers=headers)
    waiting = await env.video(body["id"])
    assert (await env.http.get(f"/w/{waiting.token}")).status_code == 410


async def test_public_pages_exist_for_the_app_store_listing(env: Env) -> None:
    for path, words in (
        ("/privacy", "What the app collects"),
        ("/terms", "Terms of Use"),
        ("/support", "No code arrived?"),
        ("/", "Service videos"),
    ):
        r = await env.http.get(path)
        assert r.status_code == 200 and words in r.text
        assert 'href="/privacy"' in r.text and 'href="/terms"' in r.text, "legal links on every public page"
    privacy = (await env.http.get("/privacy")).text
    assert "deleted after 12 months" in privacy and "deleting your data" in privacy
    assert "We do not sell this information" in privacy
    assert (await env.http.get("/healthz")).text == "ok"
    assert (await env.http.get("/app")).headers["location"] == "/support", "until the App Store page exists"
    static = await env.http.get("/static/rubik.woff2")
    assert "max-age=2592000" in static.headers["cache-control"]
    assert "Disallow: /" in (await env.http.get("/robots.txt")).text
    assert (await env.http.get("/static/logo.png")).headers["content-type"] == "image/png"


async def test_plain_http_is_bounced_to_https(env: Env) -> None:
    r = await env.http.get("/privacy?x=1", headers={"x-forwarded-proto": "http"})
    assert r.status_code == 308 and r.headers["location"] == "https://video.test/privacy?x=1"
    assert (await env.http.get("/healthz", headers={"x-forwarded-proto": "http"})).status_code == 200


# -- bounce webhook --------------------------------------------------------------


def _svix(secret: str, body: bytes, *, stamp: int | None = None) -> dict[str, str]:
    stamp = stamp or int(time.time())
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": "msg_1", "svix-timestamp": str(stamp), "svix-signature": f"v1,bogus v1,{sig}"}


async def test_a_bounce_from_the_mail_provider_flags_the_office_only_when_signed(env: Env) -> None:
    video, _ = await sent_video(env)
    body = json.dumps({"type": "email.bounced", "data": {"email_id": video.email_id}}).encode()
    secret = env.ctx.cfg.resend_webhook_secret

    assert (await env.http.post("/hooks/resend", content=body)).status_code == 401
    forged = {**_svix(secret, body), "svix-signature": "v1,AAAA"}
    assert (await env.http.post("/hooks/resend", content=body, headers=forged)).status_code == 401
    stale = _svix(secret, body, stamp=int(time.time()) - 3600)
    assert (await env.http.post("/hooks/resend", content=body, headers=stale)).status_code == 401
    assert await env.flags("email_bounced") == []

    assert (await env.http.post("/hooks/resend", content=body, headers=_svix(secret, body))).status_code == 200
    (flag,) = await env.flags("email_bounced")
    assert flag.video_id == video.id and "bounced" in flag.detail
    assert verify_svix("", {}, body) is False, "no secret configured means nothing is trusted"


# -- the office page -------------------------------------------------------------


async def test_office_sign_in_is_an_emailed_code_for_listed_addresses_only(env: Env) -> None:
    assert (await env.http.get("/office")).status_code == 303
    r = await env.http.post("/office/login", data={"email": "stranger@example.com"})
    assert r.status_code == 200 and "we just emailed it a 6-digit code" in r.text, "same answer either way"
    assert env.mailer.sent == []

    await env.http.post("/office/login", data={"email": OWNER})
    code = code_from(env.mailer.sent[-1].text)
    bad = await env.http.post("/office/verify", data={"email": OWNER, "code": "000000" if code != "000000" else "111111"})
    assert bad.status_code == 401 and "not right" in bad.text
    ok = await env.http.post("/office/verify", data={"email": OWNER, "code": code})
    assert ok.status_code == 303
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "path=/office" in cookie
    session = await env.one(sa.select(db.office_sessions))
    assert session.email == OWNER and len(session.token_hash) == 64

    page = await env.http.get("/office")
    assert page.status_code == 200 and "Sign out" in page.text and OWNER in page.text


async def test_the_day_page_shows_every_stop_by_tech_with_its_video_status(env: Env) -> None:
    pat = await login(env)
    await env.http.get("/v1/today", headers=pat)
    sent_id = await film(env, pat, 501)
    await env.window_closed(sent_id)
    await videos.send_due(env.ctx)
    await env.http.post(f"/w/{(await env.video(sent_id)).token}/played")
    await env.http.post("/v1/videos", json=video_body(502), headers=pat)  # registered, never uploaded
    await film(env, pat, None)  # not on my list
    await env.finished(503)

    await office_login(env)
    text = (await env.http.get("/office")).text
    assert "Pat Rivera" in text and "Sam Ortiz" in text
    for name in ("Jane Smith", "Bob Jones", "Acme Corp", "Ann Lee"):
        assert name in text
    assert '<span class="pill good">Watched</span>' in text
    assert '<span class="pill wait">Waiting on phone</span>' in text
    assert '<span class="pill bad">No video</span>' in text
    assert '<span class="pill off">Tech has no app yet</span>' in text, "Sam has not signed in on a phone"
    assert "Videos not tied to a stop" in text and '<span class="pill bad">Needs a customer</span>' in text
    assert "Not on the list: needs a customer" in text, "the flag is on top"
    for private in ("jane@example.com", "9165550001"):
        assert private not in text, "the office page shows no customer contact details either"

    # Once Sam has the app, his unfinished stop reads as plain "not done yet".
    await login(env, "sam@example.com")
    text = (await env.http.get("/office")).text
    assert '<span class="pill off">Not done yet</span>' in text and "Tech has no app yet" not in text


async def test_customer_emails_off_is_shouted_at_the_top_of_the_office_page(tmp_path) -> None:
    env = await make_env(tmp_path, email_mode="off")
    try:
        await office_login(env)
        assert "Customer emails are OFF" in (await env.http.get("/office")).text
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_pulling_a_video_kills_its_link_and_moving_it_emails_the_right_customer(env: Env) -> None:
    video, _ = await sent_video(env, 501)
    csrf = await office_login(env)

    r = await env.http.post(f"/office/video/{video.id}/pull", data={"csrf": csrf, "reason": "wrong house"})
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    assert (await env.http.get(f"/w/{video.token}")).status_code == 410
    pulled = await env.video(video.id)
    assert (pulled.state, pulled.ended_by, pulled.ended_reason) == ("pulled", OWNER, "wrong house")

    r = await env.http.post(f"/office/video/{video.id}/move", data={"csrf": csrf, "customer_id": "502"})
    assert r.status_code == 303
    moved = await env.video(video.id)
    assert moved.state == "held" and moved.customer_id == 502 and moved.customer_name == "Bob Jones"
    assert moved.appointment_ids == [9004] and moved.token != video.token
    await videos.send_due(env.ctx)
    assert env.mailer.sent[-1].to == "bob@example.com" and moved.token in env.mailer.sent[-1].html
    assert (await env.http.get(f"/w/{video.token}")).status_code == 404, "the first customer's link is gone for good"

    actions = await env.actions()
    assert "video_pulled" in actions and "video_moved" in actions


async def test_a_video_cannot_be_moved_onto_a_customer_who_already_has_one(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    first, second = await film(env, headers, 501), await film(env, headers, 502)
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{first}/move", data={"csrf": csrf, "customer_id": "502"})
    assert r.status_code == 303 and "err=" in r.headers["location"] and "already%20has%20a%20video" in r.headers["location"]
    assert (await env.video(first)).customer_id == 501 and (await env.video(second)).customer_id == 502
    r = await env.http.post(f"/office/video/{first}/move", data={"csrf": csrf, "customer_id_other": "99999"})
    assert "no%20customer%2099999" in r.headers["location"]


async def test_every_office_form_needs_the_csrf_token(env: Env) -> None:
    video, _ = await sent_video(env)
    await office_login(env)
    for data in ({}, {"csrf": "0" * 64}):
        r = await env.http.post(f"/office/video/{video.id}/pull", data=data)
        assert r.status_code == 400 and "stale" in r.text
    assert (await env.video(video.id)).state == "sent"


async def test_staff_can_work_the_day_but_only_owners_reach_settings_stats_users_and_audit(env: Env) -> None:
    csrf = await office_login(env)
    r = await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff"})
    assert r.status_code == 303
    await env.http.post("/office/logout", data={"csrf": csrf})
    assert (await env.http.get("/office")).status_code == 303

    await office_login(env, STAFF)
    assert (await env.http.get("/office")).status_code == 200
    assert (await env.http.get("/office/techs")).status_code == 200
    for path in ("/office/settings", "/office/stats", "/office/users", "/office/audit"):
        r = await env.http.get(path)
        assert r.status_code == 403 and "Owners only" in r.text
    assert "Settings" not in (await env.http.get("/office")).text


async def test_removing_an_office_user_signs_them_out_and_owners_set_on_the_server_are_fixed(env: Env) -> None:
    csrf = await office_login(env)
    await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff"})
    await auth.verify_office_code(env.ctx, STAFF, await auth._new_code(env.ctx, "office", STAFF), "ip")
    assert len(await env.all(sa.select(db.office_sessions).where(db.office_sessions.c.email == STAFF))) == 1

    await env.http.post("/office/users", data={"csrf": csrf, "email": STAFF, "role": "staff", "remove": "1"})
    assert await env.all(sa.select(db.office_sessions).where(db.office_sessions.c.email == STAFF)) == []
    await env.http.post("/office/login", data={"email": STAFF})
    assert env.mailer.sent[-1].to != STAFF, "a removed user gets no code"

    r = await env.http.post("/office/users", data={"csrf": csrf, "email": OWNER, "role": "staff"})
    assert "err=" in r.headers["location"]
    assert (await env.one(sa.select(db.office_users).where(db.office_users.c.email == OWNER))).role == "owner"


async def test_settings_change_what_the_phone_is_told_and_are_audited(env: Env) -> None:
    headers = await login(env)
    csrf = await office_login(env)
    page = await env.http.get("/office/settings")
    assert page.status_code == 200 and 'name="max_seconds" value="15"' in page.text

    form = {
        "csrf": csrf, "max_seconds": "20", "hold_minutes": "5", "far_meters": "150", "min_build": "1",
        "missing_alert_minutes": "60", "stuck_hours": "3", "retention_months": "12", "skip_service_ids": "89 95",
        "email_subject": "A video from {tech}", "email_intro": "Hi from {tech} at {address}.",
        "prompts": "Say the name\nWhat I did\n\nWhat I saw", "require_video": "on",
    }
    r = await env.http.post("/office/settings", data=form)
    assert r.status_code == 303 and "msg=Saved." in r.headers["location"]
    config = (await env.http.get("/v1/today", headers=headers)).json()["config"]
    assert config["maxSeconds"] == 20 and config["holdMinutes"] == 5
    assert config["prompts"] == ["Say the name", "What I did", "What I saw"]
    assert config["requireVideo"] is True and config["recommendButton"] is False, "an unticked box is off"

    changed = {r.detail["key"]: (r.detail["from"], r.detail["to"])
               for r in await env.all(sa.select(db.events).where(db.events.c.action == "setting_changed"))}
    assert changed["max_seconds"] == (15, 20) and changed["skip_service_ids"] == ([], [89, 95])
    assert "far_meters" not in changed, "unchanged settings leave no audit noise"

    bad = await env.http.post("/office/settings", data={**form, "max_seconds": "9999"})
    assert "err=" in bad.headers["location"] and "between%205%20and%2060" in bad.headers["location"]


async def test_office_can_read_out_a_code_and_cut_off_a_phone(env: Env) -> None:
    pat = await login(env)
    csrf = await office_login(env)
    page = await env.http.get("/office/techs")
    assert "Pat Rivera" in page.text and "p***@example.com" in page.text and "•••-•••-0101" in page.text
    assert "pat.rivera@example.com" not in page.text and "Olive" not in page.text, "techs only, contacts masked"

    r = await env.http.post("/office/techs/21/code", data={"csrf": csrf})
    code = re.search(r"code=(\d{6})", r.headers["location"]).group(1)
    assert "Sam%20Ortiz" in r.headers["location"]
    r = await env.http.post("/v1/auth/verify", json={"contact": "sam@example.com", "code": code})
    assert r.status_code == 200 and r.json()["tech"]["firstName"] == "Sam"

    device = await env.one(sa.select(db.devices).where(db.devices.c.tech_id == 20))
    r = await env.http.post(f"/office/devices/{device.id}/cut", data={"csrf": csrf})
    assert r.status_code == 303
    assert (await env.http.get("/v1/today", headers=pat)).status_code == 401
    assert "device_cut_off" in await env.actions() and "code_issued" in await env.actions()

    inactive = await env.http.post("/office/techs/23/code", data={"csrf": csrf})
    assert "err=" in inactive.headers["location"]


async def test_clearing_a_flag_keeps_the_note_and_who_did_it(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    await film(env, headers, 501, override=True)
    (flag,) = await env.flags("wrong_house")
    csrf = await office_login(env)
    r = await env.http.post(f"/office/flag/{flag.id}/clear", data={"csrf": csrf, "note": "Checked, right house."})
    assert r.status_code == 303
    assert await env.flags() == []
    (cleared,) = await env.flags(open_only=False)
    assert (cleared.cleared_by, cleared.note) == (OWNER, "Checked, right house.")


async def test_stats_count_videos_missed_stops_watch_rate_and_score_per_tech(env: Env) -> None:
    video, pat = await sent_video(env, 501)
    await env.http.post(f"/w/{video.token}/played")
    await env.http.post(f"/w/{video.token}/score", data={"stars": "4"})
    await env.finished(502, 503)
    second = await film(env, pat, 502)
    await env.window_closed(second)
    await videos.send_due(env.ctx)

    await office_login(env)
    from zest_video.http_office import stats

    (row,) = [r for r in await stats(env.ctx, 30) if r["name"] == "Pat Rivera"]
    assert (row["stops"], row["videos"], row["missed"], row["sent"], row["watched"]) == (3, 2, 1, 2, 1)
    assert row["watch_rate"] == 50 and row["avg_stars"] == 4.0
    page = await env.http.get("/office/stats?days=7")
    assert page.status_code == 200 and "Pat Rivera" in page.text and "50%" in page.text and "4.0★" in page.text


async def test_the_audit_trail_shows_who_did_what_and_filters_by_video(env: Env) -> None:
    video, _ = await sent_video(env)
    csrf = await office_login(env)
    await env.http.post(f"/office/video/{video.id}/pull", data={"csrf": csrf})
    page = await env.http.get("/office/audit")
    for action in ("login", "video_registered", "video_uploaded", "email_sent", "video_pulled"):
        assert f"<td>{action}</td>" in page.text
    assert "Pat Rivera" in page.text and OWNER in page.text
    assert "jane@example.com" not in page.text and "j***@example.com" in page.text, "addresses are masked in the trail"
    one = await env.http.get(f"/office/audit?video={video.id}&action=video_pulled")
    assert one.text.count("<td>video_pulled</td>") == 1 and "<td>login</td>" not in one.text


async def test_the_office_can_watch_any_video_through_a_signed_url(env: Env) -> None:
    video, _ = await sent_video(env)
    await office_login(env)
    r = await env.http.get(f"/office/video/{video.id}")
    assert r.status_code == 302 and r.headers["location"].startswith(f"https://bucket.invalid/{video.object_key}?get=1")
    assert "video_opened" in await env.actions()
    assert (await env.http.get("/office/video/nope")).status_code == 404


async def test_redirects_after_a_form_stay_inside_the_office_page(env: Env) -> None:
    video, _ = await sent_video(env)
    csrf = await office_login(env)
    r = await env.http.post(
        f"/office/video/{video.id}/pull", data={"csrf": csrf, "back": "https://evil.example/office?day=2026-11-01"}
    )
    assert r.headers["location"].startswith("/office?day=2026-11-01&msg="), "path and day only, never another host"
    r = await env.http.post("/office/flag/999/clear", data={"csrf": csrf, "back": "//evil.example"})
    assert r.headers["location"].startswith("/office?msg=")


async def test_an_old_day_can_be_opened_and_refreshed(env: Env) -> None:
    await office_login(env)
    r = await env.http.get("/office?day=2026-01-05&refresh=1")
    assert r.status_code == 200 and "Cy Young" in r.text and "Monday, January 5" in r.text
    assert (await env.http.get("/office?day=not-a-day")).status_code == 200
