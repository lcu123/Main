"""Hostile and awkward input. Nothing a phone, a customer's browser or FieldRoutes can
send may crash the server, leak into a page as markup, or reach someone else's data."""

from __future__ import annotations

import json
import random
import string
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
import sqlalchemy as sa

from video_helpers import OWNER, Env, film, login, make_env, office_login, today, video_body

from zest_video import db, videos
from zest_video.fr import clean, id_list, local_time, to_float, to_int
from zest_video.route import sync_day


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


SCRIPT = '<script>alert("x")</script>'
NASTY_NAMES = [
    ("Robert'); DROP TABLE videos;--", "O'Brien"),
    ("José", "Peña-García"),
    ("  Mary\tAnn ", "van der Berg"),
    (SCRIPT, '"><img src=x onerror=alert(1)>'),
    ("😀 Sunny", "Day 🌞"),
    ("A" * 120, "B" * 120),
]


# -- markup and odd characters never become markup -------------------------------------


@pytest.mark.parametrize("first, last", NASTY_NAMES)
async def test_awkward_customer_names_are_shown_safely_everywhere(env: Env, first: str, last: str) -> None:
    env.fake.data["customer"][501].update(fname=first, lname=last, address=f'12 {SCRIPT} "Quote" & Sons Rd')
    headers = await login(env)
    body = (await env.http.get("/v1/today", headers=headers)).json()
    assert body["stops"][0]["customerId"] == 501 and len(body["stops"][0]["name"]) <= 200

    video_id = await film(env, headers, 501, spokenText=f"at the {last} home {SCRIPT}")
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    video = await env.video(video_id)
    assert video.state == "sent"

    watch = (await env.http.get(f"/w/{video.token}")).text
    email = env.mailer.sent[-1]
    await office_login(env)
    office = (await env.http.get("/office")).text
    audit = (await env.http.get("/office/audit")).text
    for page in (watch, email.html, office, audit):
        assert "<script>alert" not in page and "onerror=alert" not in page.replace("&#34;", '"').split("<body")[-1].replace(
            "onerror=alert(1)&gt;", ""
        ), "no customer text may arrive as live markup"
    assert "\n" not in email.subject and "\r" not in email.subject


async def test_a_customers_comment_and_a_techs_words_cannot_inject_markup(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, None, spokenText=f"reservice {SCRIPT}")  # not on my list: shown to the office
    sent_id = await film(env, headers, 501)
    await env.window_closed(sent_id)
    await videos.send_due(env.ctx)
    sent = await env.video(sent_id)
    await env.http.post(f"/w/{sent.token}/score", data={"stars": "1", "comment": f"bad {SCRIPT} <b>bold</b>"})

    await office_login(env)
    office = (await env.http.get("/office")).text
    assert SCRIPT not in office and "<b>bold</b>" not in office
    assert "&lt;script&gt;" in office, "it is shown, as text"
    assert video_id


async def test_a_tech_name_with_a_line_break_cannot_split_the_email_subject(env: Env) -> None:
    env.fake.data["employee"][20]["fname"] = "Pat\r\nBcc: someone@evil.example"
    headers = await login(env, "916-555-0101")
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501)
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    subject = env.mailer.sent[-1].subject
    assert "\n" not in subject and "\r" not in subject


# -- the phone API never answers 500 to nonsense ----------------------------------------


def _junk(rng: random.Random, depth: int = 0):
    kinds = ["str", "int", "float", "none", "bool", "list", "dict", "big", "neg", "uni"]
    kind = rng.choice(kinds if depth < 2 else kinds[:5])
    if kind == "str":
        return "".join(rng.choice(string.printable) for _ in range(rng.randint(0, 40)))
    if kind == "int":
        return rng.randint(-5, 10**12)
    if kind == "float":
        return rng.choice([0.0, -1.5, 1e308, float(rng.randint(1, 99))])
    if kind == "none":
        return None
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "big":
        return "x" * 5000
    if kind == "neg":
        return -1
    if kind == "uni":
        return rng.choice(["😀", "\u0000", "'; DROP TABLE videos;--", "../../etc/passwd", "ñ", "‮"])
    if kind == "list":
        return [_junk(rng, depth + 1) for _ in range(rng.randint(0, 4))]
    return {str(i): _junk(rng, depth + 1) for i in range(rng.randint(0, 4))}


