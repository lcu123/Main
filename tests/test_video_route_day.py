"""The day's route changing under the app, and techs who do not follow it.

The owner asked what happens when the office changes the route midday, twice a day,
removes the property, or the tech does not work the route the way FieldRoutes has it.
Each case is played here against the real server, at every stage a video can be in:
no video yet, registered and still uploading, uploaded and inside the 10-minute hold,
and already emailed. Four things are checked each time:

- no video is lost;
- no video reaches the wrong customer, and nobody gets two emails for one visit, or an
  email for a visit the office took off the day, unless the office says so. That holds
  when both techs film a stop that changed hands, in either order, and when FieldRoutes
  cannot be read at the moment the email is due;
- the office is told (a flag) when a video and the route no longer agree, again for each
  further change, and a flag a person cleared stays cleared only for the thing they saw;
- the phone, the flags, the office page and the stats give the same answer to "is this
  stop filmed?", and the phone is told the truth the next time it asks.

Pat (tech 20) has Jane (three appointments, finished), Bob (checked in now) and Acme
(not started). Sam (tech 21) has Ann.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
import sqlalchemy as sa

from video_helpers import OWNER, TZ, Env, film, login, make_env, minutes_ago, office_login, seed, today, video_body

from zest_video import db, jobs, videos
from zest_video.core import set_setting
from zest_video.fr import FieldRoutesError
from zest_video.http_office import board, stats
from zest_video.route import sync_day

PAT, SAM = 20, 21
JANE, BOB, ACME, ANN, CY = 501, 502, 503, 504, 505
JANES, BOBS, ACMES = (9001, 9002, 9003), 9004, 9005

REMOVALS = ["cancelled", "deleted", "marked a no-show", "moved to another day"]
HANDOVERS = ["given to Sam", "put on Sam's route"]


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


# -- what the people in the story do ---------------------------------------------------


async def ready(env: Env) -> dict[str, str]:
    """Pat signs in and his phone fetches the day."""
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.mailer.sent.clear()
    return headers


async def both(env: Env) -> tuple[dict[str, str], dict[str, str]]:
    pat = await login(env)
    sam = await login(env, "sam@example.com")
    await env.http.get("/v1/today", headers=pat)
    env.mailer.sent.clear()
    return pat, sam


def now_iso(minutes_before: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_before)).strftime("%Y-%m-%dT%H:%M:%SZ")


def take_off(env: Env, how: str, *appointment_ids: int) -> None:
    """The office takes appointments off today, one of the four ways it can."""
    tomorrow = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    for appointment_id in appointment_ids:
        if how == "cancelled":
            env.fake.data["appointment"][appointment_id]["status"] = "-1"
        elif how == "marked a no-show":
            env.fake.data["appointment"][appointment_id]["status"] = "2"
        elif how == "deleted":
            env.fake.data["appointment"].pop(appointment_id)
        else:
            env.fake.data["appointment"][appointment_id]["date"] = tomorrow


def put_back(env: Env, *appointment_ids: int) -> None:
    for appointment_id in appointment_ids:
        env.fake.data["appointment"][appointment_id] = seed()["appointment"][appointment_id]


def hand_to_sam(env: Env, how: str, *appointment_ids: int) -> None:
    for appointment_id in appointment_ids:
        if how == "given to Sam":
            env.fake.data["appointment"][appointment_id]["assignedTech"] = str(SAM)
        else:  # dragged onto Sam's route; the appointment itself names no tech
            env.fake.data["appointment"][appointment_id].update(routeID="71", assignedTech="0")


def minutes_pass(env: Env, minutes: float) -> None:
    """Make the saved list this much older, as if the clock had moved on."""
    for key in env.ctx.last_sync:
        env.ctx.last_sync[key] -= 60 * minutes


def list_is_old(env: Env) -> None:
    """The saved list is old enough to be read again. A 'FieldRoutes failed a moment ago'
    mark is left as it is: nothing in the running server wipes one, so neither do the tests."""
    for key in [k for k in env.ctx.last_sync if not k.startswith("failed:")]:
        del env.ctx.last_sync[key]


async def look_again(env: Env) -> None:
    """The server's own five-minute round: read the day again from FieldRoutes, check the
    videos against it, and chase finished stops with no video. A round that comes in the
    minute after FieldRoutes failed reads nothing, as in the running server."""
    list_is_old(env)
    try:
        await jobs.refresh_day(env.ctx)
    except FieldRoutesError:
        pass  # the job loop logs it and goes on to the next job
    await jobs.watch_route(env.ctx)
    await jobs.flag_missing(env.ctx)


async def route_flags(env: Env, *, open_only: bool = True) -> list[Any]:
    """Every flag about a video and the route, whatever its heading on the office page."""
    return [f for f in await env.flags(open_only=open_only) if f.kind in ("off_route", "second_video", "video_waiting")]


async def clear_as_office(env: Env, csrf: str, flag: Any, note: str) -> None:
    r = await env.http.post(f"/office/flag/{flag.id}/clear", data={"csrf": csrf, "note": note})
    assert r.status_code == 303


def days_ago(days: int) -> str:
    return (datetime.now(TZ).date() - timedelta(days=days)).isoformat()


def yesterday() -> str:
    return days_ago(1)


def short(day: str) -> str:
    """A day the way a flag writes it: Nov 13."""
    return datetime.strptime(day, "%Y-%m-%d").strftime("%b %-d")


def earlier_visit(env: Env, customer_id: int, tech_id: int, *, status: str = "1", day: str | None = None) -> None:
    """Appointment 9102: a visit on an earlier day (yesterday unless told otherwise)."""
    env.fake.data["appointment"][9102] = {
        "appointmentID": "9102", "officeID": "7", "customerID": str(customer_id), "routeID": "60",
        "type": "89", "status": status, "assignedTech": str(tech_id), "employeeID": "22", "date": day or yesterday(),
        "start": "08:00:00", "sequence": "1", **({"servicedBy": str(tech_id)} if status == "1" else {}),
    }


def sams_own_work_at_bobs(env: Env) -> None:
    """Sam treated Bob's lawn earlier today (9010, finished 70 minutes ago). Pat's mowing there is 9004."""
    env.fake.data["appointment"][9010] = {
        "appointmentID": "9010", "officeID": "7", "customerID": str(BOB), "routeID": "71", "type": "80", "status": "1",
        "assignedTech": str(SAM), "servicedBy": str(SAM), "employeeID": "22", "date": today(), "start": "07:00:00",
        "sequence": "1", "checkIn": minutes_ago(90), "checkOut": minutes_ago(70),
    }


async def office_rows(env: Env) -> tuple[dict[str, dict[int, Any]], dict[str, Any]]:
    """The office day page: each tech's rows by customer, and the whole board."""
    data = await board(env.ctx, today())
    return {name: {r["stop"].customer_id: r for r in rows} for name, rows in data["groups"]}, data


async def upload(env: Env, headers: dict[str, str], video_id: str, size: int = 5000) -> httpx.Response:
    env.storage.objects[(await env.video(video_id)).object_key] = b"v" * size
    r = await env.http.post(f"/v1/videos/{video_id}/complete", json={"sizeBytes": size}, headers=headers)
    assert r.status_code == 200, r.text
    return r


async def send_all(env: Env, *video_ids: str) -> None:
    """The 10 minutes pass and the emailer runs until it has nothing left."""
    for video_id in video_ids:
        await env.window_closed(video_id)
    while await videos.send_due(env.ctx):
        pass


async def video_at(env: Env, headers: dict[str, str], customer_id: int, stage: str, **changes: Any) -> str:
    """A video for a stop, brought to one stage: 'registered' (still uploading), 'held'
    (uploaded, inside the 10 minutes) or 'emailed'."""
    # Jane's visit ended 20 minutes ago, so her video was filmed back then.
    changes.setdefault("recordedAt", now_iso(30 if customer_id == JANE else 0))
    body = video_body(customer_id, **changes)
    r = await env.http.post("/v1/videos", json=body, headers=headers)
    assert r.status_code == 200, r.text
    if stage != "registered":
        await upload(env, headers, body["id"])
    if stage == "emailed":
        await send_all(env, body["id"])
        assert (await env.video(body["id"])).state == "sent"
    return body["id"]


def video_mail(env: Env) -> list[str]:
    return [m.to for m in env.mailer.sent if m.tag == "video"]


async def today_for(env: Env, headers: dict[str, str]) -> dict[str, Any]:
    r = await env.http.get("/v1/today", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


async def stop_ids(env: Env, headers: dict[str, str]) -> list[int]:
    return [s["customerId"] for s in (await today_for(env, headers))["stops"]]


async def stop_for(env: Env, headers: dict[str, str], customer_id: int) -> dict[str, Any]:
    return next(s for s in (await today_for(env, headers))["stops"] if s["customerId"] == customer_id)


async def missing(env: Env) -> set[tuple[int, int]]:
    """Open 'finished with no video' flags, as (customer, tech)."""
    return {(f.customer_id, f.tech_id) for f in await env.flags("no_video")}


def finish_in_fieldroutes(env: Env, *appointment_ids: int, by: int | None = None) -> None:
    for appointment_id in appointment_ids:
        row = env.fake.data["appointment"][appointment_id]
        row.update(status="1", checkOut=minutes_ago(1))
        if by:
            row["servicedBy"] = str(by)


# == 1. The office takes a stop off the day ============================================


@pytest.mark.parametrize("how", REMOVALS)
async def test_a_stop_taken_off_the_day_leaves_the_phone_at_its_next_fetch_and_is_not_chased(env: Env, how: str) -> None:
    pat = await ready(env)
    # Bob's stop was finished two hours ago with no video, and the office has been told.
    finish_in_fieldroutes(env, BOBS)
    await look_again(env)
    await env.finished(BOB, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(BOB, PAT)}

    take_off(env, how, BOBS)
    await look_again(env)
    assert await stop_ids(env, pat) == [JANE, ACME], "the phone's next fetch no longer lists it"
    assert await missing(env) == set(), "a stop that is not on the route any more is not chased"
    (flag,) = await env.flags("no_video", open_only=False)
    assert flag.cleared_by == "system" and "route" in flag.note, "and the record says why the flag went away"

    await env.finished(ago=timedelta(hours=2))  # even with every saved stop marked finished
    await jobs.flag_missing(env.ctx)
    assert BOB not in {customer for customer, _ in await missing(env)}


@pytest.mark.parametrize("stage", ["registered", "held"])
@pytest.mark.parametrize("how", REMOVALS)
async def test_a_video_for_a_stop_taken_off_the_day_waits_for_the_office_and_no_email_goes(
    env: Env, how: str, stage: str
) -> None:
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, stage)
    take_off(env, how, BOBS)
    await look_again(env)
    if stage == "registered":
        await upload(env, pat, video_id)  # the upload lands after the office's change
    await send_all(env, video_id)

    video = await env.video(video_id)
    assert video_mail(env) == [], "nobody is emailed about a visit the office took off the day"
    assert (video.state, video.email_skip) == ("no_email", "off_route")
    assert video.object_key in env.storage.objects and not video.object_deleted, "the video itself is kept"
    (flag,) = await env.flags("off_route")
    assert (flag.video_id, flag.customer_id, flag.tech_id) == (video_id, BOB, PAT)
    assert "Pat filmed Bob Jones" in flag.detail and "No email went out" in flag.detail
    await videos.run_outbox(env.ctx)
    assert env.fr_calls("note", "create") == [], "and nothing is written on the account until the office decides"

    body = await today_for(env, pat)
    assert BOB not in [s["customerId"] for s in body["stops"]]
    assert [(v["id"], v["customerId"], v["state"]) for v in body["videos"]] == [(video_id, BOB, "no_email")], (
        "the tech's own list of videos still has it"
    )

    # The tech is told the truth if he tries to change it: it has not gone to the customer.
    for r in (await env.http.delete(f"/v1/videos/{video_id}", headers=pat),
              await env.http.post("/v1/videos", json=video_body(BOB), headers=pat)):
        assert r.status_code == 409 and r.json()["message"] == "The office is looking at this video. Call the office to change it."

    # The same round again, and the phone repeating itself, change nothing.
    await look_again(env)
    again = await env.http.post(f"/v1/videos/{video_id}/complete", json={"sizeBytes": 5000}, headers=pat)
    assert again.status_code == 200 and again.json()["state"] == "no_email"
    await send_all(env, video_id)
    assert video_mail(env) == [] and len(await env.flags("off_route", open_only=False)) == 1
    assert len(await env.all(sa.select(db.videos))) == 1


