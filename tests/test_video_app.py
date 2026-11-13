"""The phone-facing half of the Zest Visit Video server: login, today's stops, and the
life of a video from register to the customer's inbox."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa

from video_helpers import (
    DEMO_CODE, DEMO_CONTACT, Env, code_from, demo_login, film, login, make_env, today, video_body,
)

from zest_video import auth, db, jobs, videos
from zest_video.http_api import client_ip
from zest_video.storage import presign
from zest_video.videos import email_due


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


# -- storage signing -----------------------------------------------------------


def test_presign_matches_amazons_published_example() -> None:
    url = presign(
        method="GET", endpoint="https://s3.amazonaws.com", bucket="examplebucket", key="test.txt",
        region="us-east-1", access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", seconds=86400,
        now=datetime(2013, 5, 24, tzinfo=timezone.utc),
    )
    assert url.startswith("https://examplebucket.s3.amazonaws.com/test.txt?")
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")


def test_presigned_put_binds_the_content_type_and_supports_path_style() -> None:
    common = dict(endpoint="https://t3.storageapi.dev", bucket="b", key="videos/2026-11-13/a b.mp4", region="auto",
                  access_key="k", secret_key="s", seconds=60, now=datetime(2026, 11, 13, tzinfo=timezone.utc))
    put = presign(method="PUT", signed_headers={"content-type": "video/mp4"}, **common)
    assert put.startswith("https://b.t3.storageapi.dev/videos/2026-11-13/a%20b.mp4?")
    assert "X-Amz-SignedHeaders=content-type%3Bhost" in put
    other = presign(method="PUT", signed_headers={"content-type": "text/html"}, **common)
    assert put.rsplit("=", 1)[1] != other.rsplit("=", 1)[1], "a different content type must not share a signature"
    path = presign(method="GET", path_style=True, **common)
    assert path.startswith("https://t3.storageapi.dev/b/videos/2026-11-13/a%20b.mp4?")


# -- login ---------------------------------------------------------------------


async def test_code_goes_to_both_phone_and_email_and_signs_the_tech_in(env: Env) -> None:
    r = await env.http.post("/v1/auth/request-code", json={"contact": "(916) 555-0101"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert [to for to, _ in env.texter.sent] == ["9165550101"]
    assert [m.to for m in env.mailer.sent] == ["pat.rivera@example.com"]
    code = code_from(env.mailer.sent[0].text)
    assert code in env.texter.sent[0][1]

    r = await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": code, "appBuild": 1})
    assert r.status_code == 200
    body = r.json()
    assert body["tech"] == {"id": 20, "firstName": "Pat", "demo": False}

    # The token is stored only as a hash.
    device = await env.one(sa.select(db.devices))
    assert body["token"] not in (device.token_hash, device.id) and len(device.token_hash) == 64

    # The code works once.
    again = await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": code})
    assert again.status_code == 401 and again.json()["error"] == "bad_code"


async def test_email_also_works_as_the_contact_and_is_case_blind(env: Env) -> None:
    headers = await login(env, "SAM@Example.com")
    r = await env.http.get("/v1/today", headers=headers)
    assert r.status_code == 200 and r.json()["tech"]["firstName"] == "Sam"
    assert env.texter.sent == [], "Sam has no phone on file, so nothing is texted"


@pytest.mark.parametrize(
    "contact",
    ["9165550199", "olive@example.com", "9165550123", "gone@example.com", "nobody@example.com", "9165559999"],
)
async def test_only_active_technicians_get_a_code_and_the_answer_never_says_so(env: Env, contact: str) -> None:
    r = await env.http.post("/v1/auth/request-code", json={"contact": contact})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert env.mailer.sent == [] and env.texter.sent == []
    r = await env.http.post("/v1/auth/verify", json={"contact": contact, "code": "123456"})
    assert r.status_code == 401


async def test_garbage_contact_gets_plain_words(env: Env) -> None:
    r = await env.http.post("/v1/auth/request-code", json={"contact": "12345"})
    assert r.status_code == 400 and "mobile number" in r.json()["message"]


async def test_five_wrong_codes_lock_the_code(env: Env) -> None:
    await env.http.post("/v1/auth/request-code", json={"contact": "9165550101"})
    code = code_from(env.mailer.sent[0].text)
    wrong = "000000" if code != "000000" else "111111"
    left = []
    for _ in range(4):
        r = await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": wrong})
        assert r.status_code == 401
        left.append(r.json()["triesLeft"])
    assert left == [4, 3, 2, 1]
    r = await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": wrong})
    assert r.status_code == 423 and r.json()["error"] == "locked"
    # Even the right code is dead now.
    r = await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": code})
    assert r.status_code == 423


async def test_code_dies_after_15_minutes_and_a_new_request_retires_the_old_code(env: Env) -> None:
    await env.http.post("/v1/auth/request-code", json={"contact": "9165550101"})
    first = code_from(env.mailer.sent[0].text)
    await env.http.post("/v1/auth/request-code", json={"contact": "9165550101"})
    second = code_from(env.mailer.sent[1].text)
    if first != second:
        r = await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": first})
        assert r.status_code == 401, "only the newest code counts"
    await env.run(sa.update(db.login_codes).values(expires_at=db.now() - timedelta(seconds=1)))
    r = await env.http.post("/v1/auth/verify", json={"contact": "9165550101", "code": second})
    assert r.status_code == 401


async def test_code_requests_are_rate_limited_per_contact(env: Env) -> None:
    for _ in range(5):
        assert (await env.http.post("/v1/auth/request-code", json={"contact": "9165550101"})).status_code == 200
    r = await env.http.post("/v1/auth/request-code", json={"contact": "9165550101"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


async def test_a_tech_added_in_fieldroutes_this_morning_can_log_in_without_the_office(env: Env) -> None:
    await auth.sync_techs(env.ctx)
    env.ctx.last_tech_sync -= 10_000  # the last look was a while ago
    env.fake.data["employee"][30] = {"employeeID": "30", "officeID": "7", "fname": "New", "lname": "Hire",
                                     "type": "1", "active": "1", "email": "new@example.com", "phone": ""}
    headers = await login(env, "new@example.com")
    assert (await env.http.get("/v1/today", headers=headers)).json()["tech"]["firstName"] == "New"


async def test_inactive_in_fieldroutes_locks_the_app_and_logout_cuts_the_phone(env: Env) -> None:
    pat = await login(env)
    sam = await login(env, "sam@example.com")
    env.fake.data["employee"][20]["active"] = "0"
    await auth.sync_techs(env.ctx)
    r = await env.http.get("/v1/today", headers=pat)
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"
    assert (await env.http.get("/v1/today", headers=sam)).status_code == 200

    assert (await env.http.post("/v1/auth/logout", headers=sam)).status_code == 200
    assert (await env.http.get("/v1/today", headers=sam)).status_code == 401


async def test_no_token_and_bad_token_are_refused(env: Env) -> None:
    assert (await env.http.get("/v1/today")).status_code == 401
    assert (await env.http.get("/v1/today", headers={"Authorization": "Bearer nope"})).status_code == 401


async def test_an_old_app_build_is_told_to_update(env: Env) -> None:
    headers = await login(env)
    from zest_video.core import set_setting

    await set_setting(env.ctx, "min_build", 5, "owner@zest.test")
    r = await env.http.get("/v1/today", headers=headers)
    assert r.status_code == 426 and r.json() == {
        "error": "update_required", "message": "Please update Zest Video to keep going.", "minBuild": 5,
    }
    assert (await env.http.get("/v1/today", headers={**headers, "X-Zest-Build": "5"})).status_code == 200


def test_client_ip_trusts_only_the_proxys_own_entry() -> None:
    class Req:
        def __init__(self, headers, host="10.0.0.1"):
            self.headers = headers
            self.client = type("C", (), {"host": host})()

    assert client_ip(Req({"x-forwarded-for": "1.2.3.4, 5.6.7.8"})) == "5.6.7.8"
    assert client_ip(Req({})) == "10.0.0.1"


# -- today ---------------------------------------------------------------------


async def test_today_folds_appointments_into_one_stop_per_customer(env: Env) -> None:
    headers = await login(env)
    body = (await env.http.get("/v1/today", headers=headers)).json()
    assert body["date"] == today()
    assert [s["customerId"] for s in body["stops"]] == [501, 502, 503], "Pat's stops only, in route order"

    jane, bob, acme = body["stops"]
    assert jane["name"] == "Jane Smith" and jane["lastName"] == "Smith", "stray whitespace is cleaned"
    assert jane["appointmentIds"] == [9001, 9002, 9003], "one video covers all three appointments"
    assert jane["services"] == ["Bi-Weekly Yard Maintenance", "Leaf Pick-up Service"]
    assert jane["done"] is True and jane["checkIn"] and jane["checkOut"]
    assert jane["address"] == "123 Main St" and jane["cityLine"] == "Folsom, CA 95630"
    assert (jane["lat"], jane["lng"]) == (38.67, -121.17)

    assert bob["done"] is False and bob["checkIn"] and bob["checkOut"] is None, "an open check-in"
    assert acme["name"] == "Acme Corp" and acme["lastName"] == "", "a business has no last name"
    assert acme["address"] == "1 Industrial Way", "the leading tab is gone"
    assert acme["lat"] is None and acme["lng"] is None, "0,0 means no map pin"
    assert body["config"]["maxSeconds"] == 15 and body["config"]["holdMinutes"] == 10
    assert body["config"]["officePhone"] == "+19165550100"


async def test_the_phone_never_receives_contact_or_billing_data(env: Env) -> None:
    headers = await login(env)
    r = await env.http.get("/v1/today", headers=headers)
    for secret in ("jane@example.com", "9165550001", "150.00", "Gate code", "bob@example.com"):
        assert secret not in r.text
    assert set(r.json()["stops"][0]) == {
        "customerId", "name", "lastName", "address", "cityLine", "lat", "lng", "appointmentIds", "services",
        "order", "checkIn", "checkOut", "done", "video",
    }


async def test_a_stop_whose_tech_is_only_on_the_route_reaches_that_tech(env: Env) -> None:
    sam = await login(env, "sam@example.com")
    body = (await env.http.get("/v1/today", headers=sam)).json()
    assert [s["customerId"] for s in body["stops"]] == [504]
    assert len(env.fr_calls("route", "search")) == 1, "the route read happens only because it was needed"


async def test_today_stays_inside_the_api_budget(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    first = len(env.fake.requests)
    for _ in range(5):
        assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
    assert len(env.fake.requests) == first, "a fresh cache costs no FieldRoutes calls"

    env.ctx.last_sync.clear()  # the cache went stale
    await env.http.get("/v1/today", headers=headers)
    new = env.fake.requests[first:]
    assert [(r.entity, r.action) for r in new] == [("appointment", "search"), ("route", "search")], (
        "a refresh re-reads the day, and no customer already cached"
    )


async def test_today_keeps_working_from_the_saved_route_when_fieldroutes_is_down(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.ctx.last_sync.clear()
    env.fake.fail_with = "FieldRoutes is down"
    body = (await env.http.get("/v1/today", headers=headers)).json()
    assert [s["customerId"] for s in body["stops"]] == [501, 502, 503]


async def test_a_cancelled_appointment_drops_off_the_list(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.fake.data["appointment"][9005]["status"] = "-1"
    env.ctx.last_sync.clear()
    body = (await env.http.get("/v1/today", headers=headers)).json()
    assert [s["customerId"] for s in body["stops"]] == [501, 502]


# -- a video's life ------------------------------------------------------------


async def test_register_upload_complete_then_email_after_the_window(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.mailer.sent.clear()
    body = video_body(501, recordedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

    r = await env.http.post("/v1/videos", json=body, headers=headers)
    assert r.status_code == 200
    reg = r.json()
    assert reg["state"] == "awaiting_upload"
    assert reg["upload"]["method"] == "PUT" and reg["upload"]["headers"] == {"Content-Type": "video/mp4"}
    key = f"videos/{today()}/{body['id']}.mp4"
    assert key in reg["upload"]["url"]

    # Asking again is safe and hands back a URL, not a second video.
    again = await env.http.post("/v1/videos", json=body, headers=headers)
    assert again.status_code == 200 and again.json()["state"] == "awaiting_upload"
    assert len(await env.all(sa.select(db.videos))) == 1

    # Confirming before the bucket has the file is refused, so the phone keeps its copy.
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": 5000}, headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "not_uploaded"
    env.storage.objects[key] = b"x" * 4999  # a short upload
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": 5000}, headers=headers)
    assert r.status_code == 409

    env.storage.objects[key] = b"x" * 5000
    r = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": 5000}, headers=headers)
    assert r.status_code == 200 and r.json()["state"] == "held"
    due = datetime.fromisoformat(r.json()["emailAt"].replace("Z", "+00:00"))
    assert timedelta(minutes=9) < due - datetime.now(timezone.utc) <= timedelta(minutes=10)
    again = await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": 5000}, headers=headers)
    assert again.json()["state"] == "held"

    # Inside the window nothing is emailed.
    assert await videos.send_due(env.ctx) == 0 and env.mailer.sent == []

    await env.window_closed(body["id"])
    assert await videos.send_due(env.ctx) == 1
    video = await env.video(body["id"])
    assert video.state == "sent" and video.email_to == "jane@example.com"
    assert video.appointment_ids == [9001, 9002, 9003], "tied to every appointment of the visit"
    assert video.customer_name == "Jane Smith" and video.address == "123 Main St"

    (email,) = env.mailer.sent
    link = f"https://video.test/w/{video.token}"
    assert email.to == "jane@example.com" and email.subject == "Your service video from Pat"
    assert link in email.html and link in email.text and "Watch your video" in email.html
    assert email.reply_to == "office@zest.test"
    assert f"<https://video.test/u/{video.token}>" == email.headers["List-Unsubscribe"]
    assert len(video.token) == 32 and video.token != body["id"].replace("-", "")

    # Never twice.
    assert await videos.send_due(env.ctx) == 0 and len(env.mailer.sent) == 1

    today_body = (await env.http.get("/v1/today", headers=headers)).json()
    assert today_body["stops"][0]["video"] == {"id": body["id"], "state": "sent", "byYou": True}
    assert today_body["videos"][0]["id"] == body["id"]


async def test_the_link_is_filed_in_fieldroutes_after_the_email(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501, recommend=True)
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    assert await videos.run_outbox(env.ctx) == 2
    video = await env.video(video_id)

    (note,) = env.fr_calls("note", "create")
    assert note.one("customerID") == "501" and note.one("contactType") == "5"
    assert f"https://video.test/w/{video.token}" in note.one("notes")
    assert note.one("showCustomer") == "0" and note.one("showTech") == "0" and note.one("showOnInvoice") == "0"

    # "I recommended something" opens a follow-up task and a soft flag for the office.
    (task,) = env.fr_calls("task", "create")
    assert task.one("customerID") == "501" and task.one("type") == "0" and task.one("category") == "10002"
    assert [f.kind for f in await env.flags()] == ["recommend"]

    # A retry never writes twice.
    assert await videos.run_outbox(env.ctx) == 0
    assert len(env.fr_calls("note", "create")) == 1


async def test_recording_again_inside_the_window_replaces_the_old_video_by_itself(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    first = await film(env, headers, 501)
    first_key = (await env.video(first)).object_key
    second = await film(env, headers, 501)

    old, new = await env.video(first), await env.video(second)
    assert old.state == "replaced" and new.state == "held"
    assert first_key in env.storage.deleted and first_key not in env.storage.objects
    assert (await env.http.get(f"/w/{old.token}")).status_code == 410, "the old link is dead"

    await env.window_closed(second)
    await videos.send_due(env.ctx)
    assert [m.to for m in env.mailer.sent if "video" in m.subject] == ["jane@example.com"], "one email, for the new one"

    # Once it has gone to the customer the stop is locked for the tech.
    r = await env.http.post("/v1/videos", json=video_body(501), headers=headers)
    assert r.status_code == 409
    assert r.json()["error"] == "visit_has_video" and "Call the office" in r.json()["message"]
    assert r.json()["existing"] == {"id": second, "state": "sent", "byYou": True, "techFirstName": "Pat"}


async def test_the_tech_can_delete_for_ten_minutes_and_not_after(env: Env) -> None:
    headers = await login(env)
    sam = await login(env, "sam@example.com")
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501)
    key = (await env.video(video_id)).object_key

    assert (await env.http.delete(f"/v1/videos/{video_id}", headers=sam)).status_code == 404, "not his video"
    r = await env.http.delete(f"/v1/videos/{video_id}", headers=headers)
    assert r.status_code == 200 and r.json() == {"id": video_id, "state": "deleted"}
    assert key in env.storage.deleted
    assert (await env.http.delete(f"/v1/videos/{video_id}", headers=headers)).json()["state"] == "deleted"
    assert await videos.send_due(env.ctx) == 0

    # A deleted video frees the stop: he can film it again.
    second = await film(env, headers, 501)
    await env.window_closed(second)
    await videos.send_due(env.ctx)
    r = await env.http.delete(f"/v1/videos/{second}", headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "too_late"
    assert (await env.video(second)).state == "sent"


def test_the_window_runs_from_the_recording_not_the_upload() -> None:
    now = datetime(2026, 11, 13, 18, 0, tzinfo=timezone.utc)
    m = lambda n: timedelta(minutes=n)  # noqa: E731
    assert email_due(now - m(1), now, 10) == now + m(9), "uploaded a minute after filming"
    assert email_due(now - m(240), now, 10) == now + m(1), "uploaded hours later: the window is long gone"
    assert email_due(now + m(500), now, 10) == now + m(10), "a phone clock running fast cannot stall the email"
    assert email_due(now, now, 0) == now + m(1), "no window still leaves a beat for the confirm"


@pytest.mark.parametrize(
    "change, message",
    [
        ({"id": "not-a-uuid"}, "bad ID"),
        ({"sizeBytes": 0}, "missing"),
        ({"sizeBytes": 10**9}, "too big"),
        ({"durationSec": 45}, "too long"),
        ({"stopDate": "2020-01-01"}, "too old"),
        ({"customerId": 999999}, "not on the list"),
        ({"customerId": None, "notOnMyList": False}, "Pick a stop"),
    ],
)
async def test_bad_registrations_are_refused_in_plain_words(env: Env, change: dict, message: str) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    r = await env.http.post("/v1/videos", json=video_body(501, **change), headers=headers)
    assert r.status_code == 400 and message in r.json()["message"]


async def test_one_techs_video_id_cannot_be_claimed_by_another(env: Env) -> None:
    pat, sam = await login(env), await login(env, "sam@example.com")
    await env.http.get("/v1/today", headers=pat)
    body = video_body(501)
    assert (await env.http.post("/v1/videos", json=body, headers=pat)).status_code == 200
    assert (await env.http.post("/v1/videos", json=body, headers=sam)).status_code == 400
    assert (await env.http.post(f"/v1/videos/{body['id']}/complete", json={}, headers=sam)).status_code == 404


# -- who gets an email ---------------------------------------------------------


async def _sent_state(env: Env, headers: dict, customer_id: int) -> tuple:
    video_id = await film(env, headers, customer_id)
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    return await env.video(video_id), [m for m in env.mailer.sent if m.tag == "video"]


async def test_customer_emails_are_off_until_someone_turns_them_on(tmp_path) -> None:
    env = await make_env(tmp_path, email_mode="off")
    try:
        headers = await login(env)
        await env.http.get("/v1/today", headers=headers)
        video, mails = await _sent_state(env, headers, 501)
        assert mails == [] and (video.state, video.email_skip) == ("no_email", "email_off")
        assert (await env.http.get(f"/w/{video.token}")).status_code == 200, "the link still works for the office"
        assert await env.flags() == [], "nothing to chase: it is how the server is set"
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_test_mode_sends_every_customer_email_to_the_test_address(tmp_path) -> None:
    env = await make_env(tmp_path, email_mode="test", email_test_to="owner@zest.test")
    try:
        headers = await login(env)
        await env.http.get("/v1/today", headers=headers)
        video, mails = await _sent_state(env, headers, 501)
        assert [m.to for m in mails] == ["owner@zest.test"] and mails[0].subject.startswith("[TEST] ")
        assert "j***@example.com" in mails[0].html and "jane@example.com" not in mails[0].html
        assert video.state == "sent"
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_no_email_on_file_saves_the_link_as_a_customer_visible_note_and_flags_the_office(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video, mails = await _sent_state(env, headers, 503)
    assert mails == [] and (video.state, video.email_skip) == ("no_email", "no_address")
    (flag,) = await env.flags("no_email")
    assert flag.customer_id == 503 and "No email on file" in flag.detail
    await videos.run_outbox(env.ctx)
    (note,) = env.fr_calls("note", "create")
    assert note.one("showCustomer") == "1" and video.token in note.one("notes")


async def test_email_reminders_off_in_fieldroutes_means_no_email_unless_the_owner_says_otherwise(env: Env) -> None:
    sam = await login(env, "sam@example.com")
    await env.http.get("/v1/today", headers=sam)
    video, mails = await _sent_state(env, sam, 504)
    assert mails == [] and video.email_skip == "fr_reminders_off"
    assert len(await env.flags("no_email")) == 1

    from zest_video.core import set_setting

    await set_setting(env.ctx, "respect_fr_email_reminders", False, "owner@zest.test")
    await videos.pull(env.ctx, video.id, "owner@zest.test", "test")
    video, mails = await _sent_state(env, sam, 504)
    assert [m.to for m in mails] == ["ann@example.com"] and video.state == "sent"


async def test_a_customer_who_stopped_video_emails_is_not_emailed_again(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video, mails = await _sent_state(env, headers, 501)
    assert len(mails) == 1

    page = await env.http.get(f"/u/{video.token}")
    assert page.status_code == 200 and "Stop video emails?" in page.text
    assert await env.one(sa.select(db.email_optouts)) is None, "opening the page changes nothing"
    assert (await env.http.post(f"/u/{video.token}")).status_code == 200
    assert (await env.one(sa.select(db.email_optouts))).customer_id == 501

    await videos.pull(env.ctx, video.id, "owner@zest.test", "test")
    video, mails = await _sent_state(env, headers, 501)
    assert len(mails) == 1 and video.email_skip == "opted_out"


async def test_the_test_customer_list_keeps_everyone_else_out(tmp_path) -> None:
    env = await make_env(tmp_path, email_customer_ids=frozenset({502}))
    try:
        headers = await login(env)
        await env.http.get("/v1/today", headers=headers)
        video, mails = await _sent_state(env, headers, 501)
        assert mails == [] and video.email_skip == "not_in_test_list"
        video, mails = await _sent_state(env, headers, 502)
        assert [m.to for m in mails] == ["bob@example.com"]
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


async def test_a_failing_mail_provider_is_retried_then_handed_to_the_office(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501)
    env.mailer.fail_with = "provider said 500"
    for _ in range(videos.EMAIL_MAX_ATTEMPTS - 1):
        await env.window_closed(video_id)
        assert await videos.send_due(env.ctx) == 0
        assert (await env.video(video_id)).state == "held", "still trying"
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "send_failed")
    (flag,) = await env.flags("no_email")
    assert "could not be sent" in flag.detail


async def test_fieldroutes_down_at_send_time_just_waits(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501)
    await env.window_closed(video_id)
    env.fake.fail_with = "down"
    env.fake.fail_write_after = None
    # sync_day is fresh, so the one FieldRoutes call here is the customer lookup.
    assert await videos.send_due(env.ctx) == 0
    assert (await env.video(video_id)).state == "held"
    await env.window_closed(video_id)
    assert await videos.send_due(env.ctx) == 1


# -- not on my list ------------------------------------------------------------


async def test_not_on_my_list_waits_for_the_office_and_emails_nobody(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, None, spokenText="Reservice at the Young house on Elm")
    video = await env.video(video_id)
    assert video.state == "unmatched" and video.customer_id is None and video.email_due_at is None
    (flag,) = await env.flags("unmatched")
    assert "Young house on Elm" in flag.detail
    assert await videos.send_due(env.ctx) == 0
    assert (await env.http.get(f"/w/{video.token}")).status_code == 410, "no link until it is matched"

    # The office matches it to a customer who is not on today's list at all.
    await videos.move(env.ctx, video_id, 505, "owner@zest.test")
    moved = await env.video(video_id)
    assert moved.state == "held" and moved.customer_id == 505 and moved.token != video.token
    assert moved.customer_name == "Cy Young" and moved.address == "77 Elm St"
    assert await env.flags("unmatched") == [], "the flag clears itself"
    assert await videos.send_due(env.ctx) == 1
    assert env.mailer.sent[-1].to == "cy@example.com"


# -- override and the server's own time check ------------------------------------


async def test_an_override_is_logged_and_flagged_but_never_held(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(
        env, headers, 501, override=True, signals={"spoken": "mismatch", "gps": "match", "checkIn": "unknown"}
    )
    (flag,) = await env.flags("wrong_house")
    assert flag.video_id == video_id and "spoken" in flag.detail and "Pat" in flag.detail
    await env.window_closed(video_id)
    assert await videos.send_due(env.ctx) == 1, "nothing waits on the office"
    event = await env.one(sa.select(db.events).where(db.events.c.action == "video_registered"))
    assert event.detail["override"] is True and event.detail["signals"]["spoken"] == "mismatch"


async def test_a_video_recorded_outside_the_check_in_time_is_flagged_not_blocked(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    hours_ago = (datetime.now(timezone.utc) - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    video_id = await film(env, headers, 501, recordedAt=hours_ago)
    await env.window_closed(video_id)
    assert await videos.send_due(env.ctx) == 1
    assert (await env.video(video_id)).time_check == "mismatch"
    (flag,) = await env.flags("wrong_house")
    assert "outside the check-in time" in flag.detail

    # Inside the window (with slack) there is no flag. Acme has no check-in: unknown, no flag.
    in_window = await film(env, headers, 502, recordedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert (await env.video(in_window)).time_check == "match"
    assert (await env.video(await film(env, headers, 503))).time_check == "unknown"
    assert len(await env.flags("wrong_house")) == 1


# -- the server keeps score ------------------------------------------------------


async def test_a_finished_stop_with_no_video_is_flagged_and_clears_when_the_video_arrives(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    assert await jobs.flag_missing(env.ctx) == 0, "Jane only just finished; give the tech time"

    await env.run(sa.update(db.stops).values(done_seen_at=db.now() - timedelta(minutes=61)))
    assert await jobs.flag_missing(env.ctx) == 1
    (flag,) = await env.flags("no_video")
    assert flag.customer_id == 501 and flag.tech_id == 20 and "Jane Smith" in flag.detail
    assert await jobs.flag_missing(env.ctx) == 0, "raised once"

    # The video was on the phone all along with no signal. It lands; the flag goes away.
    await env.http.post("/v1/videos", json=video_body(501), headers=headers)
    assert await env.flags("no_video") == []
    assert await jobs.flag_missing(env.ctx) == 0


async def test_services_switched_off_for_video_are_not_chased(env: Env) -> None:
    from zest_video.core import set_setting

    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    await env.run(sa.update(db.stops).values(done_seen_at=db.now() - timedelta(hours=2)))
    await set_setting(env.ctx, "skip_service_ids", "89, 95", "owner@zest.test")
    assert await jobs.flag_missing(env.ctx) == 0
    await set_setting(env.ctx, "skip_service_ids", "89", "owner@zest.test")
    assert await jobs.flag_missing(env.ctx) == 1, "one of Jane's services still needs a video"
    await set_setting(env.ctx, "require_video", False, "owner@zest.test")
    await env.run(sa.delete(db.flags))
    assert await jobs.flag_missing(env.ctx) == 0


async def test_a_stuck_upload_nudges_the_tech_then_flags_the_office(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    r = await env.http.post("/v1/devices/push", json={"apnsToken": "AB CD-12", "sandbox": True}, headers=headers)
    assert r.status_code == 200
    body = video_body(501)
    await env.http.post("/v1/videos", json=body, headers=headers)

    await jobs.chase_stuck(env.ctx)
    assert env.pusher.sent == [] and await env.flags("stuck_upload") == [], "too soon"

    await env.run(sa.update(db.videos).values(created_at=db.now() - timedelta(minutes=31)))
    await jobs.chase_stuck(env.ctx)
    await jobs.chase_stuck(env.ctx)
    assert env.pusher.sent == [("abcd12", "A video is waiting to send", "Open Zest Video so it can finish sending.")]
    assert await env.flags("stuck_upload") == [], "nudge the tech first"

    await env.run(sa.update(db.videos).values(created_at=db.now() - timedelta(hours=3, minutes=1)))
    await jobs.chase_stuck(env.ctx)
    (flag,) = await env.flags("stuck_upload")
    assert "Pat's video for Jane Smith" in flag.detail and len(env.pusher.sent) == 1

    # It finally uploads: the flag clears itself.
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]}, headers=headers)
    assert await env.flags("stuck_upload") == []


async def test_a_fieldroutes_outage_never_loses_a_write_and_a_dead_one_reaches_the_office(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501)
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)

    env.fake.fail_with = "FieldRoutes hiccup"
    assert await videos.run_outbox(env.ctx) == 0
    row = await env.one(sa.select(db.fr_outbox))
    assert row.attempts == 1 and row.done_at is None and "hiccup" in row.last_error
    assert await videos.run_outbox(env.ctx) == 0, "it waits for its retry time"

    await env.run(sa.update(db.fr_outbox).values(next_try_at=db.now()))
    assert await videos.run_outbox(env.ctx) == 1 and len(env.fr_calls("note", "create")) == 2

    # One that keeps failing ends up as a flag instead of vanishing.
    await videos.enqueue_fr(env.ctx, "note", "note:doomed", 501, video_id, {"text": "x", "day": today()})
    for _ in range(len(videos.OUTBOX_BACKOFF)):
        env.fake.fail_with = "still down"
        await env.run(sa.update(db.fr_outbox).where(db.fr_outbox.c.done_at.is_(None)).values(next_try_at=db.now()))
        await videos.run_outbox(env.ctx)
    (flag,) = await env.flags("fr_write_failed")
    assert "still down" in flag.detail


async def test_fieldroutes_writes_obey_the_same_off_switch_and_allowlist_as_the_mcp_server(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.reconfigure(fr_writes=False)
    video_id = await film(env, headers, 501)
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    await videos.run_outbox(env.ctx)
    assert env.fr_calls("note", "create") == []
    row = await env.one(sa.select(db.fr_outbox))
    assert row.done_at is not None and row.last_error.startswith("skipped: FieldRoutes writes are off")
    assert await env.flags("fr_write_failed") == []

    env.reconfigure(fr_writes=True, fr_write_customer_ids=frozenset({502}))
    for customer in (502, 503):
        vid = await film(env, headers, customer)
        await env.window_closed(vid)
    await videos.send_due(env.ctx)
    await videos.run_outbox(env.ctx)
    assert [r.one("customerID") for r in env.fr_calls("note", "create")] == ["502"]


async def test_the_daily_sweep_deletes_old_files_and_leaves_fresh_ones(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    old = await film(env, headers, 501)
    fresh = await film(env, headers, 502)
    await env.run(sa.update(db.videos).where(db.videos.c.id == old).values(created_at=db.now() - timedelta(days=400)))
    await jobs.daily(env.ctx)
    gone, kept = await env.video(old), await env.video(fresh)
    assert gone.state == "pulled" and gone.object_deleted and gone.object_key not in env.storage.objects
    assert kept.state == "held" and kept.object_key in env.storage.objects
    assert (await env.http.get(f"/w/{gone.token}")).status_code == 410


# -- App Review demo account ---------------------------------------------------


async def test_the_demo_account_works_end_to_end_and_touches_nothing_real(env: Env) -> None:
    headers = await demo_login(env)
    calls_before = len(env.fake.requests)
    body = (await env.http.get("/v1/today", headers=headers)).json()
    assert body["tech"] == {"id": -1, "firstName": "Demo", "demo": True}
    assert [s["customerId"] for s in body["stops"]] == [-101, -102, -103]

    video_id = await film(env, headers, -101)
    await env.window_closed(video_id)
    assert await videos.send_due(env.ctx) == 0
    video = await env.video(video_id)
    assert video.demo and (video.state, video.email_skip) == ("no_email", "demo")
    assert await videos.run_outbox(env.ctx) == 0
    assert env.mailer.sent == [] and len(env.fake.requests) == calls_before, "no email, no FieldRoutes call"
    assert await env.flags(open_only=False) == []

    after = (await env.http.get("/v1/today", headers=headers)).json()
    assert after["stops"][0]["video"]["state"] == "no_email"

    wrong = await env.http.post("/v1/auth/verify", json={"contact": DEMO_CONTACT, "code": "000000"})
    assert wrong.status_code == 401


async def test_the_demo_account_does_not_exist_unless_configured(tmp_path) -> None:
    env = await make_env(tmp_path, demo_contact="", demo_code="")
    try:
        r = await env.http.post("/v1/auth/verify", json={"contact": DEMO_CONTACT, "code": DEMO_CODE})
        assert r.status_code == 401
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


# -- audit trail from the phone --------------------------------------------------


async def test_phone_events_land_in_the_audit_trail(env: Env) -> None:
    headers = await login(env)
    vid = str(uuid.uuid4())
    r = await env.http.post(
        "/v1/events",
        json={"events": [
            {"type": "far_warning", "at": "2026-11-13T18:41:00Z", "videoId": vid, "customerId": 501,
             "detail": {"meters": 420}},
            {"type": "made_up", "at": "2026-11-13T18:41:00Z"},
            "junk",
        ]},
        headers=headers,
    )
    assert r.json() == {"ok": True, "saved": 1}
    event = await env.one(sa.select(db.events).where(db.events.c.action == "app_far_warning"))
    assert (event.actor_type, event.actor, event.video_id, event.customer_id) == ("tech", "20", vid, 501)
    assert event.detail == {"meters": 420} and event.at == datetime(2026, 11, 13, 18, 41, tzinfo=timezone.utc)