async def test_garbage_in_any_field_of_a_registration_is_refused_not_crashed_on(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    rng = random.Random(20261113)
    fields = list(video_body(501))
    statuses: dict[int, int] = {}
    for _ in range(300):
        body = video_body(501)
        for field in rng.sample(fields, rng.randint(1, 4)):
            body[field] = _junk(rng)
        r = await env.http.post("/v1/videos", content=json.dumps(body).encode(), headers=headers)
        statuses[r.status_code] = statuses.get(r.status_code, 0) + 1
        assert r.status_code < 500, f"{r.status_code} for {body!r}: {r.text}"
    assert statuses.get(400, 0) > 100, "most garbage is told why it was refused"
    # And the stop is still usable afterwards.
    assert (await env.http.post("/v1/videos", json=video_body(501), headers=headers)).status_code in (200, 409)


@pytest.mark.parametrize(
    "method, path",
    [
        ("POST", "/v1/auth/request-code"), ("POST", "/v1/auth/verify"), ("POST", "/v1/videos"),
        ("POST", "/v1/videos/not-a-uuid/complete"), ("POST", "/v1/events"), ("POST", "/v1/devices/push"),
        ("DELETE", "/v1/videos/%00%0a"), ("POST", f"/v1/videos/{'9' * 300}/complete"),
    ],
)
@pytest.mark.parametrize(
    "payload",
    [b"", b"{", b"[]", b'"text"', b"null", b"123", b'{"contact": {"a": 1}, "code": [1,2], "events": "no"}',
     b"\xff\xfe\x00bad bytes", b'{"contact": "' + b"9" * 5000 + b'"}', b"x" * 70_000],
)
async def test_every_phone_endpoint_survives_a_broken_body(env: Env, method: str, path: str, payload: bytes) -> None:
    headers = await login(env)
    r = await env.http.request(method, path, content=payload, headers=headers)
    assert r.status_code < 500, f"{method} {path} answered {r.status_code}: {r.text[:200]}"
    assert r.headers["content-type"].startswith("application/json")
    assert "error" in r.json() or r.status_code == 200


async def test_sign_in_fields_full_of_sql_and_script_do_no_harm(env: Env) -> None:
    for contact in ["' OR 1=1 --", SCRIPT, "9165550101'; DROP TABLE techs;--", "a@b.c\r\nBcc: x@y.z", "%00", "😀" * 50]:
        r = await env.http.post("/v1/auth/request-code", json={"contact": contact})
        assert r.status_code in (200, 400, 429)
        r = await env.http.post("/v1/auth/verify", json={"contact": contact, "code": "' OR '1'='1"})
        assert r.status_code in (401, 429)
    assert env.texter.sent == [] and env.mailer.sent == []
    assert len(await env.all(sa.select(db.techs))) >= 0, "the tables are still there"
    assert (await env.http.post("/v1/auth/request-code", json={"contact": "sam@example.com"})).status_code in (200, 429)


# -- one person can never reach another's data -------------------------------------------


async def test_one_tech_cannot_see_or_touch_another_techs_stops_and_videos(env: Env) -> None:
    pat, sam = await login(env), await login(env, "sam@example.com")
    await env.http.get("/v1/today", headers=pat)
    pats_video = await film(env, pat, 501)

    sams_day = (await env.http.get("/v1/today", headers=sam)).json()
    assert 501 not in [s["customerId"] for s in sams_day["stops"]] and sams_day["videos"] == []
    assert pats_video not in json.dumps(sams_day)
    assert (await env.http.post(f"/v1/videos/{pats_video}/complete", json={}, headers=sam)).status_code == 404
    assert (await env.http.delete(f"/v1/videos/{pats_video}", headers=sam)).status_code == 404
    assert (await env.video(pats_video)).state == "held"


async def test_office_pages_and_actions_are_closed_without_a_session(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501)
    env.http.cookies.clear()
    for path in ("/office", "/office/techs", "/office/settings", "/office/users", "/office/stats", "/office/audit",
                 f"/office/video/{video_id}"):
        r = await env.http.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/office/login", path
    for path in (f"/office/video/{video_id}/pull", f"/office/video/{video_id}/move", "/office/flag/1/clear",
                 "/office/techs/20/code", "/office/settings", "/office/users", "/office/logout"):
        r = await env.http.post(path, data={"csrf": "x", "customer_id": "502", "email": "evil@example.com", "role": "owner"})
        assert r.status_code == 303 and r.headers["location"] == "/office/login", path
    assert (await env.video(video_id)).state == "held"
    assert [u.email for u in await env.all(sa.select(db.office_users))] == [OWNER]
    # A tech's phone token is not an office session, and a made-up cookie is nothing.
    env.http.cookies.set("zv_office", headers["Authorization"].split()[1], domain="video.test", path="/office")
    assert (await env.http.get("/office")).status_code == 303


async def test_office_forms_survive_null_characters_and_impossible_numbers(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    video_id = await film(env, headers, 501)
    await film(env, headers, 502, override=True)
    (flag,) = await env.flags("wrong_house")
    csrf = await office_login(env)
    nul = "a\x00b"
    posts = [
        (f"/office/video/{video_id}/move", {"customer_id": "9" * 30}),
        (f"/office/video/{video_id}/move", {"customer_id_other": "121604549758"}),
        (f"/office/video/{video_id}/pull", {"reason": nul * 50}),
        ("/office/video/not-a-video/pull", {"reason": "x"}),
        ("/office/video/%00/move", {"customer_id": "502"}),
        (f"/office/flag/{flag.id}/clear", {"note": nul}),
        ("/office/flag/99999999999999999999/clear", {"note": "x"}),
        ("/office/techs/99999999999999999999/code", {}),
        ("/office/users", {"email": f"new{nul}@zest.test", "role": "staff"}),
        ("/office/settings", {"email_subject": nul + "Video from {tech}", "prompts": f"one{nul}\ntwo"}),
        ("/office/login", {"email": nul + "@x.y"}),
        ("/office/verify", {"email": nul, "code": nul}),
    ]
    for path, data in posts:
        r = await env.http.post(path, data={"csrf": csrf, **data})
        assert r.status_code < 500, f"{path} answered {r.status_code}"
    for path in ("/office/video/%00", "/office/audit?video=a%00b&action=a%00b", "/office?day=%00", "/office/stats?days=9e99"):
        assert (await env.http.get(path)).status_code < 500, path
    assert (await env.video(video_id)).state == "pulled", "the one honest pull in that list went through"


async def test_links_in_emails_come_from_the_servers_own_address_never_the_request(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers={**headers, "Host": "evil.example", "X-Forwarded-Host": "evil.example"})
    body = video_body(501)
    await env.http.post("/v1/videos", json=body, headers={**headers, "Host": "evil.example"})
    env.storage.objects[(await env.video(body["id"])).object_key] = b"v" * body["sizeBytes"]
    await env.http.post(f"/v1/videos/{body['id']}/complete", json={"sizeBytes": body["sizeBytes"]},
                        headers={**headers, "Host": "evil.example"})
    await env.window_closed(body["id"])
    await videos.send_due(env.ctx)
    email = env.mailer.sent[-1]
    assert "evil.example" not in email.html + email.text and "https://video.test/w/" in email.html


async def test_static_files_cannot_be_used_to_read_the_servers_own_files(env: Env) -> None:
    for path in ("/static/../config.py", "/static/..%2fconfig.py", "/static/%2e%2e/app.py", "/static/../../pyproject.toml",
                 "/static//etc/passwd", "/static/templates/office_base.html"):
        r = await env.http.get(path)
        assert r.status_code in (400, 404), path
        assert "VIDEO_SECRET" not in r.text and "def main" not in r.text


# -- FieldRoutes sending something unexpected ------------------------------------------------


async def test_a_day_of_malformed_fieldroutes_records_does_not_stop_the_route(env: Env) -> None:
    rng = random.Random(7)
    day = today()
    for i in range(60):
        row = {"appointmentID": str(9100 + i), "officeID": "7", "date": day, "status": rng.choice(["0", "1"])}
        for field in ("customerID", "routeID", "type", "assignedTech", "additionalTechs", "checkIn", "checkOut",
                      "timeIn", "timeOut", "start", "sequence"):
            if rng.random() < 0.8:
                row[field] = rng.choice([None, "", "0", "-1", "abc", "20", "501", "999999", "2026-13-45 99:99:99",
                                         "0000-00-00 00:00:00", "12,,x,20", [], {}, 3.5, True])
        env.fake.data["appointment"][9100 + i] = row
    env.fake.data["customer"][999999] = {"customerID": "999999", "officeID": "7", "fname": None, "lname": None,
                                         "address": None, "lat": "north", "lng": None, "email": 5}
    await sync_day(env.ctx, day, force=True)  # must not raise
    headers = await login(env)
    r = await env.http.get("/v1/today", headers=headers)
    assert r.status_code == 200
    names = [s["name"] for s in r.json()["stops"]]
    assert "Jane Smith" in names, "the good stops are still there"
    for stop in r.json()["stops"]:
        assert isinstance(stop["name"], str) and stop["name"], "every stop has something to show"
        assert stop["lat"] is None or isinstance(stop["lat"], float)


def test_fieldroutes_value_helpers_take_anything() -> None:
    for junk in (None, "", "abc", [], {}, 3.5, True, "0000-00-00 00:00:00", "12,,x", b"bytes", "１２"):
        to_int(junk), to_float(junk), clean(junk), id_list(junk), local_time(junk, ZoneInfo("America/Los_Angeles"))
    assert id_list("12,34, 56") == [12, 34, 56] and id_list("12,,x,0,-3") == [12]
    assert to_float("0") is None and to_float("0.000000") is None and to_float("38.5") == 38.5


# -- clocks and calendars ---------------------------------------------------------------------


def test_office_local_times_are_right_on_both_sides_of_a_clock_change() -> None:
    tz = ZoneInfo("America/Los_Angeles")
    # Nov 1 2026: clocks go back at 2:00. Mar 8 2026: clocks go forward at 2:00.
    assert local_time("2026-10-31 12:00:00", tz) == datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc)
    assert local_time("2026-11-01 12:00:00", tz) == datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
    assert local_time("2026-03-08 12:00:00", tz) == datetime(2026, 3, 8, 19, 0, tzinfo=timezone.utc)
    assert local_time("2026-03-08 02:30:00", tz) is not None, "an hour that does not exist still gets an answer"
    assert local_time("2026-11-01 01:30:00", tz) is not None, "an hour that happens twice still gets an answer"


async def test_a_video_filmed_just_before_midnight_and_sent_just_after_is_still_taken(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    from datetime import timedelta

    yesterday = (datetime.strptime(today(), "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    # Move the whole day's stops to "yesterday", as if midnight just passed.
    await env.run(sa.update(db.stops).values(day=yesterday))
    r = await env.http.post("/v1/videos", json=video_body(501, stopDate=yesterday), headers=headers)
    assert r.status_code == 200, r.text
    tomorrow = (datetime.strptime(today(), "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    r = await env.http.post("/v1/videos", json=video_body(None, stopDate=tomorrow), headers=headers)
    assert r.status_code == 200, "a phone whose date is a few hours ahead of the office is not refused"
    r = await env.http.post("/v1/videos", json=video_body(None, stopDate="2031-01-01"), headers=headers)
    assert r.status_code == 400


# -- the shared FieldRoutes quota ---------------------------------------------------------------


async def test_a_busy_minute_of_emails_costs_one_fieldroutes_read_not_one_each(env: Env) -> None:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    for customer in (501, 502, 503):
        await env.window_closed(await film(env, headers, customer))
    before = len(env.fr_calls("customer", "get"))
    await videos.send_due(env.ctx)
    assert len(env.fr_calls("customer", "get")) - before == 1
    assert sorted(m.to for m in env.mailer.sent if m.tag == "video") == ["bob@example.com", "jane@example.com"]
    acme = await env.one(sa.select(db.videos).where(db.videos.c.customer_id == 503))
    assert acme.email_skip == "no_address", "the one with no email is still handled, from the same read"