@pytest.mark.parametrize("how", REMOVALS)
async def test_a_stop_taken_off_the_day_after_its_email_went_is_flagged_for_the_office(env: Env, how: str) -> None:
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "emailed")
    assert video_mail(env) == ["bob@example.com"]
    take_off(env, how, BOBS)
    await look_again(env)

    (flag,) = await env.flags("off_route")
    assert flag.video_id == video_id and "the customer has the video" in flag.detail
    assert (await env.video(video_id)).state == "sent", "the server does not pull it by itself: the office decides"
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"] and len(await env.flags("off_route", open_only=False)) == 1


async def test_the_office_can_send_a_held_back_video_anyway_or_pull_it(env: Env) -> None:
    pat = await ready(env)
    bobs, janes = await video_at(env, pat, BOB, "held"), await video_at(env, pat, JANE, "held")
    take_off(env, "cancelled", BOBS, *JANES)
    await look_again(env)
    await send_all(env, bobs, janes)
    assert video_mail(env) == [] and len(await env.flags("off_route")) == 2

    csrf = await office_login(env)
    page = (await env.http.get("/office")).text
    assert "Video for a stop that is no longer on the route" in page, "the flag is on top of the day page"
    assert "Videos not tied to a stop" in page and "No email: the stop is no longer on the route" in page
    assert page.count(">Send it anyway</button>") == 2, "one button for each video that is waiting"

    r = await env.http.post(f"/office/video/{bobs}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    await send_all(env, bobs)
    assert video_mail(env) == ["bob@example.com"] and (await env.video(bobs)).state == "sent"
    assert [f.video_id for f in await env.flags("off_route")] == [janes], "Bob's flag went with the decision"
    await videos.run_outbox(env.ctx)
    assert [n.one("customerID") for n in env.fr_calls("note", "create")] == [str(BOB)], "now the link is on the account"

    # Pressing it twice, or the round running again, never sends a second email.
    r = await env.http.post(f"/office/video/{bobs}/send", data={"csrf": csrf})
    assert "err=" in r.headers["location"]
    await look_again(env)
    await send_all(env, bobs)
    assert video_mail(env) == ["bob@example.com"]

    # The other choice: pull it. Its flag goes at the next round.
    r = await env.http.post(f"/office/video/{janes}/pull", data={"csrf": csrf, "reason": "the visit was cancelled"})
    assert r.status_code == 303
    await look_again(env)
    assert await env.flags("off_route") == [] and video_mail(env) == ["bob@example.com"]
    assert "video_send_anyway" in await env.actions()

    # Nobody without a session can press it.
    await env.http.post("/office/logout", data={"csrf": csrf})
    assert (await env.http.post(f"/office/video/{janes}/send", data={"csrf": csrf})).status_code == 303
    assert (await env.video(janes)).state == "pulled"


async def test_the_owner_can_switch_the_hold_off_and_the_office_is_still_told(env: Env) -> None:
    await set_setting(env.ctx, "hold_off_route", False, OWNER)
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"] and (await env.video(video_id)).state == "sent"
    await look_again(env)
    (flag,) = await env.flags("off_route")
    assert "the customer has the video" in flag.detail


async def test_send_it_anyway_does_not_promise_an_email_that_will_not_go(env: Env) -> None:
    """Acme has no email address. Letting its held-back video go files the link and emails
    nobody, so the office must not be told 'the customer gets the email in a minute'."""
    pat = await ready(env)
    video_id = await video_at(env, pat, ACME, "held")
    take_off(env, "deleted", ACMES)
    await look_again(env)
    await send_all(env, video_id)
    assert (await env.video(video_id)).email_skip == "off_route"

    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    said = unquote(r.headers["location"])
    assert "err=" not in said and "If this customer can be emailed" in said and "The day page will say" in said
    assert "gets the email" not in said
    await send_all(env, video_id)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "no_address") and video_mail(env) == []
    assert "No email: no email on file" in (await env.http.get("/office")).text, "and the day page does say"


async def test_with_customer_emails_off_the_flag_and_the_button_say_what_will_happen(tmp_path) -> None:
    """How the service is deployed today: customer emails off. Nothing can be 'sent anyway'."""
    env = await make_env(tmp_path, email_mode="off")
    try:
        pat = await ready(env)
        video_id = await video_at(env, pat, BOB, "held")
        take_off(env, "cancelled", BOBS)
        await look_again(env)
        await send_all(env, video_id)
        assert (await env.video(video_id)).email_skip == "off_route"
        (flag,) = await env.flags("off_route")
        assert "Send it anyway" not in flag.detail and "Press Keep it, or Pull" in flag.detail
        assert "No email went out and nothing was saved on the account" in flag.detail
        await videos.run_outbox(env.ctx)
        assert env.fr_calls("note", "create") == []

        csrf = await office_login(env)
        page = (await env.http.get("/office")).text
        assert page.count(">Keep it</button>") == 1 and ">Send it anyway</button>" not in page
        r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
        said = unquote(r.headers["location"])
        assert "err=" not in said and "Customer emails are off, so no email goes" in said
        await send_all(env, video_id)
        await videos.run_outbox(env.ctx)
        video = await env.video(video_id)
        assert (video.state, video.email_skip) == ("no_email", "email_off") and video_mail(env) == []
        assert len(env.fr_calls("note", "create")) == 1, "the link is saved on the account, which is what Keep it means"
        await look_again(env)
        assert await route_flags(env) == []
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


# == 2. The stop goes to another tech ==================================================


@pytest.mark.parametrize("how", HANDOVERS)
async def test_a_stop_handed_to_another_tech_moves_from_one_phone_to_the_other(env: Env, how: str) -> None:
    pat, sam = await both(env)
    hand_to_sam(env, how, BOBS)
    await look_again(env)
    assert await stop_ids(env, pat) == [JANE, ACME] and await stop_ids(env, sam) == [ANN, BOB]
    bob = await stop_for(env, sam, BOB)
    assert bob["appointmentIds"] == [BOBS] and bob["video"] is None

    # It is finished with no video: Sam owes it now, and Pat does not.
    finish_in_fieldroutes(env, BOBS, by=SAM)
    await look_again(env)
    await env.finished(BOB, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(BOB, SAM)}


@pytest.mark.parametrize("stage", ["registered", "held", "emailed"])
@pytest.mark.parametrize("how", HANDOVERS)
async def test_a_video_filmed_before_the_stop_went_to_another_tech_goes_once_and_covers_the_visit(
    env: Env, how: str, stage: str
) -> None:
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, stage)
    hand_to_sam(env, how, BOBS)
    await look_again(env)
    if stage == "registered":
        await upload(env, pat, video_id)
    await send_all(env, video_id)
    await look_again(env)

    assert video_mail(env) == ["bob@example.com"], "one email, to the customer who was filmed"
    (flag,) = await env.flags("off_route")
    assert flag.video_id == video_id and "Pat filmed Bob Jones" in flag.detail and "under Sam" in flag.detail

    # Sam's phone shows the visit as filmed, so he does not send the customer a second video...
    assert (await stop_for(env, sam, BOB))["video"] == {"id": video_id, "state": "sent", "byYou": False}
    assert BOB not in await stop_ids(env, pat)
    assert [v["id"] for v in (await today_for(env, pat))["videos"]] == [video_id], "Pat still sees his own video"

    # ...and when Sam closes the visit out in FieldRoutes, nobody is chased for one.
    finish_in_fieldroutes(env, BOBS, by=SAM)
    await look_again(env)
    await env.finished(BOB, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == set()

    # The office page says the same: Pat's video sits on Sam's row, and Sam has missed nothing.
    data = await board(env.ctx, today())
    rows = {name: {r["stop"].customer_id: r for r in rows} for name, rows in data["groups"]}
    assert rows["Sam Ortiz"][BOB]["video"].id == video_id and rows["Sam Ortiz"][BOB]["label"] == "Sent"
    assert data["others"] == [], "not in the pile of videos tied to no stop"
    assert {r["name"]: r["missed"] for r in await stats(env.ctx, 1)}.get("Sam Ortiz", 0) == 0
    assert video_mail(env) == ["bob@example.com"]


BOTH_FILMED = [
    "the new tech's video goes out, then the first tech's phone finds signal",
    "the first tech's video was still uploading when the stop changed hands",
    "both videos are inside their 10 minutes",
    "the first tech's video had already gone out, and the new tech films anyway",
]


@pytest.mark.parametrize("order", BOTH_FILMED)
async def test_when_a_stop_changes_hands_and_both_techs_film_it_the_customer_gets_one_email(env: Env, order: str) -> None:
    pat, sam = await both(env)
    if order == BOTH_FILMED[0]:
        hand_to_sam(env, "given to Sam", BOBS)
        await look_again(env)
        # Pat has no signal and an old list: he films Bob, and the video stays on his phone.
        pats_body = video_body(BOB, appointmentIds=[BOBS], recordedAt=now_iso(40))
        assert (await stop_for(env, sam, BOB))["video"] is None
        sams = await video_at(env, sam, BOB, "emailed", recordedAt=now_iso(20))
        assert (await env.http.post("/v1/videos", json=pats_body, headers=pat)).status_code == 200  # signal at last
        pats = pats_body["id"]
        await upload(env, pat, pats)
        goes, waits = sams, pats
    elif order == BOTH_FILMED[1]:
        pats = await video_at(env, pat, BOB, "registered")
        hand_to_sam(env, "given to Sam", BOBS)
        await look_again(env)
        assert (await stop_for(env, sam, BOB))["video"] is None, "a video still on Pat's phone does not count yet"
        sams = await video_at(env, sam, BOB, "emailed")
        await upload(env, pat, pats)
        goes, waits = sams, pats
    elif order == BOTH_FILMED[2]:
        pats = await video_at(env, pat, BOB, "held")
        hand_to_sam(env, "given to Sam", BOBS)
        await look_again(env)
        sams = await video_at(env, sam, BOB, "held")  # his phone shows Pat's, and he films his own anyway
        goes, waits = sams, pats
    else:
        pats = await video_at(env, pat, BOB, "emailed")
        hand_to_sam(env, "given to Sam", BOBS)
        await look_again(env)
        assert (await stop_for(env, sam, BOB))["video"] == {"id": pats, "state": "sent", "byYou": False}
        sams = await video_at(env, sam, BOB, "held")
        goes, waits = pats, sams
    await look_again(env)  # a round while one of them is still to be decided
    assert await env.flags("second_video") == [], "nothing is said about two videos before there are two to send"
    for _ in range(2):  # the emailer and the rounds, twice over
        await send_all(env, pats, sams)
        await look_again(env)

    assert video_mail(env) == ["bob@example.com"], "one visit, one email"
    assert (await env.video(goes)).state == "sent"
    waiting = await env.video(waits)
    assert (waiting.state, waiting.email_skip) == ("no_email", "second_video"), "the other one waits for the office"
    assert waiting.object_key in env.storage.objects and not waiting.object_deleted, "and is kept"
    (flag,) = await env.flags("second_video")
    assert flag.video_id == waits and "No email went out" in flag.detail
    assert "Pat" in flag.detail and "Sam" in flag.detail and "Bob Jones" in flag.detail
    assert len(await env.flags("second_video", open_only=False)) == 1
    await videos.run_outbox(env.ctx)
    assert len(env.fr_calls("note", "create")) == 1, "one link on the account, not two"
    assert (await stop_for(env, sam, BOB))["video"]["state"] in ("sent", "no_email")

    # The office reads the flag and decides. Sending it is the office's say-so for a second email.
    csrf = await office_login(env)
    page = (await env.http.get("/office")).text
    assert "Two videos for one visit" in page and "No email: a second video for this visit" in page
    assert page.count(">Send it anyway</button>") == 1
    r = await env.http.post(f"/office/video/{waits}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    await send_all(env, waits)
    await look_again(env)
    assert video_mail(env) == ["bob@example.com"] * 2 and (await env.video(waits)).state == "sent"
    assert await env.flags("second_video") == []


async def test_with_the_hold_switched_off_both_videos_go_and_the_office_is_told(env: Env) -> None:
    await set_setting(env.ctx, "hold_off_route", False, OWNER)
    pat, sam = await both(env)
    pats = await video_at(env, pat, BOB, "emailed")
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    sams = await video_at(env, sam, BOB, "emailed")
    await look_again(env)
    assert video_mail(env) == ["bob@example.com"] * 2, "the owner's switch: nothing is held for the route"
    (two,) = await env.flags("second_video")
    assert two.video_id == sams and "Pat's video for the same visit went out too" in two.detail
    (handover,) = await env.flags("off_route")
    assert handover.video_id == pats and "under Sam" in handover.detail


async def test_a_second_video_held_back_goes_by_itself_when_the_other_one_is_deleted(env: Env) -> None:
    pat, sam = await both(env)
    pats = await video_at(env, pat, BOB, "held")
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    sams = await video_at(env, sam, BOB, "held")
    await send_all(env, pats)
    assert (await env.video(pats)).email_skip == "second_video" and video_mail(env) == []
    # Sam thinks better of it and deletes his, inside his 10 minutes. Pat's is the only one left.
    assert (await env.http.delete(f"/v1/videos/{sams}", headers=sam)).status_code == 200
    await look_again(env)
    assert await env.flags("second_video") == [] and (await env.video(pats)).state == "held"
    await send_all(env, pats)
    await look_again(env)
    assert video_mail(env) == ["bob@example.com"]
    (flag,) = await route_flags(env)
    assert flag.kind == "off_route" and "under Sam" in flag.detail and "stands for the visit" in flag.detail


async def test_pulling_the_first_techs_video_after_a_handover_means_the_new_tech_owes_his_own(env: Env) -> None:
    """The handover flag ends 'Pull it if that is wrong'. Pulling it has to mean something:
    the new tech is asked for a video, and chased like anyone else if he sends none."""
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, "emailed")
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    (flag,) = await env.flags("off_route")
    assert "Sam is not asked for a second one. Pull it if that is wrong." in flag.detail
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/pull", data={"csrf": csrf, "reason": "Sam must film his own"})
    assert r.status_code == 303

    finish_in_fieldroutes(env, BOBS, by=SAM)
    await look_again(env)
    await env.finished(BOB, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert (await stop_for(env, sam, BOB))["video"] is None, "Sam's phone asks for a video"
    assert await missing(env) == {(BOB, SAM)}, "the office is told when he finishes without one"
    rows, data = await office_rows(env)
    assert (rows["Sam Ortiz"][BOB]["video"], rows["Sam Ortiz"][BOB]["label"]) == (None, "No video")
    assert [(o["video"].id, o["label"]) for o in data["others"]] == [(video_id, "Pulled")], "the pulled one is still listed"
    assert {r["name"]: r["missed"] for r in await stats(env.ctx, 1)}["Sam Ortiz"] == 1, "and the stats count the miss"
    assert await route_flags(env) == [], "the flag about the pulled video went with it"

    sams = await video_at(env, sam, BOB, "emailed")
    assert await missing(env) == set() and (await stop_for(env, sam, BOB))["video"]["id"] == sams


@pytest.mark.parametrize("registered", ["before the stop changed hands", "after it, from a stale list"])
async def test_a_video_still_on_the_first_techs_phone_does_not_count_for_the_new_tech_until_it_lands(
    env: Env, registered: str
) -> None:
    pat, sam = await both(env)
    if registered == "before the stop changed hands":
        pats = await video_at(env, pat, BOB, "registered")  # registered, and stuck on Pat's phone
    hand_to_sam(env, "given to Sam", BOBS)
    finish_in_fieldroutes(env, BOBS, by=SAM)
    await look_again(env)
    await env.finished(BOB, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    if registered != "before the stop changed hands":
        assert await missing(env) == {(BOB, SAM)}
        pats = await video_at(env, pat, BOB, "registered", appointmentIds=[BOBS])
    assert await missing(env) == {(BOB, SAM)}, "a video that may never arrive does not answer for Sam"
    assert (await stop_for(env, sam, BOB))["video"] is None, "and Sam's phone still asks"
    rows, _ = await office_rows(env)
    assert rows["Sam Ortiz"][BOB]["label"] == "No video"

    await upload(env, pat, pats)  # it lands
    assert (await stop_for(env, sam, BOB))["video"] == {"id": pats, "state": "held", "byYou": False}
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == set(), "now it counts, and the flag goes on the next round"
    rows, _ = await office_rows(env)
    assert rows["Sam Ortiz"][BOB]["video"].id == pats

    # Pat deletes it inside his 10 minutes: the stop has no video again.
    assert (await env.http.delete(f"/v1/videos/{pats}", headers=pat)).status_code == 200
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(BOB, SAM)} and (await stop_for(env, sam, BOB))["video"] is None
    assert len(await env.flags("no_video", open_only=False)) == 1


HANDOVER_ORDER = ["the office is told about Sam's missing video first", "the work is moved to Sam first"]


@pytest.mark.parametrize("order", HANDOVER_ORDER)
async def test_work_moved_to_a_tech_with_work_of_his_own_at_the_stop_gives_one_answer_everywhere(
    env: Env, order: str
) -> None:
    """Sam did his own job at Bob's and did not film it. Pat mowed and filmed. Then the
    office puts Pat's mowing under Sam. Pat's video is not Sam's video for his own job:
    the phone, the flag, the office page and the stats all say Sam still owes one,
    whichever came first."""
    sams_own_work_at_bobs(env)
    pat, sam = await both(env)
    pats = await video_at(env, pat, BOB, "emailed")

    async def chase() -> None:
        await env.finished(BOB, ago=timedelta(hours=2), force=False)
        await jobs.flag_missing(env.ctx)

    if order == HANDOVER_ORDER[0]:
        await chase()
        assert await missing(env) == {(BOB, SAM)}
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    await chase()

    bob = await stop_for(env, sam, BOB)
    assert bob["appointmentIds"] == [BOBS, 9010] and bob["video"] is None
    assert await missing(env) == {(BOB, SAM)}
    rows, data = await office_rows(env)
    assert (rows["Sam Ortiz"][BOB]["video"], rows["Sam Ortiz"][BOB]["label"]) == (None, "No video")
    assert [o["video"].id for o in data["others"]] == [pats]
    assert {r["name"]: r["missed"] for r in await stats(env.ctx, 1)}["Sam Ortiz"] == 1
    (flag,) = await env.flags("off_route")
    assert flag.video_id == pats and "under Sam" in flag.detail and "still asked for his own video" in flag.detail
    assert "not asked for a second one" not in flag.detail

    # Sam films his own. Two techs worked there, so the customer gets one from each.
    sams = await video_at(env, sam, BOB, "emailed", recordedAt=now_iso(80))
    assert video_mail(env) == ["bob@example.com"] * 2 and await missing(env) == set()
    assert await env.flags("second_video") == [] and (await stop_for(env, sam, BOB))["video"]["id"] == sams


async def test_the_handover_flag_names_the_tech_who_has_the_filmed_work_now(env: Env) -> None:
    """Three techs at one property. Lee has his own appointment there and is listed first;
    the work Pat filmed went to Sam. The flag must say Sam."""
    env.fake.data["employee"][24] = {
        "employeeID": "24", "officeID": "7", "fname": "Lee", "lname": "Park", "type": "1", "active": "1",
        "email": "lee@example.com", "phone": "",
    }
    env.fake.data["appointment"][9000] = {
        "appointmentID": "9000", "officeID": "7", "customerID": str(BOB), "routeID": "70", "type": "80", "status": "0",
        "assignedTech": "24", "employeeID": "22", "date": today(), "start": "07:30:00", "sequence": "1",
    }
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, "emailed")
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    (flag,) = await env.flags("off_route")
    assert flag.video_id == video_id and "under Sam" in flag.detail and "Lee" not in flag.detail
    assert (await stop_for(env, sam, BOB))["video"] == {"id": video_id, "state": "sent", "byYou": False}


async def test_an_open_flag_keeps_its_words_true_when_the_route_moves_on(env: Env) -> None:
    """Pat's video stands for the visit Sam was given. Then the office adds a job of Sam's
    own at that stop: Pat's video no longer answers for him, and the flag that is still
    open must not go on saying that it does."""
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, "emailed")
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    (flag,) = await env.flags("off_route")
    assert "Sam is not asked for a second one" in flag.detail
    assert (await stop_for(env, sam, BOB))["video"] == {"id": video_id, "state": "sent", "byYou": False}

    sams_own_work_at_bobs(env)
    await look_again(env)
    (same,) = await env.flags("off_route")
    assert same.id == flag.id and "still asked for his own video" in same.detail
    assert "not asked for a second one" not in same.detail
    assert (await stop_for(env, sam, BOB))["video"] is None and len(await route_flags(env, open_only=False)) == 1


async def test_a_missing_video_flag_follows_the_stop_to_whoever_has_it_now(env: Env) -> None:
    pat, sam = await both(env)
    finish_in_fieldroutes(env, BOBS)
    await look_again(env)
    await env.finished(BOB, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(BOB, PAT)}

    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    assert await missing(env) == {(BOB, SAM)}, "Pat is no longer asked for a stop that is not his"

    # The office changes its mind and gives it back.
    env.fake.data["appointment"][BOBS]["assignedTech"] = str(PAT)
    await look_again(env)
    assert await missing(env) == {(BOB, PAT)}, "the flag for Pat comes back; Sam's goes"
    assert len(await env.flags("no_video", open_only=False)) == 2, "one flag each, however often the stop moves"

    # A flag a person cleared stays cleared, whatever the route does next.
    (flag,) = await env.flags("no_video")
    await env.run(sa.update(db.flags).where(db.flags.c.id == flag.id).values(cleared_at=db.now(), cleared_by=OWNER))
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    env.fake.data["appointment"][BOBS]["assignedTech"] = str(PAT)
    await look_again(env)
    assert await missing(env) == set()


async def test_a_video_that_stands_in_for_the_leads_shows_on_the_leads_phone_too(env: Env) -> None:
    """The server already took a helper's video as covering the stop for the lead (no flag).
    The lead's phone now says the same, so he is not left thinking it still has to be filmed."""
    env.fake.data["appointment"][9001]["additionalTechs"] = str(SAM)  # Sam rides along at Jane's
    pat, sam = await both(env)
    sams = await video_at(env, sam, JANE, "held")
    assert (await stop_for(env, pat, JANE))["video"] == {"id": sams, "state": "held", "byYou": False}
    # He may still film his own, and then he sees his own.
    pats = await video_at(env, pat, JANE, "held")
    assert (await stop_for(env, pat, JANE))["video"] == {"id": pats, "state": "held", "byYou": True}
    assert (await env.video(sams)).state == "held"


async def test_a_video_the_office_put_on_a_customer_by_hand_is_not_second_guessed_by_the_route(env: Env) -> None:
    pat = await ready(env)
    video_id = await film(env, pat, None)  # Not On My List
    csrf = await office_login(env)
    # Cy has no visit today at all. The office knows that and matches the video to him anyway.
    r = await env.http.post(f"/office/video/{video_id}/move", data={"csrf": csrf, "customer_id": str(CY)})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    await look_again(env)
    await send_all(env, video_id)
    await look_again(env)
    assert video_mail(env) == ["cy@example.com"] and (await env.video(video_id)).state == "sent"
    assert await env.flags("off_route") == []


async def test_a_missing_video_flag_comes_back_when_the_video_that_cleared_it_is_deleted(env: Env) -> None:
    pat = await ready(env)
    await env.finished(JANE, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(JANE, PAT)}
    video_id = await video_at(env, pat, JANE, "held")
    assert await missing(env) == set()
    assert (await env.http.delete(f"/v1/videos/{video_id}", headers=pat)).status_code == 200  # inside his 10 minutes
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(JANE, PAT)}, "the stop has no video again, and the office hears of it again"
    assert len(await env.flags("no_video", open_only=False)) == 1


# == 3. A stop is added mid-day ========================================================


def add_cy(env: Env, **changes: Any) -> None:
    env.fake.data["appointment"][9020] = {
        "appointmentID": "9020", "officeID": "7", "customerID": str(CY), "routeID": "70", "type": "89", "status": "0",
        "assignedTech": str(PAT), "employeeID": "22", "date": today(), "start": "08:30:00", "sequence": "9", **changes,
    }


async def test_a_stop_added_mid_day_is_on_the_phone_at_its_next_fetch_for_one_extra_read(env: Env) -> None:
    pat = await ready(env)
    add_cy(env)
    before = len(env.fake.requests)
    await look_again(env)
    new = env.fake.requests[before:]
    assert [(r.entity, r.action) for r in new] == [("appointment", "search"), ("route", "search"), ("customer", "get")]
    assert new[-1].form["customerIDs[]"] == [str(CY)], "only the customer we had not seen today is read"

    assert await stop_ids(env, pat) == [JANE, CY, BOB, ACME], "in FieldRoutes' order, not at the end"
    cy = await stop_for(env, pat, CY)
    assert (cy["name"], cy["address"], cy["appointmentIds"], cy["done"], cy["video"]) == (
        "Cy Young", "77 Elm St", [9020], False, None,
    )
    video_id = await video_at(env, pat, CY, "emailed")
    assert video_mail(env) == ["cy@example.com"] and (await env.video(video_id)).appointment_ids == [9020]


async def test_a_stop_filmed_before_it_was_on_the_list_is_matched_by_the_office_and_then_counts(env: Env) -> None:
    pat = await ready(env)
    video_id = await film(env, pat, None, spokenText="This is Pat at the Young home on Elm.")  # Not On My List
    add_cy(env, status="1", checkIn=minutes_ago(30), checkOut=minutes_ago(10))
    await look_again(env)

    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/move", data={"csrf": csrf, "customer_id": str(CY)})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    await send_all(env, video_id)
    await look_again(env)
    assert video_mail(env) == ["cy@example.com"]
    assert (await stop_for(env, pat, CY))["video"] == {"id": video_id, "state": "sent", "byYou": True}
    await env.finished(CY, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == set() and await env.flags("off_route") == []


# == 4. The route is reshuffled: times and order, nothing else ==========================


async def test_a_reshuffled_route_changes_the_order_on_the_phone_and_nothing_else(env: Env) -> None:
    pat = await ready(env)
    emailed = await video_at(env, pat, JANE, "emailed")
    held = await video_at(env, pat, BOB, "held")
    waiting = await video_at(env, pat, ACME, "registered")
    async def every_video() -> list[tuple[Any, ...]]:
        rows = await env.all(sa.select(db.videos))
        return sorted((v.id, v.state, v.customer_id, v.appointment_ids, v.token) for v in rows)

    snapshot = await every_video()
    before = await today_for(env, pat)

    env.fake.data["appointment"][ACMES]["start"] = "07:00:00"  # Acme first now
    env.fake.data["appointment"][BOBS].update(start="13:00:00", sequence="7")
    for appointment_id in JANES:
        env.fake.data["appointment"][appointment_id]["start"] = "09:30:00"
    calls = len(env.fake.requests)
    await look_again(env)
    assert [(r.entity, r.action) for r in env.fake.requests[calls:]] == [("appointment", "search"), ("route", "search")]

    after = await today_for(env, pat)
    assert [s["customerId"] for s in after["stops"]] == [ACME, JANE, BOB]

    def unordered(body: dict[str, Any]) -> dict[int, dict[str, Any]]:
        return {s["customerId"]: {k: v for k, v in s.items() if k != "order"} for s in body["stops"]}

    assert unordered(after) == unordered(before), "same stops, same appointments, same videos"
    assert await every_video() == snapshot
    assert await env.flags() == [] and video_mail(env) == ["jane@example.com"]

    await upload(env, pat, waiting)
    await send_all(env, held, waiting)
    assert (await env.video(emailed)).state == "sent" and (await env.video(held)).state == "sent"
    assert video_mail(env) == ["jane@example.com", "bob@example.com"], "each video still goes to its own customer, once"


# == 5. The office changes its mind ====================================================


async def test_a_stop_taken_off_and_put_back_sends_the_waiting_video_by_itself(env: Env) -> None:
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == [] and len(await env.flags("off_route")) == 1

    put_back(env, BOBS)
    await look_again(env)
    assert await env.flags("off_route") == [], "the flag clears itself"
    assert (await env.video(video_id)).state == "held"
    assert await videos.send_due(env.ctx) == 1
    assert video_mail(env) == ["bob@example.com"] and (await env.video(video_id)).state == "sent"
    assert (await stop_for(env, pat, BOB))["video"] == {"id": video_id, "state": "sent", "byYou": True}

    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"], "once"
    assert "video_released" in await env.actions()


async def test_taken_off_put_back_handed_over_and_taken_off_again_in_one_afternoon_is_one_email(env: Env) -> None:
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, "emailed")

    async def open_flags() -> list[str]:
        return [f.detail for f in await env.flags("off_route")]

    take_off(env, "cancelled", BOBS)
    await look_again(env)
    assert len(await open_flags()) == 1
    put_back(env, BOBS)
    await look_again(env)
    assert await open_flags() == []
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    (words,) = await open_flags()
    assert "under Sam" in words, "the flag says what is true now, not what was true an hour ago"
    put_back(env, BOBS)
    await look_again(env)
    assert await open_flags() == []
    take_off(env, "moved to another day", BOBS)
    await look_again(env)
    (words,) = await open_flags()
    assert "the customer has the video" in words

    every = await env.flags("off_route", open_only=False)
    assert len(every) == 3 and len([f for f in every if f.cleared_at is None]) == 1, (
        "a flag for each of the three things that happened, each closed when it stopped being true: "
        "one open at a time, never a pile"
    )
    for _ in range(3):  # the rounds keep running and add nothing
        await look_again(env)
    assert len(await env.flags("off_route", open_only=False)) == 3 and len(await open_flags()) == 1
    assert video_mail(env) == ["bob@example.com"] and (await env.video(video_id)).state == "sent"
    assert len(await env.all(sa.select(db.videos))) == 1


async def test_handed_to_another_tech_and_back_before_the_email_goes_as_if_nothing_happened(env: Env) -> None:
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, "held")
    hand_to_sam(env, "put on Sam's route", BOBS)
    await look_again(env)
    assert BOB in await stop_ids(env, sam)
    put_back(env, BOBS)
    await look_again(env)
    await send_all(env, video_id)
    await look_again(env)
    assert video_mail(env) == ["bob@example.com"] and await env.flags() == []
    assert (await stop_for(env, pat, BOB))["video"] == {"id": video_id, "state": "sent", "byYou": True}
    assert BOB not in await stop_ids(env, sam)


async def test_a_second_change_gets_a_flag_of_its_own_even_after_a_person_cleared_the_first(env: Env) -> None:
    """Twice in a day. The stop goes to Sam after Pat's video was emailed; the office reads
    that flag and clears it. Later the visit is cancelled outright. That is a new thing,
    and a flag a person cleared is cleared for the thing they saw, not for the video."""
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, "emailed")
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    (first,) = await env.flags("off_route")
    assert "under Sam" in first.detail
    csrf = await office_login(env)
    await clear_as_office(env, csrf, first, "fine, Pat did it")
    for _ in range(2):
        await look_again(env)
    assert await route_flags(env) == [], "while nothing changes, a flag a person cleared stays cleared"

    take_off(env, "cancelled", BOBS)  # the second change
    for _ in range(2):
        await look_again(env)
    (second,) = await env.flags("off_route")
    assert second.id != first.id and second.video_id == video_id
    assert "the customer has the video" in second.detail and "no longer has that visit" in second.detail
    await clear_as_office(env, csrf, second, "called the customer")
    for _ in range(2):
        await look_again(env)
    assert await route_flags(env) == []

    # Put back and taken off once more: the visit was on again in between, so this is told again.
    put_back(env, BOBS)
    await look_again(env)
    assert await route_flags(env) == []
    take_off(env, "deleted", BOBS)
    await look_again(env)
    (third,) = await env.flags("off_route")
    assert third.id not in (first.id, second.id)
    kept = {f.id: (f.cleared_by, f.note) for f in await env.flags("off_route", open_only=False)}
    assert kept[first.id] == (OWNER, "fine, Pat did it") and kept[second.id] == (OWNER, "called the customer"), (
        "who cleared the earlier flags, and their notes, stay on record"
    )
    assert video_mail(env) == ["bob@example.com"]


async def test_clearing_the_flag_on_a_held_back_video_is_not_a_yes(env: Env) -> None:
    """The office reads 'No email went out. Press Send it anyway, or Pull', decides not to
    send, and presses Clear. To a person that reads as dealt with. If the visit then comes
    back, the video must not go out by itself behind their back."""
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    await look_again(env)
    await send_all(env, video_id)
    (first,) = await env.flags("off_route")
    csrf = await office_login(env)
    await clear_as_office(env, csrf, first, "do not send this one")

    put_back(env, BOBS)
    for _ in range(2):
        await look_again(env)
        await send_all(env, video_id)
    video = await env.video(video_id)
    assert video_mail(env) == [] and (video.state, video.email_skip) == ("no_email", "off_route")
    (waiting,) = await route_flags(env)
    assert waiting.kind == "video_waiting" and waiting.video_id == video_id
    assert "nothing goes out until the office says so" in waiting.detail and "Send it anyway" in waiting.detail
    page = (await env.http.get("/office")).text
    assert "Video still waiting for the office" in page and page.count(">Send it anyway</button>") == 1
    assert "video_released" not in await env.actions()

    # The visit is taken off again. The video was held back all along; the office is told
    # where things stand now, with a flag of its own.
    take_off(env, "cancelled", BOBS)
    await look_again(env)
    await send_all(env, video_id)
    (again,) = await route_flags(env)
    assert again.kind == "off_route" and again.id != first.id and "No email went out" in again.detail
    assert video_mail(env) == []

    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    await send_all(env, video_id)
    await look_again(env)
    assert video_mail(env) == ["bob@example.com"] and await route_flags(env) == []


async def test_a_video_let_go_and_held_back_again_is_told_to_the_office_again(env: Env) -> None:
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert len(await env.flags("off_route")) == 1
    put_back(env, BOBS)
    await look_again(env)  # nobody touched the flag, so the video is let go
    assert (await env.video(video_id)).state == "held" and await route_flags(env) == []
    take_off(env, "marked a no-show", BOBS)  # and off again before the emailer's next pass
    await look_again(env)
    await send_all(env, video_id)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "off_route") and video_mail(env) == []
    assert len(await env.flags("off_route")) == 1 and len(await env.flags("off_route", open_only=False)) == 2


# == 6. The phone has a stale list =====================================================


@pytest.mark.parametrize("how", REMOVALS)
async def test_a_phone_with_a_stale_list_can_still_hand_in_a_video_for_a_stop_removed_hours_ago(
    env: Env, how: str
) -> None:
    pat = await ready(env)  # the list was fetched in the morning; no signal since
    take_off(env, how, BOBS)
    await look_again(env)

    body = video_body(BOB, appointmentIds=[BOBS], recordedAt=now_iso())
    for _ in range(2):  # the phone finds signal and sends, then sends again to be sure
        r = await env.http.post("/v1/videos", json=body, headers=pat)
        assert r.status_code == 200 and r.json()["state"] == "awaiting_upload", "taken, not refused: the video is not lost"
    await upload(env, pat, body["id"])
    await upload(env, pat, body["id"])
    await send_all(env, body["id"])

    video = await env.video(body["id"])
    assert (video.customer_id, video.state, video.email_skip) == (BOB, "no_email", "off_route")
    assert video.object_key in env.storage.objects
    assert video_mail(env) == [] and len(await env.flags("off_route")) == 1
    assert len(await env.all(sa.select(db.videos))) == 1


async def test_a_phone_with_a_stale_list_films_a_stop_that_now_belongs_to_another_tech(env: Env) -> None:
    pat, sam = await both(env)
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)

    body = video_body(BOB, appointmentIds=[BOBS], recordedAt=now_iso())
    for _ in range(2):
        assert (await env.http.post("/v1/videos", json=body, headers=pat)).status_code == 200
    await upload(env, pat, body["id"])
    await send_all(env, body["id"])
    await look_again(env)

    video = await env.video(body["id"])
    assert (video.state, video.appointment_ids, video.customer_name) == ("sent", [BOBS], "Bob Jones")
    assert video_mail(env) == ["bob@example.com"], "Pat was there and filmed it: the customer gets it, once"
    (flag,) = await env.flags("off_route")
    assert "under Sam" in flag.detail, "and the office is told the route says otherwise"
    assert (await stop_for(env, sam, BOB))["video"] == {"id": body["id"], "state": "sent", "byYou": False}


async def test_a_stale_phone_that_names_no_appointments_cannot_tie_its_video_to_another_techs_work(env: Env) -> None:
    """The app sends the appointments its list had. A client that sends none, for a stop
    that is no longer this tech's, must not be guessed onto whatever is at the stop now."""
    pat, sam = await both(env)
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    video_id = await video_at(env, pat, BOB, "held", appointmentIds=[])
    await send_all(env, video_id)
    await look_again(env)

    video = await env.video(video_id)
    assert video.appointment_ids == [] and (video.state, video.email_skip) == ("no_email", "off_route")
    assert video_mail(env) == [], "nobody can say what he filmed, so the office decides"
    (flag,) = await route_flags(env)
    assert "no longer has work for Pat at that stop" in flag.detail and "No email went out" in flag.detail
    assert (await stop_for(env, sam, BOB))["video"] is None, "and it does not count as Sam's video"


# == 7. FieldRoutes is down, or slow, at the moment of the change ======================


class Outage:
    """FieldRoutes answers every call with an error, or hangs until let go."""

    def __init__(self, env: Env, *, hang: bool = False):
        self.env, self.hang, self.calls = env, hang, 0
        self.asked: list[tuple[str, ...]] = []  # (entity, action) of every call that reached FieldRoutes
        self.gate = asyncio.Event()
        self.transport = env.ctx.fr.client._http._transport
        self.real = self.transport.handler

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.asked.append(tuple(request.url.path.split("/")[-2:]))
        if self.hang:
            await self.gate.wait()
        return httpx.Response(200, json={"success": False, "errorMessage": "FieldRoutes is not answering"})

    def __enter__(self) -> "Outage":
        self.transport.handler = self.handler
        return self

    def __exit__(self, *exc: Any) -> None:
        self.gate.set()
        self.transport.handler = self.real


async def test_a_change_made_while_fieldroutes_was_down_is_still_caught_before_the_email(env: Env) -> None:
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)  # the office cancels it; our next read of the day fails
    minutes_pass(env, 4)

    with Outage(env) as outage:
        assert await stop_ids(env, pat) == [JANE, BOB, ACME], "the phone keeps working from the saved list"
        await send_all(env, video_id)
        assert outage.calls > 0
        assert video_mail(env) == [] and (await env.video(video_id)).state == "held", "with FieldRoutes down the email waits"

    # FieldRoutes is back, and the emailer's next pass comes inside the minute in which it is
    # being left alone after that failure. The day cannot be checked, so the email still waits.
    asked = len(env.fake.requests)
    await send_all(env, video_id)
    await look_again(env)
    assert len(env.fake.requests) == asked, "nothing is asked of FieldRoutes inside that minute"
    assert video_mail(env) == [] and (await env.video(video_id)).state == "held", (
        "no email goes out on a list nobody could check"
    )

    minutes_pass(env, 2)  # the minute is over: the day is read, and the cancellation is seen first
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == [] and (await env.video(video_id)).email_skip == "off_route"
    assert len(await env.flags("off_route")) == 1 and BOB not in await stop_ids(env, pat)


async def test_one_failed_read_of_the_day_by_anyone_makes_the_email_wait_until_the_day_can_be_checked(env: Env) -> None:
    """One bad answer from FieldRoutes, on a phone's fetch a moment before the email is due.
    FieldRoutes is fine again at once, but it is left alone for a minute after a failure,
    so the emailer cannot check the day. It must not send on the old list instead."""
    pat = await ready(env)
    cancelled, still_on = await video_at(env, pat, BOB, "held"), await video_at(env, pat, JANE, "held")
    take_off(env, "cancelled", BOBS)
    minutes_pass(env, 4)  # the saved list is four minutes old
    env.fake.fail_with = "one bad answer"
    assert await stop_ids(env, pat) == [JANE, BOB, ACME]  # that one read fails; the phone gets the saved list

    asked = len(env.fake.requests)
    await send_all(env, cancelled, still_on)
    assert video_mail(env) == [], "neither goes: nobody could say which visits are still on the day"
    assert {(await env.video(v)).state for v in (cancelled, still_on)} == {"held"}
    assert ("appointment", "search") not in [(r.entity, r.action) for r in env.fake.requests[asked:]], (
        "and FieldRoutes was not asked for the day again inside its minute"
    )

    minutes_pass(env, 2)
    await send_all(env, cancelled, still_on)
    assert video_mail(env) == ["jane@example.com"], "the visit that is still on goes, a little late"
    assert (await env.video(cancelled)).email_skip == "off_route" and video_mail(env).count("bob@example.com") == 0
    (flag,) = await env.flags("off_route")
    assert flag.video_id == cancelled and "No email went out" in flag.detail


async def test_a_failed_read_at_the_moment_the_email_is_due_makes_it_wait_too(env: Env) -> None:
    """The emailer's own read of the day fails while the rest of FieldRoutes answers. The
    server cannot know whether the visit is still on, so it does not send; it tries again."""
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    list_is_old(env)
    env.fake.fail_with = "one bad answer"
    await send_all(env, video_id)
    assert video_mail(env) == [] and (await env.video(video_id)).state == "held"

    minutes_pass(env, 2)
    await send_all(env, video_id)
    assert video_mail(env) == [] and (await env.video(video_id)).email_skip == "off_route"
    (flag,) = await env.flags("off_route")
    assert flag.video_id == video_id and "No email went out" in flag.detail


async def test_with_the_hold_switched_off_a_failed_read_does_not_keep_an_email_back(env: Env) -> None:
    """The owner's switch says: do not hold emails for the route. Then a route that cannot
    be read holds nothing either, and the office is told afterwards as before."""
    await set_setting(env.ctx, "hold_off_route", False, OWNER)
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    list_is_old(env)
    env.fake.fail_with = "one bad answer"
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"]

    minutes_pass(env, 2)
    await look_again(env)
    (flag,) = await env.flags("off_route")
    assert flag.video_id == video_id and "the customer has the video" in flag.detail


@pytest.mark.parametrize("server", ["has been running all day", "has just restarted"])
async def test_a_slow_fieldroutes_does_not_hold_up_the_phones(env: Env, server: str) -> None:
    pat, sam = await both(env)
    if server == "has just restarted":
        # A restart or a deploy: the day's stops are still in the database, but what the
        # server remembered about its last read of FieldRoutes is gone.
        env.ctx.last_sync.clear()
    else:
        minutes_pass(env, 30)  # the saved list is old enough that the next phone to ask goes to FieldRoutes

    with Outage(env, hang=True) as outage:
        first = asyncio.create_task(env.http.get("/v1/today", headers=pat))  # this one is asking FieldRoutes
        try:
            for _ in range(500):  # until that request has reached FieldRoutes and is stuck there
                if outage.calls:
                    break
                await asyncio.sleep(0.01)
            assert outage.calls == 1 and not first.done()
            # Everyone else is served the saved list at once, and can still hand in a video.
            r = await asyncio.wait_for(env.http.get("/v1/today", headers=sam), timeout=5)
            assert r.status_code == 200 and [s["customerId"] for s in r.json()["stops"]] == [ANN]
            r = await asyncio.wait_for(env.http.post("/v1/videos", json=video_body(BOB), headers=pat), timeout=5)
            assert r.status_code == 200
        finally:
            outage.gate.set()
            answer = await first
        assert answer.status_code == 200 and [s["customerId"] for s in answer.json()["stops"]] == [JANE, BOB, ACME]

        # FieldRoutes has just failed. For the next minute phones get the saved list
        # without anyone asking it again.
        asked = outage.calls
        for headers in (pat, sam, pat):
            assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
        assert outage.calls == asked

        # After that minute the next phone tries again, and the office can force a look at any time.
        minutes_pass(env, 2)
        await env.http.get("/v1/today", headers=pat)
        assert outage.calls == asked + 1
        await office_login(env)
        page = await env.http.get(f"/office?day={today()}&refresh=1")
        assert outage.calls == asked + 2 and "FieldRoutes did not answer" in page.text

    minutes_pass(env, 2)
    assert await stop_ids(env, pat) == [JANE, BOB, ACME], "and when FieldRoutes is back the day is read again"


# == 8. Two appointments at one property, one cancelled ================================


async def test_one_of_a_customers_appointments_cancelled_keeps_the_stop_and_its_video(env: Env) -> None:
    pat = await ready(env)
    video_id = await video_at(env, pat, JANE, "held")
    assert (await env.video(video_id)).appointment_ids == list(JANES)

    take_off(env, "cancelled", 9001)
    await look_again(env)
    jane = await stop_for(env, pat, JANE)
    assert jane["appointmentIds"] == [9002, 9003] and jane["done"] is True
    assert jane["video"] == {"id": video_id, "state": "held", "byYou": True}
    assert await env.flags() == [], "the stop is still his: nothing for the office to decide"

    await send_all(env, video_id)
    assert video_mail(env) == ["jane@example.com"]
    assert (await env.video(video_id)).appointment_ids == [9002, 9003], "the video lets go of the cancelled appointment"

    # A second one is cancelled after the email. The customer's score still lands on a live appointment.
    take_off(env, "deleted", 9002)
    await look_again(env)
    token = (await env.video(video_id)).token
    assert (await env.http.post(f"/w/{token}/score", data={"stars": "5", "comment": "Great"})).status_code == 303
    (review,) = [r for r in await env.all(sa.select(db.fr_outbox)) if r.kind == "review"]
    assert review.payload["appointment_id"] == 9003
    assert await env.flags() == [] and video_mail(env) == ["jane@example.com"]


FILMED = ["before the cancellation", "after it, from a stale list", "after it, by a phone that names no appointments"]


@pytest.mark.parametrize("filmed", FILMED)
async def test_two_techs_at_one_property_and_one_techs_appointment_is_cancelled(env: Env, filmed: str) -> None:
    sams_own_work_at_bobs(env)  # Sam treated the lawn earlier; Pat is there now to mow (9004)
    pat, sam = await both(env)
    if filmed == "before the cancellation":
        pats = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)  # the mowing is cancelled; Sam's visit stands
    await look_again(env)

    # Sam's own visit finished long ago with no video, and the office has been told.
    await env.finished(BOB, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(BOB, SAM)}
    if filmed == "after it, from a stale list":
        pats = await video_at(env, pat, BOB, "held", appointmentIds=[BOBS])
    elif filmed != "before the cancellation":
        pats = await video_at(env, pat, BOB, "held", appointmentIds=[])
    assert await missing(env) == {(BOB, SAM)}, "a video for Pat's cancelled work does not answer for Sam's visit"
    await send_all(env, pats)

    assert video_mail(env) == [], "Pat's video was for the cancelled work: it waits for the office"
    assert (await env.video(pats)).email_skip == "off_route" and len(await env.flags("off_route")) == 1
    assert 9010 not in (await env.video(pats)).appointment_ids, "it is never tied to Sam's appointment"
    assert await stop_ids(env, pat) == [JANE, ACME]
    bob = await stop_for(env, sam, BOB)
    assert bob["appointmentIds"] == [9010] and bob["video"] is None, "Pat's video does not stand in for Sam's own visit"

    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(BOB, SAM)}, "Sam still owes his"
    sams = await video_at(env, sam, BOB, "emailed", recordedAt=now_iso(80))
    assert video_mail(env) == ["bob@example.com"] and await missing(env) == set()
    assert (await env.video(sams)).appointment_ids == [9010] and (await env.video(pats)).state == "no_email"


# == 9. The tech does not follow the route ==============================================


async def test_stops_worked_in_any_order_all_reach_the_right_customer(env: Env) -> None:
    env.fake.data["customer"][ACME]["email"] = "frontdesk@acme.example"
    pat = await ready(env)
    # Last stop first, then the middle one; he skipped the first and came back to it.
    filmed = {c: await video_at(env, pat, c, "held") for c in (ACME, BOB, JANE)}
    await send_all(env, *filmed.values())

    assert sorted(video_mail(env)) == ["bob@example.com", "frontdesk@acme.example", "jane@example.com"]
    for customer_id, video_id in filmed.items():
        video = await env.video(video_id)
        assert (video.customer_id, video.state) == (customer_id, "sent")
        assert video.email_to == env.fake.data["customer"][customer_id]["email"]
    assert await env.flags() == [], "the order he works in is his own business"
    body = await today_for(env, pat)
    assert [s["customerId"] for s in body["stops"]] == [JANE, BOB, ACME], "the list stays in FieldRoutes' order"
    assert [s["video"]["id"] for s in body["stops"]] == [filmed[JANE], filmed[BOB], filmed[ACME]]


async def test_filming_well_before_checking_in_is_flagged_and_never_blocked(env: Env) -> None:
    pat = await ready(env)
    early = await video_at(env, pat, BOB, "held", recordedAt=now_iso(45))  # he checked in 5 minutes ago
    after = await video_at(env, pat, JANE, "held", recordedAt=now_iso(10))  # 10 minutes after checking out
    await send_all(env, early, after)
    assert sorted(video_mail(env)) == ["bob@example.com", "jane@example.com"], "both go: a flag, never a block"
    (flag,) = await env.flags()
    assert (flag.kind, flag.video_id) == ("wrong_house", early) and "outside the check-in time" in flag.detail


async def test_a_tech_who_never_checks_in_is_not_flagged_for_the_time_and_still_owes_his_videos(env: Env) -> None:
    env.fake.data["customer"][ACME]["email"] = "frontdesk@acme.example"
    env.fake.data["appointment"][BOBS].update(checkIn="0000-00-00 00:00:00")
    pat = await ready(env)
    video_id = await video_at(env, pat, ACME, "emailed")  # Acme was never checked in
    assert (await env.video(video_id)).time_check == "unknown" and await env.flags() == []

    # Bob's visit is completed in FieldRoutes with no check-in at all, and no video.
    env.fake.data["appointment"][BOBS].update(status="1")
    await look_again(env)
    bob = await stop_for(env, pat, BOB)
    assert bob["done"] is True and bob["checkIn"] is None
    await env.finished(BOB, ago=timedelta(minutes=61), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(BOB, PAT)}


async def test_closing_out_every_appointment_in_one_batch_at_the_end_of_the_day(env: Env) -> None:
    env.fake.data["customer"][ACME]["email"] = "frontdesk@acme.example"
    for appointment_id in (*JANES, BOBS, ACMES):  # all day FieldRoutes shows nothing started
        row = env.fake.data["appointment"][appointment_id]
        row.update(status="0", checkIn="0000-00-00 00:00:00", checkOut="0000-00-00 00:00:00")
    pat = await ready(env)
    janes = await video_at(env, pat, JANE, "emailed", recordedAt=now_iso(300))
    bobs = await video_at(env, pat, BOB, "emailed", recordedAt=now_iso(200))
    await look_again(env)
    assert await env.flags() == [], "no check-in to compare with, and nothing finished yet"
    assert [s["done"] for s in (await today_for(env, pat))["stops"]] == [False, False, False]

    # 5 p.m.: he checks every appointment in and out, seconds apart.
    for appointment_id in (*JANES, BOBS, ACMES):
        env.fake.data["appointment"][appointment_id].update(status="1", checkIn=minutes_ago(1), checkOut=minutes_ago(1))
    await look_again(env)
    assert [s["done"] for s in (await today_for(env, pat))["stops"]] == [True, True, True]
    assert await env.flags() == [], (
        "videos filmed hours before the batch are not judged against it, and Acme gets its hour"
    )
    assert {(await env.video(v)).state for v in (janes, bobs)} == {"sent"}

    await env.finished(ago=timedelta(minutes=61), force=False)
    await jobs.flag_missing(env.ctx)
    assert await missing(env) == {(ACME, PAT)}, "an hour after the batch, only the stop with no video is chased"
    assert sorted(video_mail(env)) == ["bob@example.com", "jane@example.com"]


async def test_work_closed_out_after_hours_is_still_checked_the_next_morning(env: Env) -> None:
    """A tech who completes his appointments from home at 9 p.m. is never seen by the
    day's refreshes (they stop with work hours). The 3 a.m. job looks at yesterday once."""
    yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
    for appointment_id, customer_id, tech_id, route_id in ((9101, CY, PAT, "60"), (9102, BOB, PAT, "60"),
                                                         (9103, ANN, SAM, "61")):
        env.fake.data["appointment"][appointment_id] = {
            "appointmentID": str(appointment_id), "officeID": "7", "customerID": str(customer_id), "routeID": route_id,
            "type": "89", "status": "1", "assignedTech": str(tech_id), "servicedBy": str(tech_id), "employeeID": "22",
            "date": yesterday, "start": "08:00:00", "sequence": "1",
        }
    await jobs.daily(env.ctx)
    assert env.fr_calls("appointment", "search") == [], "nobody has the app, so no read is spent on yesterday"

    pat = await login(env)
    await env.run(sa.update(db.devices).values(created_at=db.now() - timedelta(days=3)))  # Pat has had the app all week
    await login(env, "sam@example.com")  # Sam signed in for the first time this morning
    before = len(env.fr_calls("appointment", "search"))
    await jobs.daily(env.ctx)
    searches = env.fr_calls("appointment", "search")[before:]
    assert [s.one("date") for s in searches] == [yesterday], "one more read of yesterday"

    flags = await env.flags("no_video")
    assert {(f.day, f.customer_id, f.tech_id) for f in flags} == {(yesterday, CY, PAT), (yesterday, BOB, PAT)}, (
        "Pat's two are flagged with no waiting time; Sam did not have the app yesterday"
    )
    await jobs.daily(env.ctx)
    assert len(await env.flags("no_video", open_only=False)) == 2, "raised once"

    # One of them was filmed after all and sat on the phone overnight. It lands: that flag goes.
    late = video_body(BOB, stopDate=yesterday, recordedAt=now_iso(600))
    assert (await env.http.post("/v1/videos", json=late, headers=pat)).status_code == 200
    assert {(f.customer_id, f.tech_id) for f in await env.flags("no_video")} == {(CY, PAT)}


# == 10. The route changes overnight ====================================================


@pytest.mark.parametrize("filmed", [1, 3], ids=["filmed yesterday", "filmed on Friday and rolled over the weekend"])
async def test_a_filmed_visit_the_office_rolls_to_another_day_is_told_to_the_office(env: Env, filmed: int) -> None:
    """Pat did Cy's job and filmed it, and the customer has the video. He did not close the
    appointment out, so the office rolled it to today: same appointment, new date. Whether
    that first video is enough is the owner's call. Until he makes it, the app asks again
    as it always did, and the office is told the visit was already filmed."""
    then = days_ago(filmed)
    earlier_visit(env, CY, PAT, status="0", day=then)
    pat = await ready(env)
    await sync_day(env.ctx, then, force=True)
    body = video_body(CY, stopDate=then, appointmentIds=[9102], recordedAt=now_iso(filmed * 1440 - 600))
    assert (await env.http.post("/v1/videos", json=body, headers=pat)).status_code == 200
    await upload(env, pat, body["id"])
    await send_all(env, body["id"])
    assert video_mail(env) == ["cy@example.com"] and await env.flags() == []

    env.fake.data["appointment"][9102].update(date=today(), status="1", checkIn=minutes_ago(130), checkOut=minutes_ago(125))
    await look_again(env)
    (flag,) = await route_flags(env)
    assert flag.kind == "off_route" and flag.video_id == body["id"] and flag.day == then
    assert f"Pat filmed Cy Young, 77 Elm St on {short(then)}, and the customer has the video" in flag.detail
    assert f"now has that same visit on {short(today())}" in flag.detail and "filmed again" in flag.detail

    cy = await stop_for(env, pat, CY)
    assert cy["appointmentIds"] == [9102] and cy["video"] is None, "today's list asks for it, as before"
    await env.finished(CY, ago=timedelta(hours=2), force=False)
    await jobs.flag_missing(env.ctx)
    (chase,) = await env.flags("no_video")
    assert (chase.customer_id, chase.tech_id, chase.day) == (CY, PAT, today())
    assert f"A video for this same visit went out on {short(then)} (Pat's)" in chase.detail, (
        "the 'no video' flag says a video for this visit already went, so nobody is chased blind"
    )

    # Each is raised once, and stays cleared once a person has cleared it.
    csrf = await office_login(env)
    await clear_as_office(env, csrf, flag, "Pat told: no need to film it again")
    await clear_as_office(env, csrf, chase, "filmed already")
    for _ in range(2):
        await look_again(env)
    assert await env.flags() == [] and video_mail(env) == ["cy@example.com"]
    assert (await env.video(body["id"])).state == "sent"


async def test_the_rounds_do_not_dig_up_days_that_are_no_longer_read(env: Env) -> None:
    """A day before yesterday is never read from FieldRoutes again, so its saved stops can
    say nothing new about a video that was checked when it went out. The rounds look at
    such a video only to see whether its visit has turned up on a later day."""
    then = days_ago(3)
    earlier_visit(env, BOB, PAT, day=then)
    pat = await ready(env)
    await sync_day(env.ctx, then, force=True)
    video_id = await video_at(env, pat, BOB, "emailed", stopDate=then, recordedAt=now_iso(3 * 1440 - 600))
    assert video_mail(env) == ["bob@example.com"] and await route_flags(env) == []
    # Whatever state that old day's saved stops were left in:
    await env.run(sa.update(db.stops).where(db.stops.c.day == then).values(gone=True))
    for _ in range(2):
        await look_again(env)
    assert await route_flags(env) == [] and (await env.video(video_id)).state == "sent"


async def test_a_late_video_for_a_past_day_whose_stop_changed_hands_is_flagged_when_it_goes(env: Env) -> None:
    """A video that sat on a phone overnight. The rounds never look at a past day again,
    so the office has to be told at the moment the email goes."""
    earlier_visit(env, BOB, PAT)
    pat, sam = await both(env)
    await sync_day(env.ctx, yesterday(), force=True)  # yesterday's list as Pat's phone had it
    env.fake.data["appointment"][9102]["assignedTech"] = str(SAM)  # the office corrects who had it
    list_is_old(env)
    late = video_body(BOB, stopDate=yesterday(), appointmentIds=[9102], recordedAt=now_iso(600))
    assert (await env.http.post("/v1/videos", json=late, headers=pat)).status_code == 200
    await upload(env, pat, late["id"])
    await send_all(env, late["id"])

    assert video_mail(env) == ["bob@example.com"]
    (flag,) = await route_flags(env)
    assert flag.video_id == late["id"] and flag.day == yesterday() and "under Sam" in flag.detail
    for _ in range(2):
        await look_again(env)
    assert len(await route_flags(env, open_only=False)) == 1


async def test_yesterdays_video_for_a_visit_found_cancelled_overnight_is_flagged(env: Env) -> None:
    """The 3 a.m. job reads yesterday once more. If that shows a filmed visit was cancelled
    after its email went, the office hears of it, the same as on the day."""
    earlier_visit(env, CY, PAT)  # Cy is not on today's list at all
    pat = await login(env)
    await env.run(sa.update(db.devices).values(created_at=db.now() - timedelta(days=3)))  # Pat has had the app all week
    await sync_day(env.ctx, yesterday(), force=True)
    video_id = await video_at(env, pat, CY, "emailed", stopDate=yesterday(), recordedAt=now_iso(600))
    env.mailer.sent.clear()
    await look_again(env)
    assert await route_flags(env) == []

    env.fake.data["appointment"][9102]["status"] = "-1"  # cancelled in the evening, after hours
    await jobs.daily(env.ctx)
    await jobs.watch_route(env.ctx)
    (flag,) = await route_flags(env)
    assert flag.video_id == video_id and "the customer has the video" in flag.detail
    assert (await env.video(video_id)).state == "sent" and video_mail(env) == []


# == 11. What the phone is told about a video the office is holding ======================
#
# One optional field on every video in GET /v1/today, in stops[].video and in videos[]:
# "withOffice": true while the server holds the video for the office to decide (Send it
# anyway, or Pull), and no such key at all otherwise. The app then shows SENT TO OFFICE
# where it would show SENT.

STOP_VIDEO_KEYS = {"id", "state", "byYou"}
LISTED_VIDEO_KEYS = {"id", "customerId", "state", "emailAt"}


async def listed(env: Env, headers: dict[str, str]) -> dict[str, dict[str, Any]]:
    return {v["id"]: v for v in (await today_for(env, headers))["videos"]}


async def test_a_video_that_is_not_held_for_the_office_carries_no_with_office_key(env: Env) -> None:
    pat = await ready(env)
    on_phone = await video_at(env, pat, ACME, "registered")
    in_ten_minutes = await video_at(env, pat, BOB, "held")
    emailed = await video_at(env, pat, JANE, "emailed")
    loose = await film(env, pat, None)  # not on the list
    body = await today_for(env, pat)
    assert {v["id"]: v["state"] for v in body["videos"]} == {
        on_phone: "awaiting_upload", in_ten_minutes: "held", emailed: "sent", loose: "unmatched",
    }
    assert all(set(v) == LISTED_VIDEO_KEYS for v in body["videos"]), "exactly the four keys, and no withOffice"
    refs = [s["video"] for s in body["stops"]]
    assert len(refs) == 3 and all(set(ref) == STOP_VIDEO_KEYS for ref in refs)

    # No email went, for a reason that is nobody's to decide: still not "with the office".
    await upload(env, pat, on_phone)
    await send_all(env, on_phone)
    assert (await env.video(on_phone)).email_skip == "no_address"  # Acme has no email on file
    await videos.pull(env.ctx, in_ten_minutes, OWNER, "wrong take")
    body = await today_for(env, pat)
    assert all("withOffice" not in v for v in body["videos"])
    assert all("withOffice" not in (s["video"] or {}) for s in body["stops"])


@pytest.mark.parametrize("how", REMOVALS)
async def test_the_phone_is_told_when_a_video_waits_because_its_visit_left_the_route(env: Env, how: str) -> None:
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, how, BOBS)
    await look_again(env)
    assert "withOffice" not in (await listed(env, pat))[video_id], "inside its ten minutes nothing is decided yet"
    await send_all(env, video_id)

    mine = (await listed(env, pat))[video_id]
    assert mine == {"id": video_id, "customerId": BOB, "state": "no_email", "emailAt": mine["emailAt"], "withOffice": True}
    assert BOB not in await stop_ids(env, pat), "the stop itself is off his list"

    # The visit comes back: the video goes by itself and the key is gone again.
    put_back(env, BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"]
    assert set((await listed(env, pat))[video_id]) == LISTED_VIDEO_KEYS
    assert (await stop_for(env, pat, BOB))["video"] == {"id": video_id, "state": "sent", "byYou": True}


async def test_the_phone_is_told_when_a_second_video_for_one_visit_waits(env: Env) -> None:
    """Pat's video for Bob went out, then the office gave the stop to Sam, and Sam films it
    too. Sam's is the second video for one visit: it waits, and his phone says so on the
    stop and in his list."""
    pat, sam = await both(env)
    pats = await video_at(env, pat, BOB, "emailed")
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    sams = await video_at(env, sam, BOB, "held")
    await send_all(env, sams)
    assert (await env.video(sams)).email_skip == "second_video"

    assert (await stop_for(env, sam, BOB))["video"] == {"id": sams, "state": "no_email", "byYou": True, "withOffice": True}
    assert (await listed(env, sam))[sams]["withOffice"] is True
    assert set((await listed(env, pat))[pats]) == LISTED_VIDEO_KEYS, "Pat's went out: nothing is held"

    # The office says send. From then on it is an ordinary sent video.
    csrf = await office_login(env)
    await env.http.post(f"/office/video/{sams}/send", data={"csrf": csrf})
    await send_all(env, sams)
    assert (await stop_for(env, sam, BOB))["video"] == {"id": sams, "state": "sent", "byYou": True}
    assert set((await listed(env, sam))[sams]) == LISTED_VIDEO_KEYS


async def test_the_phone_is_told_when_a_video_waits_because_that_tech_has_no_work_there(env: Env) -> None:
    """Two techs at one property and Pat's appointment is cancelled. His video has no work
    to go with, so it waits. Sam's own visit there is untouched and shows no video."""
    sams_own_work_at_bobs(env)
    pat, sam = await both(env)
    await look_again(env)
    pats = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    await look_again(env)
    await send_all(env, pats)
    assert ((await env.video(pats)).state, (await env.video(pats)).email_skip) == ("no_email", "off_route")
    assert (await listed(env, pat))[pats]["withOffice"] is True
    assert (await stop_for(env, sam, BOB))["video"] is None, "and it never stands in for Sam's own video"

    # Pulled by the office: it leaves the tech's list altogether.
    await videos.pull(env.ctx, pats, OWNER, "the visit was cancelled")
    assert pats not in await listed(env, pat)


async def test_a_video_held_for_being_late_is_not_let_go_by_the_route(env: Env) -> None:
    """The fourth kind of hold: the email was hours late (the server was down, or a backup
    was put back). The route has no say over that one. Whatever happens to the visit, the
    video waits for the office, and the phone keeps saying so."""
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=8)
    await env.run(sa.update(db.videos).where(db.videos.c.id == video_id).values(
        recorded_at=long_ago, uploaded_at=long_ago, created_at=long_ago))
    await send_all(env, video_id)
    assert (await env.video(video_id)).email_skip == "overdue" and video_mail(env) == []
    assert (await stop_for(env, pat, BOB))["video"] == {"id": video_id, "state": "no_email", "byYou": True, "withOffice": True}

    take_off(env, "cancelled", BOBS)
    await look_again(env)
    # While the visit is off the day, the one open flag says so as well as "late".
    (flag,) = [f for f in await env.flags() if f.video_id == video_id]
    assert flag.kind == "overdue" and "FieldRoutes no longer has that visit on this day" in flag.detail
    put_back(env, BOBS)
    for _ in range(2):
        await look_again(env)
        await send_all(env, video_id)
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == [] and (await env.video(video_id)).email_skip == "overdue"
    assert (await listed(env, pat))[video_id]["withOffice"] is True
    assert [f.kind for f in await env.flags() if f.video_id == video_id] == ["overdue"], "one flag, and it says why"
    assert await route_flags(env, open_only=False) == [], "and none about the route: that is not what holds it"


async def held_back_this_long_ago(env: Env, pat: dict[str, str], **ago: float) -> str:
    """Pat films Bob, the office cancels the visit, and the video is held back. All of
    that happened `ago`."""
    video_id = await video_at(env, pat, BOB, "held")
    take_off(env, "cancelled", BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert (await env.video(video_id)).email_skip == "off_route"
    then = datetime.now(timezone.utc) - timedelta(**ago)
    await env.run(sa.update(db.videos).where(db.videos.c.id == video_id).values(
        recorded_at=then, uploaded_at=then, created_at=then))
    return video_id


async def test_a_video_the_route_held_back_goes_when_its_visit_returns_the_same_afternoon(env: Env) -> None:
    """Held back in the morning because the visit was cancelled; the office puts the visit
    back five hours later. That is the route letting it go: it goes by itself."""
    pat = await ready(env)
    video_id = await held_back_this_long_ago(env, pat, hours=5)
    put_back(env, BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"] and (await env.video(video_id)).state == "sent"
    assert await env.flags("overdue", open_only=False) == []


async def test_a_video_the_route_held_back_does_not_go_by_itself_once_its_email_is_hours_late(env: Env) -> None:
    """The same, but the visit comes back nine hours later (an appointment un-cancelled in
    the evening, or found back on the day at the 3 a.m. look). By then the email is hours
    late, and the route does not let such a video go: nobody is emailed at 3 a.m., and a
    video found waiting again after a backup was put back is not sent a second time.

    (This test used to say the opposite: "goes when its visit returns, however long that
    took". That rule is what sent second emails after a restore. However long still holds
    with the owner's late rule switched off; see the next test.)"""
    pat = await ready(env)
    video_id = await held_back_this_long_ago(env, pat, hours=9)
    put_back(env, BOBS)
    for _ in range(2):
        await look_again(env)
        await send_all(env, video_id)
    video = await env.video(video_id)
    assert video_mail(env) == [] and (video.state, video.email_skip) == ("no_email", "overdue")
    assert await env.all(sa.select(db.fr_outbox)) == [], "and nothing is written to FieldRoutes"
    (flag,) = [f for f in await env.flags() if f.video_id == video_id]
    assert flag.kind == "overdue" and "Its email was 9 hours late, so it was not sent by itself" in flag.detail
    assert await route_flags(env) == [], "the flag about the cancelled visit is closed: that is over"
    assert (await stop_for(env, pat, BOB))["video"]["withOffice"] is True
    actions = await env.actions()
    assert "video_released" not in actions and actions.count("email_skipped") == 2, "held twice, each written down"

    # It waits for the office, like any late video, and goes once on its word.
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"] and (await env.video(video_id)).state == "sent"


async def test_with_the_late_rule_switched_off_the_route_lets_a_video_go_however_long_it_took(env: Env) -> None:
    """The owner's setting (Overdue hours) at 0: no email is ever held for being late, so a
    video the route held back goes when its visit returns, however long that took."""
    await set_setting(env.ctx, "overdue_hours", "0", OWNER)
    pat = await ready(env)
    video_id = await held_back_this_long_ago(env, pat, hours=9)
    put_back(env, BOBS)
    await look_again(env)
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"] and (await env.video(video_id)).state == "sent"
    assert await env.flags("overdue", open_only=False) == []


async def test_only_the_office_ends_the_hold_on_a_late_video(env: Env) -> None:
    """`release` is the route letting a video go. It must never let go of one that waits
    because its email was late, whoever calls it."""
    pat = await ready(env)
    video_id = await video_at(env, pat, BOB, "held")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=8)
    await env.run(sa.update(db.videos).where(db.videos.c.id == video_id).values(
        recorded_at=long_ago, uploaded_at=long_ago, created_at=long_ago))
    await send_all(env, video_id)
    video = await env.video(video_id)
    assert video.email_skip == "overdue"

    assert await videos.release(env.ctx, video) is False
    after = await env.video(video_id)
    assert (after.state, after.email_skip) == ("no_email", "overdue")
    assert "video_released" not in await env.actions()
    await send_all(env, video_id)
    assert video_mail(env) == []


async def test_send_it_anyway_on_a_late_video_does_not_let_a_second_video_for_the_visit_out(env: Env) -> None:
    """Bob's stop is given to Sam. Sam's video is late (the server was down) and waits
    under a flag that says only that. Meanwhile Pat's phone, with its old list, hands in
    his own video for the same visit, and that one goes to Bob. If the office then answers
    Sam's flag with Send it anyway, it has said "send it, late as it is". Nobody has told
    it there are two videos now, so Bob does not get the second one on that say-so."""
    pat, sam = await both(env)
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    sams = await video_at(env, sam, BOB, "held")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=8)
    await env.run(sa.update(db.videos).where(db.videos.c.id == sams).values(
        recorded_at=long_ago, uploaded_at=long_ago, created_at=long_ago))
    await send_all(env, sams)
    assert (await env.video(sams)).email_skip == "overdue"
    await video_at(env, pat, BOB, "emailed", appointmentIds=[BOBS])
    assert video_mail(env) == ["bob@example.com"]

    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{sams}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "err=" not in r.headers["location"]
    await send_all(env, sams)
    assert video_mail(env) == ["bob@example.com"], "still one email for the one visit"
    assert (await env.video(sams)).email_skip == "second_video"
    (flag,) = await env.flags("second_video")
    assert flag.video_id == sams and "Pat" in flag.detail and flag.detail.endswith("Press Send it anyway, or Pull.")

    # Told now, the office may still choose to send both.
    await env.http.post(f"/office/video/{sams}/send", data={"csrf": csrf})
    await send_all(env, sams)
    assert video_mail(env) == ["bob@example.com", "bob@example.com"] and (await env.video(sams)).state == "sent"


async def test_a_late_video_says_on_its_flag_when_there_is_a_second_video_for_the_visit(env: Env) -> None:
    """The same pair, seen by the server's own round before anyone presses anything: the
    late video's flag is replaced by one that also says the other tech filmed the visit
    and the customer has that video. An answer to that flag is an answer to both."""
    pat, sam = await both(env)
    hand_to_sam(env, "given to Sam", BOBS)
    await look_again(env)
    sams = await video_at(env, sam, BOB, "held")
    long_ago = datetime.now(timezone.utc) - timedelta(hours=8)
    await env.run(sa.update(db.videos).where(db.videos.c.id == sams).values(
        recorded_at=long_ago, uploaded_at=long_ago, created_at=long_ago))
    await send_all(env, sams)
    (first,) = await env.flags("overdue")
    assert "filmed the same visit" not in first.detail
    await video_at(env, pat, BOB, "emailed", appointmentIds=[BOBS])
    await look_again(env)

    (flag,) = await env.flags("overdue")
    assert flag.id != first.id and flag.video_id == sams
    assert "Pat filmed the same visit too, and the customer already has it." in flag.detail
    assert (await env.video(sams)).email_skip == "overdue", "still held for being late: only the office ends that"
    await look_again(env)
    assert len(await env.flags("overdue", open_only=False)) == 2, "told once for each thing, not every round"

    csrf = await office_login(env)
    await env.http.post(f"/office/video/{sams}/send", data={"csrf": csrf})
    await send_all(env, sams)
    assert video_mail(env) == ["bob@example.com", "bob@example.com"], "the office knew of both, and sent it: one press"
    assert (await env.video(sams)).state == "sent"


async def test_the_office_is_still_told_when_the_server_stops_between_sending_and_flagging(env: Env, monkeypatch) -> None:
    """A late video for a past day, for a stop that has changed hands: it goes, and the
    office is told at that moment, because the rounds never look at a past day again. If
    the server is stopped in between, the next round finds it from the emailer's own note
    in the audit trail."""
    earlier_visit(env, BOB, PAT, day=days_ago(3))
    pat, sam = await both(env)
    await sync_day(env.ctx, days_ago(3), force=True)
    env.fake.data["appointment"][9102]["assignedTech"] = str(SAM)
    list_is_old(env)
    late = video_body(BOB, stopDate=days_ago(3), appointmentIds=[9102], recordedAt=now_iso(600))
    assert (await env.http.post("/v1/videos", json=late, headers=pat)).status_code == 200
    await upload(env, pat, late["id"])

    class Stopped(BaseException):
        """The server was stopped right here."""

    async def stopped(*args: Any, **kwargs: Any) -> int:
        raise Stopped

    with monkeypatch.context() as patch:
        patch.setattr(videos, "review_route", stopped)
        with pytest.raises(Stopped):
            await send_all(env, late["id"])
    assert video_mail(env) == ["bob@example.com"] and await route_flags(env) == [], "sent, and the office not yet told"

    await look_again(env)
    (flag,) = await route_flags(env)
    assert flag.video_id == late["id"] and flag.day == days_ago(3) and "under Sam" in flag.detail
    await look_again(env)
    assert len(await route_flags(env, open_only=False)) == 1


# == How soon a change is seen, and what watching costs ================================


async def test_how_soon_the_phone_and_the_server_see_a_change(env: Env) -> None:
    pat = await ready(env)
    take_off(env, "cancelled", ACMES)
    calls = len(env.fake.requests)

    minutes_pass(env, 2)
    assert await stop_ids(env, pat) == [JANE, BOB, ACME] and len(env.fake.requests) == calls, (
        "inside three minutes the phone is given the saved list and FieldRoutes is not asked"
    )
    minutes_pass(env, 2)
    assert await stop_ids(env, pat) == [JANE, BOB], "after three, the phone's own fetch brings the change in"
    assert [(r.entity, r.action) for r in env.fake.requests[calls:]] == [("appointment", "search"), ("route", "search")]

    # With no phone asking, the server's own round finds it within five minutes.
    put_back(env, ACMES)
    minutes_pass(env, 5)
    await jobs.refresh_day(env.ctx)
    calls = len(env.fake.requests)
    assert await stop_ids(env, pat) == [JANE, BOB, ACME] and len(env.fake.requests) == calls

    # Outside work hours the server does not look by itself.
    env.reconfigure(work_start_hour=0, work_end_hour=0)
    take_off(env, "cancelled", ACMES)
    minutes_pass(env, 30)
    await jobs.refresh_day(env.ctx)
    assert len(env.fake.requests) == calls


async def test_watching_the_route_all_day_stays_far_inside_the_shared_fieldroutes_budget(env: Env) -> None:
    pat, sam = await both(env)
    await video_at(env, pat, BOB, "emailed")
    start = len(env.fake.requests)
    for minute in range(60):  # one busy hour: two phones asking every minute, the server's round every five
        minutes_pass(env, 1)
        for headers in (pat, sam):
            await env.http.get("/v1/today", headers=headers)
        if minute % 5 == 0:
            await jobs.refresh_day(env.ctx)
            asked = len(env.fake.requests)
            await jobs.watch_route(env.ctx)
            await jobs.flag_missing(env.ctx)
            assert len(env.fake.requests) == asked, "checking videos and flags against the route reads nothing"
    hour = env.fake.requests[start:]
    searches = [r for r in hour if (r.entity, r.action) == ("appointment", "search")]
    assert len(searches) == 20, "the day is read at most once every three minutes, however many phones ask"
    assert len(hour) <= 2 * len(searches), "plus the route read, on days when an appointment names no tech"
    assert len(hour) * 14 <= 3000 // 5, "a 14-hour work day of this is under a fifth of the 3,000 shared reads"



async def test_while_fieldroutes_is_failing_it_is_tried_once_a_minute_and_no_more(env: Env) -> None:
    """The other side of the budget: an hour in which every read fails. Two phones ask
    every 20 seconds, the emailer comes round with a video that is due, and the rounds run."""
    pat, sam = await both(env)
    video_id = await video_at(env, pat, BOB, "held")
    with Outage(env) as outage:
        for minute in range(60):
            if minute % 5 == 0:
                await env.window_closed(video_id)  # the emailer's five-minute lease on the waiting video is up
                await look_again(env)
            for _ in range(3):
                minutes_pass(env, 1 / 3)
                for headers in (pat, sam):
                    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
                await videos.send_due(env.ctx)
        day_reads = outage.asked.count(("appointment", "search"))
        # Once a minute at most, whoever is asking. 61 counts both ends of the hour.
        assert 30 <= day_reads <= 61
        assert outage.calls - day_reads <= 12, "plus, at most, the emailer's own try for an address once per lease"
    assert video_mail(env) == [] and (await env.video(video_id)).state == "held", "and no email went out unchecked"
    minutes_pass(env, 2)
    await send_all(env, video_id)
    assert video_mail(env) == ["bob@example.com"], "it goes as soon as the day can be read again"
