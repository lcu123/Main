"""One email: the video link inside FieldRoutes' own after-service email.

The owner's decision of Nov 14, 2026: after a visit the customer gets one email, not two,
and the video is one tap away inside it. So the link rides in the email FieldRoutes
already sends. The office sets that email to go 30 or 60 minutes after a visit is
completed and prints the technician's comments in it; this server adds one line with
the link to those comments, and sends no email of its own.

It ships switched off (`fr_email_minutes` = 0), and switched off the server does what it
did before. Everything here runs against the fake FieldRoutes in `video_helpers.VideoFR`,
which models `appointment/complete` the way FieldRoutes' help centre documents it. The
real call has never been made: docs/video/GO-LIVE.md has the test the owner runs first.

The story: Pat (tech 20) has Jane (three appointments in one visit), Bob and Acme (no
email on file). Sam (tech 21) has Ann. A test moves the server's clock; the fake's
`comments_in_the_email` then says what FieldRoutes' delayed email would have carried.
"""

from __future__ import annotations

import asyncio
import html
import time
import urllib.parse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from conftest import _client_for
from test_video_restart import (  # noqa: F401 - the same stand-ins for a restart, a kill and a backup as the restart tests use
    Clock, Killed, kill_switch, not_this_machines_own_settings, one_pass, restart, restore_backup, take_backup,
)
from video_helpers import (
    BASE, NEVER, OWNER, TZ, Env, film, fr_time, login, make_env, minutes_ago, office_login, today, video_body,
)

from fr_mcp.client import FieldRoutesClient, RateLimiter
from zest_video import db, jobs, route, videos
from zest_video.app import build_ctx
from zest_video.core import DEFAULT_SETTINGS, get_settings, set_setting
from zest_video.fr import FieldRoutes, FieldRoutesError
from zest_video.http_office import stats
from zest_video.route import FR_EMAIL, FR_MISSED, HELD_BACK

PAT, SAM = 20, 21
JANE, BOB, ACME, ANN = 501, 502, 503, 504
JANES, BOBS, ACMES, ANNS = (9001, 9002, 9003), 9004, 9005, 9006
SAMS_AT_BOBS = 9010


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


# -- what the people in the story do ---------------------------------------------------


async def ready(env: Env, contact: str = "916-555-0101") -> dict[str, str]:
    """A tech signs in and his phone fetches the day."""
    headers = await login(env, contact)
    assert (await env.http.get("/v1/today", headers=headers)).status_code == 200
    env.mailer.sent.clear()
    return headers


async def one_email(env: Env, minutes: int = 30) -> None:
    """The owner picks FieldRoutes' own email on the Settings page."""
    await set_setting(env.ctx, "fr_email_minutes", minutes, OWNER)


def completes(env: Env, clock: Clock, *appointment_ids: int, ago: float = 0, comments: str | None = None) -> None:
    """The tech completes these appointments in FieldRoutes' own app, `ago` minutes back
    by the server's clock. `comments` are what he typed for the customer."""
    when = fr_time(clock.now() - timedelta(minutes=ago))
    for appointment_id in appointment_ids:
        appointment = env.fake.data["appointment"][appointment_id]
        appointment.update(status="1", dateCompleted=when)
        appointment["notes"] = comments if comments is not None else appointment.get("notes", "")


def not_completed(env: Env, *appointment_ids: int) -> None:
    for appointment_id in appointment_ids:
        env.fake.data["appointment"][appointment_id].update(status="0", dateCompleted=NEVER, notes="")


def just_now(clock: Clock) -> str:
    """Filmed a minute ago by the server's clock."""
    return (clock.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def the_list_is_old(env: Env) -> None:
    """The saved route is old enough to be read again, as it is every few minutes."""
    for key in [k for k in env.ctx.last_sync if not k.startswith("failed:")]:
        del env.ctx.last_sync[key]


async def emailer(env: Env) -> None:
    """The emailer, once a minute. In work hours the server also reads today's list every
    five minutes, so the list it has saved is never much older than that."""
    the_list_is_old(env)
    try:
        await jobs.refresh_day(env.ctx)
    except FieldRoutesError:
        pass  # the job loop logs it and carries on
    the_list_is_old(env)
    await videos.send_due(env.ctx)


async def a_minute(env: Env) -> None:
    """The two jobs the server runs once a minute: the emailer, then the FieldRoutes outbox."""
    await emailer(env)
    await videos.run_outbox(env.ctx)


async def five_minute_round(env: Env) -> None:
    the_list_is_old(env)
    await jobs.refresh_day(env.ctx)
    await jobs.watch_route(env.ctx)
    await jobs.flag_missing(env.ctx)


def our_emails(env: Env) -> list[Any]:
    """The video emails this server sent to customers. (The recorder also holds sign-in codes.)"""
    return [m for m in env.mailer.sent if m.tag == "video"]


def link_of(video: Any) -> str:
    return f"{BASE}/w/{video.token}"


def line_for(video: Any) -> str:
    return f"Watch the video of today's visit: {link_of(video)}"


async def outbox(env: Env, kind: str | None = None) -> list[Any]:
    rows = await env.all(sa.select(db.fr_outbox).order_by(db.fr_outbox.c.id))
    return [r for r in rows if kind is None or r.kind == kind]


async def events(env: Env, action: str) -> list[Any]:
    return await env.all(sa.select(db.events).where(db.events.c.action == action).order_by(db.events.c.id))


async def day_page(env: Env) -> str:
    return html.unescape((await env.http.get("/office")).text)


async def line_of(env: Env) -> Any:
    """The one outbox row that carries a video's line for FieldRoutes' email."""
    (row,) = await outbox(env, "comment")
    return row


async def the_last_look(env: Env, clock: Clock) -> Any:
    """Time passes until two minutes before the line's deadline, when the server reads the
    visit once more, and the outbox runs. The row as it is after that."""
    row = await line_of(env)
    look_at = datetime.fromisoformat(row.payload["deadline"]) - timedelta(minutes=2)
    if clock.now() < look_at:
        clock.to(look_at + timedelta(seconds=1))
    await videos.run_outbox(env.ctx)
    return await line_of(env)


def looked_at(env: Env, appointment_id: int = 9001) -> int:
    """How often the server has read that one appointment (appointment/get)."""
    return len([r for r in env.fr_calls("appointment", "get") if r.form.get("appointmentIDs[]") == [str(appointment_id)]])


async def released_in_time(env: Env, clock: Clock, customer_id: int = JANE, *appointment_ids: int) -> tuple[dict[str, str], str]:
    """Pat films, completes the visit right after, and his ten minutes pass. The video is
    settled as going into FieldRoutes' email; the outbox has not run yet."""
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, customer_id, recordedAt=just_now(clock))
    completes(env, clock, *(appointment_ids or JANES))
    clock.forward(minutes=11)
    await emailer(env)
    assert ((await env.video(video_id)).state, (await env.video(video_id)).email_skip) == ("no_email", FR_EMAIL)
    return headers, video_id


async def missed(env: Env, clock: Clock) -> tuple[dict[str, str], str]:
    """Pat completed Jane's visit 40 minutes before his video was ready: FieldRoutes' email
    (30 minutes after completion) has gone without it."""
    headers = await ready(env)
    await one_email(env, 30)
    completes(env, clock, *JANES, ago=29)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED)
    return headers, video_id


# == switched off, it is the server it was ===============================================


async def test_it_ships_switched_off(env: Env) -> None:
    assert DEFAULT_SETTINGS["fr_email_minutes"] == 0
    assert (await get_settings(env.ctx))["fr_email_minutes"] == 0
    assert DEFAULT_SETTINGS["fr_email_line"] == "Watch the video of today's visit: {link}"


async def test_switched_off_nothing_changes_and_fieldroutes_is_never_asked_to_complete_anything(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    completes(env, clock, *JANES, ACMES, comments="Sprayed the roses.")
    janes = await film(env, headers, JANE, recordedAt=just_now(clock), recommend=True)
    acmes = await film(env, headers, ACME, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    for _ in range(3):
        await a_minute(env)
        await five_minute_round(env)
        clock.forward(minutes=6)

    assert [m.to for m in our_emails(env)] == ["jane@example.com"], "our own email, as before"
    jane, acme = await env.video(janes), await env.video(acmes)
    assert (jane.state, jane.email_skip) == ("sent", "") and (acme.state, acme.email_skip) == ("no_email", "no_address")
    r = await env.http.post(f"/w/{jane.token}/score", data={"stars": "5", "comment": "Great"})
    assert r.status_code == 303
    await a_minute(env)

    assert sorted(r.kind for r in await outbox(env)) == ["note", "note", "review", "task"]
    assert env.fake.completions == [], "appointment/complete is never called"
    assert env.fr_calls("appointment", "complete") == [] and env.fr_calls("appointment", "get") == []
    assert env.fake.comments(9001) == "Sprayed the roses.", "the technician's comments are never touched"
    assert await env.flags(FR_MISSED) == []
    page = await day_page(env)
    assert "FieldRoutes' email" not in page and "FieldRoutes' own" not in page


async def test_the_fake_fieldroutes_would_complete_a_pending_appointment_as_the_real_one_is_documented_to(env: Env) -> None:
    """Why the write is guarded so heavily: this is what the very same call does to an
    appointment that is not completed. The tests below prove the server never makes it."""
    assert env.fake.data["appointment"][BOBS]["status"] == "0"
    await env.ctx.fr.client.call("appointment", "complete", {"appointmentID": BOBS, "completionNotes": "x"})
    assert env.fake.data["appointment"][BOBS]["status"] == "1", "completed, with nobody having been there"
    assert env.fake.completed_by_api == [BOBS] and env.fake.invoices == [BOBS], "and the invoicing logic ran"

    # On one that is completed already it changes only what was sent.
    before = dict(env.fake.data["appointment"][9001])
    await env.ctx.fr.client.call("appointment", "complete", {"appointmentID": 9001, "completionNotes": "y"})
    after = dict(env.fake.data["appointment"][9001])
    assert after.pop("notes") == "y" and before.pop("notes") == "" and after == before
    assert env.fake.completed_by_api == [BOBS] and env.fake.invoices == [BOBS], "no second invoice"


# == the heart of it ===================================================================


async def test_the_link_rides_in_fieldroutes_email_and_ours_is_not_sent(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock), recommend=True)
    completes(env, clock, *JANES)
    env.fake.data["appointment"][9001]["notes"] = "  Trimmed the hedge.\nThe gate is latched.  "
    clock.forward(minutes=11)
    await a_minute(env)

    video = await env.video(video_id)
    assert (video.state, video.email_skip, video.emailed_at) == ("no_email", FR_EMAIL, None)
    assert our_emails(env) == [], "no email of ours"

    # One call, on his own completed appointment, with exactly two parameters. His own
    # words are kept, line breaks and all, and the line goes under them.
    assert env.fake.completions == [
        {"appointmentID": "9001", "completionNotes": f"Trimmed the hedge.\nThe gate is latched.\n\n{line_for(video)}"}
    ]
    assert env.fake.completed_by_api == [] and env.fake.invoices == [], "nothing was completed, no invoice was made"
    assert env.fake.data["appointment"][9001]["status"] == "1"
    # FieldRoutes' email, 30 minutes after completion, carries it. One line for the video.
    assert line_for(video) in env.fake.comments_in_the_email(9001, 30)
    assert env.fake.comments(9002) == "" and env.fake.comments(9003) == ""

    # What a video that went out always leaves behind is still there: the internal note,
    # the follow-up for "I recommended something", and the audit trail.
    (note,) = env.fr_calls("note", "create")
    assert note.one("showCustomer") == "0" and link_of(video) in note.one("notes")
    assert len(env.fr_calls("task", "create")) == 1 and len(await env.flags("recommend")) == 1
    (settled,) = await events(env, "email_skipped")
    assert settled.detail["reason"] == FR_EMAIL and settled.detail["appointment"] == 9001
    (written,) = await events(env, "fr_email_line")
    assert written.detail == {"appointment": 9001} and written.video_id == video_id
    assert await events(env, "email_sent") == []
    assert [f.kind for f in await env.flags()] == ["recommend"], "nothing for the office to do"
    # The visit was read before the write and again after it: nothing rests on "success".
    assert [f"{r.entity}/{r.action}" for r in env.fake.requests if r.entity == "appointment" and r.action != "search"] == [
        "appointment/get", "appointment/complete", "appointment/get",
    ]

    # For the tech it went out: the stop is locked, and the phone is told nothing new.
    today_body = (await env.http.get("/v1/today", headers=headers)).json()
    stop = next(s for s in today_body["stops"] if s["customerId"] == JANE)
    assert stop["video"] == {"id": video_id, "state": "no_email", "byYou": True}
    r = await env.http.delete(f"/v1/videos/{video_id}", headers=headers)
    assert r.status_code == 409 and "already went to the customer" in r.json()["message"]

    # And the customer's link plays.
    assert (await env.http.get(f"/w/{video.token}")).status_code == 200

    # Any number of further rounds change nothing: still one call. The server looks at
    # the visit once more shortly before FieldRoutes sends, finds the line, and is done.
    for _ in range(4):
        clock.forward(minutes=6)
        await a_minute(env)
        await five_minute_round(env)
    assert len(env.fake.completions) == 1 and our_emails(env) == []
    assert len(env.fr_calls("note", "create")) == 1
    row = await line_of(env)
    assert row.done_at is not None and row.payload["looked_at"] and looked_at(env) == 3
    assert [e.detail for e in await events(env, "fr_email_looked")] == [{"appointment": 9001}]


async def test_a_technician_who_wrote_nothing_gets_just_the_line(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]


async def test_filmed_first_and_completed_later_waits_for_the_completion(env: Env, clock: Clock) -> None:
    not_completed(env, *JANES)
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    await office_login(env)
    assert "Sending at" in await day_page(env), "inside his ten minutes the page reads as it always did"
    clock.forward(minutes=11)
    customer_reads = len(env.fr_calls("customer", "get"))

    # His ten minutes are over and he has not completed the visit in FieldRoutes: there is
    # no FieldRoutes email on the way yet. The video waits, and so does everybody else.
    for _ in range(5):
        await a_minute(env)
        await five_minute_round(env)
        clock.forward(minutes=6)
    video = await env.video(video_id)
    assert video.state == "held" and our_emails(env) == [] and env.fake.completions == []
    assert await outbox(env) == [] and [f.kind for f in await env.flags()] == []
    assert len(env.fr_calls("customer", "get")) == customer_reads, "waiting costs no customer read"
    page = await day_page(env)
    assert "Waiting for the tech to complete the visit" in page and "Sending at" not in page
    # He can still take it back: it has gone nowhere yet.
    assert (await env.http.get("/v1/today", headers=headers)).json()["videos"][0]["state"] == "held"

    # Now he completes it. The next look finds the open window and the line goes in.
    completes(env, clock, *JANES, comments="All done.")
    clock.forward(minutes=6)
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_EMAIL)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": f"All done.\n\n{line_for(video)}"}]
    assert line_for(video) in env.fake.comments_in_the_email(9001, 30) and our_emails(env) == []


@pytest.mark.parametrize("minutes", [30, 60])
async def test_thirty_and_sixty_minutes_each_draw_the_line_five_minutes_before_fieldroutes_sends(
    env: Env, clock: Clock, minutes: int
) -> None:
    """Two videos are let go at the same moment. Jane's visit was completed just early
    enough for the line to be in five minutes before FieldRoutes sends; Bob's a little
    too long ago."""
    headers = await ready(env)
    await one_email(env, minutes)
    # Both are let go 11 minutes from now.
    completes(env, clock, *JANES, ago=minutes - 6 - 11)
    completes(env, clock, BOBS, ago=minutes - 4 - 11)
    janes = await film(env, headers, JANE, recordedAt=just_now(clock))
    bobs = await film(env, headers, BOB, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await a_minute(env)

    jane, bob = await env.video(janes), await env.video(bobs)
    assert (jane.state, jane.email_skip) == ("no_email", FR_EMAIL)
    assert (bob.state, bob.email_skip) == ("no_email", FR_MISSED)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(jane)}]
    assert line_for(jane) in env.fake.comments_in_the_email(9001, minutes)
    assert env.fake.comments_in_the_email(BOBS, minutes) == "" and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert flag.video_id == bobs and f"{minutes} minutes after" in flag.detail
    row = (await outbox(env, "comment"))[0]
    completed = datetime.fromisoformat(row.payload["completed"])
    assert datetime.fromisoformat(row.payload["deadline"]) == completed + timedelta(minutes=minutes - 5)


async def test_a_video_that_is_ready_too_late_waits_for_the_office_and_no_second_email_goes(env: Env, clock: Clock) -> None:
    headers, video_id = await missed(env, clock)
    video = await env.video(video_id)
    assert video.email_skip in HELD_BACK and videos.went_out(video) is False

    assert our_emails(env) == [], "no second email by itself"
    assert env.fake.completions == [], "and nothing is written late"
    assert await outbox(env) == [], "nothing goes to FieldRoutes while it waits for the office"

    # Before it said so, the server looked at the visit: one read of his three appointments.
    (read,) = env.fr_calls("appointment", "get")
    assert read.form["appointmentIDs[]"] == ["9001", "9002", "9003"]
    (flag,) = await env.flags(FR_MISSED)
    assert flag.video_id == video_id and flag.customer_id == JANE and flag.tech_id == PAT
    assert flag.detail.startswith("Pat's video for Jane Smith, 123 Main St was too late for FieldRoutes' email.")
    assert "FieldRoutes sends its email 30 minutes after that" in flag.detail and "too late" in flag.detail
    assert "This server looked at the visit in FieldRoutes at " in flag.detail
    assert ": the link is not in the technician's comments. " in flag.detail
    assert flag.detail.endswith(
        "No second email was sent. Press Send it anyway to send our own email with the link, or Pull."
    )
    (entry,) = await events(env, "email_skipped")
    assert entry.detail == {"reason": FR_MISSED, "why": "shut"}

    # The phone shows SENT TO OFFICE, and the tech cannot change the video himself.
    body = (await env.http.get("/v1/today", headers=headers)).json()
    stop = next(s for s in body["stops"] if s["customerId"] == JANE)
    assert stop["video"] == {"id": video_id, "state": "no_email", "byYou": True, "withOffice": True}
    r = await env.http.delete(f"/v1/videos/{video_id}", headers=headers)
    assert r.status_code == 409 and "The office is looking at this video" in r.json()["message"]

    # The office page says it plainly and offers both buttons.
    await office_login(env)
    page = await day_page(env)
    assert "Video not in FieldRoutes' email: the office decides" in page
    assert "No email: it waits for the office, and its flag says what is known about FieldRoutes' email" in page
    assert f'action="/office/video/{video_id}/send"' in page and ">Send it anyway<" in page
    assert "Read its flag above first: it says whether the link is there." in page
    assert "FieldRoutes sends its own email for the visit too, so that is a second email for the customer." in page

    # However long it waits, nothing goes by itself and the office is told once.
    for _ in range(4):
        clock.forward(minutes=6)
        await a_minute(env)
        await five_minute_round(env)
    assert our_emails(env) == [] and env.fake.completions == [] and len(await env.flags(FR_MISSED)) == 1
    assert (await env.video(video_id)).email_skip == FR_MISSED


# == the write itself ====================================================================


async def test_a_write_that_lands_late_is_not_made_at_all(env: Env, clock: Clock) -> None:
    """FieldRoutes goes down right after the video is let go and stays down until after
    its email was due. A line added then would be in no email, so it is not added."""
    _, video_id = await released_in_time(env, clock)
    (row,) = await outbox(env, "comment")
    deadline = datetime.fromisoformat(row.payload["deadline"])
    env.fake.down = True
    tries = 0
    while clock.now() < deadline:
        await videos.run_outbox(env.ctx)
        tries += 1
        clock.forward(minutes=1)
    (row,) = await outbox(env, "comment")
    assert row.attempts >= 2 and row.done_at is None and row.failed_at is None, "tried again while there was time"
    assert row.next_try_at <= deadline, "and never put off past the deadline"
    assert (await env.video(video_id)).email_skip == FR_EMAIL
    await office_login(env)
    assert "Going into FieldRoutes' email" in await day_page(env)

    env.fake.down = False  # back, a minute too late
    await videos.run_outbox(env.ctx)
    (row,) = await outbox(env, "comment")
    assert row.failed_at is not None and row.done_at is None
    assert env.fake.completions == [] and env.fake.comments(9001) == "", "nothing was written after the deadline"
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert "The link was not added before that email was due (by " in flag.detail and "Press Send it anyway" in flag.detail
    # It read the visit before saying so: an earlier try might have landed after all.
    assert "This server looked at the visit in FieldRoutes at " in flag.detail and looked_at(env) >= 1
    assert [e.detail for e in await events(env, "email_skipped")][-1] == {"reason": FR_MISSED, "why": "late", "was": FR_EMAIL}
    assert "it waits for the office, and its flag says what is known about FieldRoutes' email" in await day_page(env)

    for _ in range(3):
        clock.forward(minutes=6)
        await a_minute(env)
    assert env.fake.completions == [] and our_emails(env) == [] and len(await env.flags(FR_MISSED)) == 1


async def test_fieldroutes_down_for_a_few_minutes_and_the_line_still_makes_the_email(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    env.fake.down = True
    for _ in range(4):
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    assert env.fake.completions == []
    env.fake.down = False
    for _ in range(6):
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    video = await env.video(video_id)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    assert line_for(video) in env.fake.comments_in_the_email(9001, 30)
    assert video.email_skip == FR_EMAIL and await env.flags(FR_MISSED) == []


def reopened(a: dict[str, Any]) -> None:
    a["status"] = "0"


def no_show(a: dict[str, Any]) -> None:
    a["status"] = "2"


def cancelled(a: dict[str, Any]) -> None:
    a["status"] = "-1"


def status_empty(a: dict[str, Any]) -> None:
    a["status"] = ""


def status_missing(a: dict[str, Any]) -> None:
    a.pop("status")


def another_customers(a: dict[str, Any]) -> None:
    a["customerID"] = str(BOB)


@pytest.mark.parametrize(
    "change", [reopened, no_show, cancelled, status_empty, status_missing, another_customers, None],
    ids=["pending", "no-show", "cancelled", "empty status", "no status", "another customer's", "deleted"],
)
async def test_the_write_is_never_made_on_an_appointment_that_is_not_this_customers_completed_one(
    env: Env, clock: Clock, change: Any
) -> None:
    """The appointment was completed when the server last read the day, and the line was
    queued for it. By the time of the write FieldRoutes has it differently. The call would
    complete a pending appointment and make an invoice, so it is not made."""
    _, video_id = await released_in_time(env, clock)
    if change is None:
        env.fake.data["appointment"].pop(9001)
    else:
        change(env.fake.data["appointment"][9001])
    before = {k: dict(v) for k, v in env.fake.data["appointment"].items()}

    await videos.run_outbox(env.ctx)
    assert env.fake.completions == [] and env.fr_calls("appointment", "complete") == []
    assert env.fake.completed_by_api == [] and env.fake.invoices == []
    assert env.fake.data["appointment"] == before, "FieldRoutes' appointments are untouched"

    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert "this server did not add its link to FieldRoutes' email." in flag.detail and "Press Send it anyway" in flag.detail
    assert ("FieldRoutes no longer" in flag.detail) or ("belongs to another customer" in flag.detail)
    (row,) = await outbox(env, "comment")
    assert row.failed_at is not None and row.last_error.startswith("refused:")

    # Refused for good: no later round tries again.
    for _ in range(3):
        clock.forward(minutes=6)
        await a_minute(env)
    assert env.fake.completions == [] and env.fake.completed_by_api == []
    assert len(env.fr_calls("appointment", "get")) == 1


async def test_the_call_carries_two_parameters_and_never_a_status(env: Env, clock: Clock) -> None:
    _, _ = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    (request,) = env.fr_calls("appointment", "complete")
    sent = set(request.form) - {"authenticationKey", "authenticationToken"}
    assert sent == {"appointmentID", "completionNotes"}
    assert "status" not in request.body


async def test_a_parameter_fieldroutes_ignores_is_an_error_and_is_not_hammered(env: Env, clock: Clock) -> None:
    """If FieldRoutes answers "done" and lists completionNotes as ignored, nothing was
    written. Asking again would get the same answer, so it is asked once."""
    _, video_id = await released_in_time(env, clock)
    env.fake.ignore_params = {"completionNotes"}
    for _ in range(4):
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=2)
    assert len(env.fake.completions) == 1 and env.fake.comments(9001) == ""
    assert (await env.video(video_id)).email_skip == FR_MISSED
    (flag,) = await env.flags(FR_MISSED)
    assert "FieldRoutes did not take the comments from this server" in flag.detail


async def test_run_twice_the_line_is_written_once(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    for _ in range(5):
        await a_minute(env)
        clock.forward(minutes=1)
    assert len(env.fake.completions) == 1
    assert env.fake.comments(9001).count(link_of(await env.video(video_id))) == 1
    assert len(await events(env, "fr_email_line")) == 1


async def test_a_line_that_is_already_there_is_not_written_again(env: Env, clock: Clock) -> None:
    """How things stand when an earlier try reached FieldRoutes and its answer was lost."""
    _, video_id = await released_in_time(env, clock)
    video = await env.video(video_id)
    env.fake.data["appointment"][9001]["notes"] = f"Swept the patio.\n\n{line_for(video)}"
    await videos.run_outbox(env.ctx)
    assert env.fake.completions == [], "read, found, left alone"
    (row,) = await outbox(env, "comment")
    assert row.payload["written_at"] and row.failed_at is None and row.last_error == ""
    (written,) = await events(env, "fr_email_line")
    assert written.detail == {"appointment": 9001, "already_there": True}
    assert (await env.video(video_id)).email_skip == FR_EMAIL and await env.flags(FR_MISSED) == []
    row = await the_last_look(env, clock)
    assert row.done_at is not None and env.fake.completions == [], "still there at the last look: done, nothing written"


async def test_the_owner_can_word_the_line(env: Env, clock: Clock) -> None:
    await set_setting(env.ctx, "fr_email_line", "Your tech filmed today's visit. Watch it here: {link}", OWNER)
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert env.fake.comments(9001) == f"Your tech filmed today's visit. Watch it here: {link_of(video)}"


async def test_the_setting_is_a_switch_for_lines_already_on_their_way(env: Env, clock: Clock) -> None:
    """The owner switches it off while a line is waiting to be written. Off means off:
    nothing more is written, and the video waits for the office instead of hanging."""
    _, video_id = await released_in_time(env, clock)
    await one_email(env, 0)
    await videos.run_outbox(env.ctx)
    assert env.fake.completions == [] and env.fr_calls("appointment", "get") == []
    assert (await env.video(video_id)).email_skip == FR_MISSED
    (flag,) = await env.flags(FR_MISSED)
    assert (
        "this server did not add its link to FieldRoutes' email. The setting that puts the link there was "
        "switched off first."
    ) in flag.detail
    # It did not look (off means nothing more is asked of FieldRoutes), so it does not say the link is not there.
    assert "look at the technician's comments on the visit in FieldRoutes before you send anything" in flag.detail


async def test_a_write_that_gets_blocked_never_leaves_a_video_in_limbo(env: Env, clock: Clock) -> None:
    """FieldRoutes writes are switched off between letting the video go and writing its line."""
    _, video_id = await released_in_time(env, clock)
    env.reconfigure(fr_writes=False)
    await videos.run_outbox(env.ctx)
    assert env.fake.completions == []
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED)
    (flag,) = await env.flags(FR_MISSED)
    assert "Writing to FieldRoutes was switched off" in flag.detail
    assert all(r.done_at or r.failed_at for r in await outbox(env)), "no row is left hanging either"


# == the office decides about a missed video =============================================


async def test_send_it_anyway_on_a_missed_video_sends_our_own_email(env: Env, clock: Clock) -> None:
    _, video_id = await missed(env, clock)
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert r.status_code == 303
    assert (await env.video(video_id)).state == "held"
    await a_minute(env)

    video = await env.video(video_id)
    assert video.state == "sent" and [m.to for m in our_emails(env)] == ["jane@example.com"]
    assert link_of(video) in our_emails(env)[0].text
    assert env.fake.completions == [], "the office said our own email, so nothing goes into FieldRoutes' comments"
    assert len(env.fr_calls("note", "create")) == 1, "the link is filed on the account, as for any email"
    assert await env.flags(FR_MISSED) == [], "the flag was answered"
    (entry,) = await events(env, "video_send_anyway")
    assert entry.detail == {"held": FR_MISSED, "route_told": False}

    # It stays our email: no later round moves it back towards FieldRoutes.
    completes(env, clock, *JANES)  # even a fresh completion
    for _ in range(3):
        clock.forward(minutes=6)
        await a_minute(env)
        await five_minute_round(env)
    assert env.fake.completions == [] and len(our_emails(env)) == 1


async def test_with_customer_emails_off_a_missed_video_can_only_be_kept(env: Env, clock: Clock) -> None:
    env.reconfigure(email_mode="off")
    _, video_id = await missed(env, clock)
    (flag,) = await env.flags(FR_MISSED)
    assert flag.detail.endswith(
        "No second email was sent, and customer emails are off, so none can be. Press Keep it to keep the link "
        "on file, or Pull."
    )
    csrf = await office_login(env)
    env.mailer.sent.clear()
    page = await day_page(env)
    assert ">Keep it<" in page and ">Send it anyway<" not in page
    assert "Customer emails are off, so no email can go: Keep it keeps the link on file." in page

    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert r.status_code == 303 and "Kept." in html.unescape(r.headers["location"]).replace("%20", " ")
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "email_off") and our_emails(env) == []
    assert env.fake.completions == [] and len(env.fr_calls("note", "create")) == 1
    assert await env.flags(FR_MISSED) == []


@pytest.mark.parametrize("email_mode", ["live", "off"])
async def test_pull_on_a_missed_video_kills_the_link_and_sends_nothing(env: Env, clock: Clock, email_mode: str) -> None:
    env.reconfigure(email_mode=email_mode)
    _, video_id = await missed(env, clock)
    csrf = await office_login(env)
    env.mailer.sent.clear()
    r = await env.http.post(f"/office/video/{video_id}/pull", data={"csrf": csrf, "reason": "too late to matter"})
    assert r.status_code == 303
    video = await env.video(video_id)
    assert video.state == "pulled"
    assert (await env.http.get(f"/w/{video.token}")).status_code == 410
    for _ in range(3):
        clock.forward(minutes=6)
        await a_minute(env)
        await five_minute_round(env)
    assert our_emails(env) == [] and env.fake.completions == [] and await outbox(env) == []
    assert await env.flags(FR_MISSED) == [], "its flag went with it"


async def test_send_it_anyway_on_a_missed_video_is_no_word_on_the_route(env: Env, clock: Clock) -> None:
    """The flag said the video missed FieldRoutes' email. It said nothing about the route.
    The visit is cancelled before the office presses Send it anyway: the route is still
    looked at before that email goes, as it is for a video that was only late."""
    _, video_id = await missed(env, clock)
    for appointment_id in JANES:
        env.fake.data["appointment"][appointment_id]["status"] = "-1"
    csrf = await office_login(env)
    env.mailer.sent.clear()
    await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "off_route") and our_emails(env) == []
    assert [f.kind for f in await env.flags() if f.video_id == video_id] == ["off_route"]

    # Now the flag does say so. Pressed again, it goes.
    await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    await a_minute(env)
    assert (await env.video(video_id)).state == "sent" and len(our_emails(env)) == 1
    assert env.fake.completions == []


async def test_clearing_the_flag_of_a_missed_video_sends_nothing(env: Env, clock: Clock) -> None:
    _, video_id = await missed(env, clock)
    csrf = await office_login(env)
    env.mailer.sent.clear()
    (flag,) = await env.flags(FR_MISSED)
    await env.http.post(f"/office/flag/{flag.id}/clear", data={"csrf": csrf, "note": "seen"})
    for _ in range(3):
        clock.forward(minutes=6)
        await a_minute(env)
        await five_minute_round(env)
    assert (await env.video(video_id)).email_skip == FR_MISSED and our_emails(env) == [] and env.fake.completions == []
    assert f'action="/office/video/{video_id}/send"' in await day_page(env), "it still waits, and the page still offers both"


# == what stays first, unchanged =========================================================


async def test_only_the_test_customer_while_writes_are_limited_to_the_test_customer(env: Env, clock: Clock) -> None:
    """FR_WRITE_CUSTOMER_IDS names Jane alone: that is how the owner tries the setting on
    one customer. Bob's video goes exactly as it would with the setting off."""
    env.reconfigure(fr_write_customer_ids=frozenset({JANE}))
    headers = await ready(env)
    await one_email(env, 30)
    janes = await film(env, headers, JANE, recordedAt=just_now(clock))
    bobs = await film(env, headers, BOB, recordedAt=just_now(clock))
    completes(env, clock, *JANES, BOBS)
    clock.forward(minutes=11)
    await a_minute(env)

    jane, bob = await env.video(janes), await env.video(bobs)
    assert (jane.state, jane.email_skip) == ("no_email", FR_EMAIL)
    assert (bob.state, bob.email_skip) == ("sent", "") and [m.to for m in our_emails(env)] == ["bob@example.com"]
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(jane)}]
    assert env.fake.comments(BOBS) == ""
    assert [n.one("customerID") for n in env.fr_calls("note", "create")] == [str(JANE)], "Bob's note is skipped, as before"

    await office_login(env)
    page = await day_page(env)
    assert "for the test customers only (501)" in page and "Everybody else gets our own email, as before." in page


async def test_with_fieldroutes_writes_off_every_video_goes_as_before(env: Env, clock: Clock) -> None:
    env.reconfigure(fr_writes=False)
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)
    for _ in range(3):
        await a_minute(env)
        clock.forward(minutes=6)
    assert (await env.video(video_id)).state == "sent" and [m.to for m in our_emails(env)] == ["jane@example.com"]
    assert env.fake.completions == [] and env.fr_calls("appointment", "get") == []
    assert [r.kind for r in await outbox(env)] == ["note"], "no line is even queued"
    await office_login(env)
    page = html.unescape((await env.http.get("/office/settings")).text)
    assert "This does nothing yet." in page and "picked above, but FieldRoutes writes are off, so it does nothing" in page


async def test_customer_emails_off_does_not_stop_the_link_going_into_fieldroutes_email(env: Env, clock: Clock) -> None:
    """Our own emails are off (as on the live server today). The link in FieldRoutes' email
    is not one of ours, so it goes, and the office page does not call the video unsent."""
    env.reconfigure(email_mode="off")
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_EMAIL)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    assert our_emails(env) == []

    await office_login(env)
    page = await day_page(env)
    assert "Link added for FieldRoutes' email" in page
    await the_last_look(env, clock)
    page = await day_page(env)
    assert "In FieldRoutes' email" in page
    assert "Our own customer emails are OFF" in page
    assert "The video link goes into FieldRoutes' own after-service email." in page
    assert "no customer is emailed" not in page, "that would call this video unsent"

    await one_email(env, 0)
    page = await day_page(env)
    assert "Customer emails are OFF. Videos are saved and links work, but no customer is emailed." in page


async def test_a_customer_who_stopped_video_emails_gets_no_line(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 30)
    await env.run(sa.insert(db.email_optouts).values(customer_id=JANE, at=db.now()))
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)
    for _ in range(3):
        await a_minute(env)
        clock.forward(minutes=6)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "opted_out")
    assert env.fake.completions == [] and env.fake.comments(9001) == "" and our_emails(env) == []
    assert [r.kind for r in await outbox(env)] == ["note"], "the internal note, as before; no line"


async def test_no_email_on_file_is_what_it_always_was(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, ACME, recordedAt=just_now(clock))
    completes(env, clock, ACMES)
    clock.forward(minutes=11)
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "no_address")
    (flag,) = await env.flags("no_email")
    assert flag.detail == "No email on file. The link is saved on the account as a note."
    (note,) = env.fr_calls("note", "create")
    assert note.one("showCustomer") == "1" and link_of(video) in note.one("notes")
    assert env.fake.completions == [] and our_emails(env) == []


async def test_email_reminders_off_in_fieldroutes_still_means_no_email_and_no_line(env: Env, clock: Clock) -> None:
    sam = await ready(env, "sam@example.com")
    await one_email(env, 30)
    video_id = await film(env, sam, ANN, recordedAt=just_now(clock))
    completes(env, clock, ANNS)
    clock.forward(minutes=11)
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "fr_reminders_off")
    (flag,) = await env.flags("no_email")
    assert flag.detail == (
        "Email reminders are off for this customer in FieldRoutes, so the video link was not added to the visit "
        "and this server sent no email. FieldRoutes may still have sent its own email, without the link."
    )
    assert env.fake.completions == [] and our_emails(env) == []


async def test_a_visit_taken_off_the_day_is_held_for_the_route_first_as_before(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    for appointment_id in JANES:
        env.fake.data["appointment"][appointment_id]["status"] = "-1"
    clock.forward(minutes=11)
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "off_route")
    assert [f.kind for f in await env.flags()] == ["off_route"] and env.fake.completions == []


async def test_a_visit_never_completed_is_bounded_by_the_overdue_rule_and_nothing_else(env: Env, clock: Clock) -> None:
    not_completed(env, *JANES)
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    clock.forward(hours=5)
    await a_minute(env)
    assert (await env.video(video_id)).state == "held", "still inside the six hours"

    clock.forward(hours=2)
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "overdue")
    (flag,) = await env.flags("overdue")
    assert "It also happens when the visit was not completed in FieldRoutes" in flag.detail
    assert env.fake.completions == [] and our_emails(env) == []

    # The office says send: our own email, whatever FieldRoutes does later.
    csrf = await office_login(env)
    env.mailer.sent.clear()
    await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    await a_minute(env)
    assert (await env.video(video_id)).state == "sent" and len(our_emails(env)) == 1 and env.fake.completions == []


async def test_a_completion_time_ahead_of_the_servers_clock_is_not_guessed_at(env: Env, clock: Clock) -> None:
    """The server's time zone is set wrong, so FieldRoutes' completion times read as hours
    ahead. Written on that reckoning, every line would be 'in time' and none would be."""
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES, ago=-180)
    clock.forward(minutes=11)
    await a_minute(env)
    assert (await env.video(video_id)).email_skip == FR_MISSED and env.fake.completions == []
    (flag,) = await env.flags(FR_MISSED)
    assert "later than this server's own clock" in flag.detail and "time zone" in flag.detail


async def test_a_completed_visit_with_no_completion_time_is_not_guessed_at_either(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    for appointment_id in JANES:
        env.fake.data["appointment"][appointment_id].update(status="1", dateCompleted=NEVER)
    clock.forward(minutes=11)
    await a_minute(env)
    assert (await env.video(video_id)).email_skip == FR_MISSED and env.fake.completions == []
    (flag,) = await env.flags(FR_MISSED)
    assert "gives no time for it" in flag.detail


# == several appointments, several techs ================================================


async def test_three_appointments_in_one_visit_get_one_line_on_the_first_whose_email_has_not_gone(
    env: Env, clock: Clock
) -> None:
    headers = await ready(env)
    await one_email(env, 30)
    completes(env, clock, 9001, ago=30)  # its email goes before the video is ready
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, 9003, comments="Blew the leaves.")
    clock.forward(minutes=2)
    completes(env, clock, 9002)
    clock.forward(minutes=9)
    await a_minute(env)

    video = await env.video(video_id)
    assert video.email_skip == FR_EMAIL
    assert env.fake.completions == [{"appointmentID": "9003", "completionNotes": f"Blew the leaves.\n\n{line_for(video)}"}]
    assert env.fake.comments(9001) == "" and env.fake.comments(9002) == "", "never the same line on two appointments"
    for _ in range(3):
        clock.forward(minutes=6)
        await a_minute(env)
    assert len(env.fake.completions) == 1 and len(await outbox(env, "comment")) == 1


async def test_one_appointment_done_and_an_add_on_left_open_does_not_wait_for_the_add_on(env: Env, clock: Clock) -> None:
    """The main job is completed and the leaf pick-up stays pending for days, as on about
    one real stop in ten. The video goes with the completed one's email."""
    not_completed(env, 9002, 9003)
    _, video_id = await released_in_time(env, clock, JANE, 9001)
    await videos.run_outbox(env.ctx)
    assert [c["appointmentID"] for c in env.fake.completions] == ["9001"]
    assert env.fake.completed_by_api == [], "and the open add-on is not completed by us"


async def sam_at_bobs(env: Env) -> None:
    env.fake.data["appointment"][SAMS_AT_BOBS] = {
        "appointmentID": str(SAMS_AT_BOBS), "officeID": "7", "customerID": str(BOB), "routeID": "71", "type": "80",
        "status": "0", "assignedTech": str(SAM), "employeeID": "22", "date": today(), "start": "07:00:00",
        "sequence": "1", "checkIn": minutes_ago(30), "checkOut": minutes_ago(10),
    }


async def test_two_techs_at_one_property_put_one_line_each_on_their_own_appointments(env: Env, clock: Clock) -> None:
    await sam_at_bobs(env)
    pat, sam = await ready(env), await ready(env, "sam@example.com")
    await one_email(env, 30)
    pats = await film(env, pat, BOB, recordedAt=just_now(clock))
    sams = await film(env, sam, BOB, recordedAt=just_now(clock))
    completes(env, clock, BOBS, comments="Mowed.")
    completes(env, clock, SAMS_AT_BOBS, comments="Fed the lawn.")
    clock.forward(minutes=11)
    await a_minute(env)

    pats_video, sams_video = await env.video(pats), await env.video(sams)
    assert {pats_video.email_skip, sams_video.email_skip} == {FR_EMAIL} and our_emails(env) == []
    assert sorted(env.fake.completions, key=lambda c: c["appointmentID"]) == [
        {"appointmentID": str(BOBS), "completionNotes": f"Mowed.\n\n{line_for(pats_video)}"},
        {"appointmentID": str(SAMS_AT_BOBS), "completionNotes": f"Fed the lawn.\n\n{line_for(sams_video)}"},
    ]


async def test_a_helpers_video_puts_its_line_on_the_leads_appointment(env: Env, clock: Clock) -> None:
    env.fake.data["appointment"][9001]["additionalTechs"] = str(SAM)  # Sam rides along at Jane's
    await ready(env)
    sam = await ready(env, "sam@example.com")
    await one_email(env, 30)
    video_id = await film(env, sam, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)
    await a_minute(env)
    video = await env.video(video_id)
    assert video.tech_id == SAM and video.email_skip == FR_EMAIL
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]


async def test_a_video_tied_to_no_visit_on_the_day_has_no_fieldroutes_email_to_ride_in(env: Env, clock: Clock) -> None:
    """The office matched a not-on-my-list video to a customer who has no visit that day.
    There is no FieldRoutes email for the link to go in, and our own does not go by itself."""
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, None, recordedAt=just_now(clock))
    csrf = await office_login(env)
    env.mailer.sent.clear()
    r = await env.http.post(f"/office/video/{video_id}/move", data={"csrf": csrf, "customer_id_other": "505"})
    assert r.status_code == 303 and "the video waits here for you" in html.unescape(r.headers["location"]).replace("%20", " ")
    await a_minute(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert "has no FieldRoutes email to go in. FieldRoutes has no visit of Pat's at that stop on this day" in flag.detail
    assert "before you send anything" not in flag.detail, "there is no visit to look at"


# == the server is stopped, or there are two of it =======================================


async def test_killed_just_as_the_video_is_settled_the_line_is_still_queued_and_written_once(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 60)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)

    async def it_is_settled() -> bool:
        return (await env.video(video_id)).state == "no_email"

    switch = kill_switch(env, when=it_is_settled)
    the_list_is_old(env)
    with pytest.raises(Killed):
        await videos.send_due(env.ctx)
    assert switch.dead
    # Settled, and everything that follows from it was in that one save.
    assert sorted(r.kind for r in await outbox(env)) == ["comment", "note"]

    clock.forward(minutes=11)
    await restart(env)
    video = await env.video(video_id)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    assert len(env.fr_calls("note", "create")) == 1 and our_emails(env) == []


async def test_a_write_cut_off_in_the_middle_is_tried_again_and_still_written_once(env: Env, clock: Clock) -> None:
    """Unlike a note, this write may be tried again: it reads the comments first and adds
    nothing when the line is there. Here the server dies before FieldRoutes got the call."""
    headers = await ready(env)
    await one_email(env, 60)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)
    await emailer(env)

    async def the_row_is_claimed() -> bool:
        return (await outbox(env, "comment"))[0].last_error == videos.WRITING

    switch = kill_switch(env, when=the_row_is_claimed)
    with pytest.raises(Killed):
        await videos.run_outbox(env.ctx)
    assert switch.dead and env.fake.completions == []

    clock.forward(minutes=11)  # past the dead server's claim, well inside the hour
    await restart(env)
    video = await env.video(video_id)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    assert video.email_skip == FR_EMAIL and await env.flags(FR_MISSED) == [] and await env.flags("fr_write_failed") == []
    assert len(env.fr_calls("note", "create")) == 1


async def test_killed_after_fieldroutes_took_the_line_and_before_that_was_written_down(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 60)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES, comments="Edged the beds.")
    clock.forward(minutes=11)
    await emailer(env)
    write = env.ctx.fr.add_to_tech_comments

    async def dies_after(*args: Any, **kwargs: Any) -> Any:
        await write(*args, **kwargs)
        raise Killed

    env.ctx.fr.add_to_tech_comments = dies_after  # type: ignore[method-assign]
    with pytest.raises(Killed):
        await videos.run_outbox(env.ctx)
    assert len(env.fake.completions) == 1 and (await outbox(env, "comment"))[0].last_error == videos.WRITING

    clock.forward(minutes=11)
    await restart(env)
    video = await env.video(video_id)
    assert len(env.fake.completions) == 1, "the second try read the comments, found the line and wrote nothing"
    assert env.fake.comments(9001) == f"Edged the beds.\n\n{line_for(video)}"
    (row,) = await outbox(env, "comment")
    assert row.payload["written_at"] and video.email_skip == FR_EMAIL and await env.flags(FR_MISSED) == []
    row = await the_last_look(env, clock)
    assert row.done_at is not None and len(env.fake.completions) == 1


async def test_cut_off_before_fieldroutes_got_the_line_and_back_too_late_the_server_looks_before_it_says_so(
    env: Env, clock: Clock
) -> None:
    _, video_id = await released_in_time(env, clock)

    async def the_row_is_claimed() -> bool:
        return (await outbox(env, "comment"))[0].last_error == videos.WRITING

    kill_switch(env, when=the_row_is_claimed)
    with pytest.raises(Killed):
        await videos.run_outbox(env.ctx)

    clock.forward(minutes=40)  # FieldRoutes' email has gone by now
    await restart(env)
    assert env.fake.completions == [], "past the deadline nothing is written"
    assert (await env.video(video_id)).email_skip == FR_MISSED
    (flag,) = await env.flags(FR_MISSED)
    # It does not guess whether the dead server's write got through. It reads the visit.
    assert looked_at(env) == 1
    assert "was too late for FieldRoutes' email" in flag.detail
    assert "This server looked at the visit in FieldRoutes at " in flag.detail
    assert "the link is not in the technician's comments" in flag.detail


async def test_cut_off_after_fieldroutes_took_the_line_and_back_too_late_it_is_found_and_nobody_is_told_it_missed(
    env: Env, clock: Clock
) -> None:
    """The server died between FieldRoutes taking the line and that being written down, and
    came back after FieldRoutes' email had gone. The customer has the link. A flag that
    said "not in FieldRoutes' email" would have the office send a second one."""
    _, video_id = await released_in_time(env, clock)
    write = env.ctx.fr.add_to_tech_comments

    async def dies_after(*args: Any, **kwargs: Any) -> Any:
        await write(*args, **kwargs)
        raise Killed

    env.ctx.fr.add_to_tech_comments = dies_after  # type: ignore[method-assign]
    with pytest.raises(Killed):
        await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert line_for(video) in env.fake.comments_in_the_email(9001, 30)

    clock.forward(minutes=40)
    await restart(env)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_EMAIL) and await env.flags() == []
    row = await line_of(env)
    assert row.done_at is not None and row.payload["found_at"] and len(env.fake.completions) == 1
    (found,) = await events(env, "fr_email_line")
    assert found.detail == {"appointment": 9001, "already_there": True}
    await office_login(env)
    assert "In FieldRoutes' email" in await day_page(env) and our_emails(env) == []


async def test_two_servers_side_by_side_write_one_line(env: Env, clock: Clock) -> None:
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)

    new = replace(env)
    new.ctx = build_ctx(
        replace(env.ctx.cfg), fr_client=_client_for(env.fake), storage=env.storage, mailer=env.mailer,
        texter=env.texter, pusher=env.pusher,
    )
    for server in (env, new):  # a real FieldRoutes call takes a moment; so does this one
        write = server.ctx.fr.add_to_tech_comments

        async def slow(*args: Any, _write: Any = write, **kwargs: Any) -> Any:
            await asyncio.sleep(0.05)
            return await _write(*args, **kwargs)

        server.ctx.fr.add_to_tech_comments = slow  # type: ignore[method-assign]
    try:
        # Both let the video go in the same instant, and both run the outbox in the same instant.
        the_list_is_old(env)
        await asyncio.gather(videos.send_due(env.ctx), videos.send_due(new.ctx))
        await asyncio.gather(videos.run_outbox(env.ctx), videos.run_outbox(new.ctx))
        await asyncio.gather(videos.run_outbox(env.ctx), videos.run_outbox(new.ctx))
    finally:
        await new.ctx.engine.dispose()
    video = await env.video(video_id)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    assert len(await outbox(env, "comment")) == 1 and len(await events(env, "fr_email_line")) == 1
    assert len(env.fr_calls("note", "create")) == 1 and our_emails(env) == []


async def test_a_whole_start_of_the_real_job_loop_carries_a_video_into_fieldroutes_email(env: Env, clock: Clock) -> None:
    """Not the jobs called by hand: the server's own loop, after a restart."""
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)
    await restart(env)
    video = await env.video(video_id)
    assert (await get_settings(env.ctx))["fr_email_minutes"] == 30, "the owner's choice is in the database"
    assert video.email_skip == FR_EMAIL
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    await one_pass(env)
    assert len(env.fake.completions) == 1 and our_emails(env) == []


# == after it went out ==================================================================


async def test_a_video_pulled_before_its_line_was_written_gets_no_line(env: Env, clock: Clock) -> None:
    """A line written now would put a dead link into FieldRoutes' email."""
    _, video_id = await released_in_time(env, clock)
    csrf = await office_login(env)
    await env.http.post(f"/office/video/{video_id}/pull", data={"csrf": csrf, "reason": "wrong house"})
    await videos.run_outbox(env.ctx)
    assert env.fake.completions == [] and env.fr_calls("appointment", "get") == []
    (row,) = await outbox(env, "comment")
    assert row.done_at is not None and row.last_error.startswith("skipped:")
    assert await env.flags(FR_MISSED) == [], "the office pulled it: nothing to tell them"


async def test_pulled_after_the_line_went_in_the_link_says_no_longer_available_and_the_line_stays(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    csrf = await office_login(env)
    await env.http.post(f"/office/video/{video_id}/pull", data={"csrf": csrf, "reason": ""})
    page = await env.http.get(f"/w/{video.token}")
    assert page.status_code == 410 and "no longer available" in html.unescape(page.text)
    for _ in range(2):
        clock.forward(minutes=6)
        await a_minute(env)
        await five_minute_round(env)
    assert env.fake.comments(9001) == line_for(video), "nothing takes the line back out of FieldRoutes"
    assert len(env.fake.completions) == 1


async def test_not_my_home_on_a_video_from_fieldroutes_email_works_as_for_any_video(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    r = await env.http.post(f"/w/{video.token}/not-my-home")
    assert r.status_code in (200, 303)
    assert (await env.video(video_id)).state == "pulled" and len(await env.flags("not_my_home")) == 1
    assert len(env.fake.completions) == 1


async def test_a_score_on_a_video_from_fieldroutes_email_is_saved_and_filed(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    r = await env.http.post(f"/w/{video.token}/score", data={"stars": "5", "comment": "Lovely"})
    assert r.status_code == 303
    await videos.run_outbox(env.ctx)
    (review,) = env.fr_calls("review", "create")
    assert review.one("appointmentID") == "9001" and review.one("starRating") == "5"
    assert len(env.fake.completions) == 1


async def test_moved_to_another_customer_the_new_link_goes_into_that_customers_fieldroutes_email(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    old = await env.video(video_id)
    completes(env, clock, BOBS)
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{video_id}/move", data={"csrf": csrf, "customer_id": str(BOB)})
    assert r.status_code == 303
    await a_minute(env)
    new = await env.video(video_id)
    assert new.customer_id == BOB and new.token != old.token and new.email_skip == FR_EMAIL
    assert env.fake.comments(BOBS) == line_for(new)
    assert env.fake.comments(9001) == line_for(old), "the old line stays; its link now says no longer available"
    assert (await env.http.get(f"/w/{old.token}")).status_code in (404, 410)
    assert len(env.fake.completions) == 2 and our_emails(env) == []


async def test_a_video_kept_as_long_as_the_settings_allow_is_removed_like_any_other(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    clock.forward(days=30 * 12 + 2)
    await jobs.daily(env.ctx)
    video = await env.video(video_id)
    assert video.state == "pulled" and video.object_deleted and len(env.fake.completions) == 1


# == the office page ====================================================================


async def test_the_day_page_and_the_stats_say_where_the_video_is(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await office_login(env)
    page = await day_page(env)
    assert "Going into FieldRoutes' email" in page and "In FieldRoutes' email" not in page
    assert "One email: the video link goes into FieldRoutes' own after-service email." in page
    assert f'action="/office/video/{video_id}/send"' not in page, "it is not waiting for the office"
    (row,) = [r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera"]
    assert (row["videos"], row["sent"]) == (1, 0), "not counted as sent before the line is in"

    # The line is in the technician's comments. FieldRoutes has not sent yet, and the
    # comments can still be saved again without it, so the page says what is known.
    await videos.run_outbox(env.ctx)
    line = await line_of(env)
    added = datetime.fromisoformat(line.payload["written_at"]).astimezone(TZ).strftime("%-I:%M %p")
    page = await day_page(env)
    assert f"Link added for FieldRoutes' email · {added}" in page
    assert "Going into FieldRoutes' email" not in page and "In FieldRoutes' email" not in page
    (row,) = [r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera"]
    assert (row["videos"], row["sent"]) == (1, 0), "nor before the server has looked once more"

    # Two minutes before the last moment a line can be added, the server reads the visit
    # again. The line is still there: only now does the page say so, and the stats count it.
    line = await the_last_look(env, clock)
    assert line.done_at is not None
    page = await day_page(env)
    assert f"In FieldRoutes' email · {added}" in page and "Link added for" not in page
    (row,) = [r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera"]
    assert (row["videos"], row["sent"], row["watched"]) == (1, 1, 0)

    video = await env.video(video_id)
    await videos.mark_played(env.ctx, video)
    (row,) = [r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera"]
    assert (row["sent"], row["watched"], row["watch_rate"]) == (1, 1, 100)
    stats_page = html.unescape((await env.http.get("/office/stats")).text)
    assert "videos whose link is in FieldRoutes' own email" in stats_page

    # The audit trail has the whole life.
    trail = html.unescape((await env.http.get(f"/office/audit?video={video_id}")).text)
    assert "fr_email_line" in trail and "email_skipped" in trail and "fr_email_looked" in trail


async def test_in_test_mode_the_page_says_fieldroutes_email_goes_to_the_real_customer(env: Env, clock: Clock) -> None:
    env.reconfigure(email_mode="test", email_test_to="inbox@zest.test")
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    assert len(env.fake.completions) == 1 and our_emails(env) == [], "no test email either: ours is not the one that goes"
    await office_login(env)
    page = await day_page(env)
    assert "TEST MODE." in page
    assert "The video link itself goes into FieldRoutes' own after-service email, and that email goes to the real customer." in page


# == the settings page ==================================================================


async def test_the_settings_page_offers_three_choices_and_says_what_must_be_true_in_fieldroutes(env: Env) -> None:
    csrf = await office_login(env)
    page = html.unescape((await env.http.get("/office/settings")).text)
    assert '<select id="fr_email_minutes" name="fr_email_minutes">' in page
    assert '<option value="0" selected>Our own email (as it has always been)</option>' in page
    assert '<option value="30">' in page and '<option value="60">' in page and '<option value="45">' not in page
    assert "{{techComments}}" in page and "the choice here is the same as that one" in page
    assert "Admin > Preferences > Customer Preferences > Customer Communication" in page
    assert "No second email goes by itself." in page and "Overdue hours (above) cannot be 0" in page
    assert 'name="fr_email_line" value="Watch the video of today\'s visit: {link}"' in page
    assert "off: our own email carries the link" in page

    r = await env.http.post("/office/settings", data={"csrf": csrf, "fr_email_minutes": "30"})
    assert r.status_code == 303 and "msg=Saved." in r.headers["location"]
    assert (await get_settings(env.ctx))["fr_email_minutes"] == 30
    page = html.unescape((await env.http.get("/office/settings")).text)
    assert '<option value="30" selected>' in page and "on, 30 minutes" in page
    changed = [e.detail for e in await events(env, "setting_changed")]
    assert {"key": "fr_email_minutes", "from": 0, "to": 30} in changed

    for bad in ("45", "15", "-30", "soon", ""):
        r = await env.http.post("/office/settings", data={"csrf": csrf, "fr_email_minutes": bad})
        assert "err=" in r.headers["location"], bad
    assert (await get_settings(env.ctx))["fr_email_minutes"] == 30


async def test_the_settings_page_refuses_a_hold_that_does_not_fit_inside_fieldroutes_wait(env: Env) -> None:
    csrf = await office_login(env)

    async def save(**form: str) -> str:
        r = await env.http.post("/office/settings", data={"csrf": csrf, **form})
        assert r.status_code == 303
        return html.unescape(r.headers["location"]).replace("%20", " ").replace("%27", "'").replace("%3A", ":")

    async def now() -> tuple[int, int]:
        settings = await get_settings(env.ctx)
        return settings["hold_minutes"], settings["fr_email_minutes"]

    # The default hold of 10 minutes fits inside 30.
    assert "msg=Saved." in await save(fr_email_minutes="30")
    assert await now() == (10, 30)

    # A longer hold is refused while 30 is saved, in one plain sentence. 20 is the most.
    said = await save(hold_minutes="25")
    assert "err=The tech's hold cannot be 25 minutes while the video link goes into FieldRoutes' email 30 minutes after a visit: 20 minutes is the most." in said
    assert await now() == (10, 30)
    assert "msg=Saved." in await save(hold_minutes="20")
    assert "err=" in await save(hold_minutes="21")
    assert await now() == (20, 30)

    # And the other way round: 30 is refused while the hold is too long for it.
    assert "msg=Saved." in await save(fr_email_minutes="0")
    assert "msg=Saved." in await save(hold_minutes="45")
    said = await save(fr_email_minutes="30")
    assert "err=FieldRoutes' email cannot carry the video link at 30 minutes while the tech's hold is 45 minutes: make the hold 20 minutes or less first." in said
    assert await now() == (45, 0)
    assert "msg=Saved." in await save(fr_email_minutes="60"), "45 fits inside 60"
    assert await now() == (45, 60)

    # Both changed on one form are judged as the pair they will be.
    assert "msg=Saved." in await save(hold_minutes="15", fr_email_minutes="30")
    assert await now() == (15, 30)
    said = await save(hold_minutes="55", fr_email_minutes="60")
    assert "err=A hold of 55 minutes does not fit inside FieldRoutes' 60 minutes: 50 minutes is the most. None of these was changed." in said
    assert await now() == (15, 30)
    # The whole form saved again with nothing changed is not complained about.
    assert "msg=Saved." in await save(hold_minutes="15", fr_email_minutes="30", max_seconds="15")

    # The same rule holds wherever a setting is saved from.
    with pytest.raises(ValueError, match="20 minutes is the most"):
        await set_setting(env.ctx, "hold_minutes", "30", OWNER)


async def test_the_wait_for_a_visit_to_be_completed_must_have_a_limit(env: Env) -> None:
    """A video waits for its visit to be completed, and Overdue hours is the only limit on
    that wait. At 0 (the rule switched off) a visit that is never completed would keep a
    video waiting, and FieldRoutes being read for it, for good. So the two cannot be saved
    together, whichever is tried second."""
    csrf = await office_login(env)

    async def save(**form: str) -> str:
        r = await env.http.post("/office/settings", data={"csrf": csrf, **form})
        return html.unescape(r.headers["location"]).replace("%20", " ").replace("%27", "'").replace("%3A", ":")

    assert "msg=Saved." in await save(fr_email_minutes="30")
    said = await save(overdue_hours="0")
    assert "err=Overdue hours cannot be 0 while the video link goes into FieldRoutes' email" in said
    assert "this number is the only limit on that wait." in said
    assert (await get_settings(env.ctx))["overdue_hours"] == 6
    assert "msg=Saved." in await save(overdue_hours="24"), "any other number is the owner's to pick"

    assert "msg=Saved." in await save(fr_email_minutes="0")
    assert "msg=Saved." in await save(overdue_hours="0"), "with our own email, 0 is allowed as it always was"
    said = await save(fr_email_minutes="60")
    assert "err=FieldRoutes' email cannot carry the video link while Overdue hours is 0" in said
    assert "Set it to 1 or more first." in said
    assert (await get_settings(env.ctx))["fr_email_minutes"] == 0
    assert "msg=Saved." in await save(fr_email_minutes="60", overdue_hours="6"), "both on one form, judged together"
    settings = await get_settings(env.ctx)
    assert (settings["fr_email_minutes"], settings["overdue_hours"]) == (60, 6)


async def test_what_fieldroutes_says_about_completion_is_kept_with_the_stop(env: Env, clock: Clock) -> None:
    """The completion time is one more key in the appointments saved with a stop. No new column."""
    completes(env, clock, 9001, ago=7)
    env.fake.data["appointment"][9002].update(status="1", dateCompleted=NEVER)
    not_completed(env, 9003)
    env.fake.data["appointment"][BOBS].pop("dateCompleted", None)
    await ready(env)
    stop = await env.one(sa.select(db.stops).where(db.stops.c.customer_id == JANE))
    by_id = {a["id"]: a for a in stop.appointments}
    completed = datetime.fromisoformat(by_id[9001]["completedAt"])
    assert abs(completed - (clock.now() - timedelta(minutes=7))) < timedelta(seconds=2)
    assert by_id[9002]["completedAt"] is None, "completed, with no time given"
    assert by_id[9003]["completedAt"] is None and by_id[9003]["status"] == 0
    bobs = await env.one(sa.select(db.stops).where(db.stops.c.customer_id == BOB))
    assert bobs.appointments[0]["completedAt"] is None
    assert "completedAt" not in {c.name for c in db.stops.c} and "completed_at" not in {c.name for c in db.stops.c}


async def test_the_line_must_be_one_line_with_the_link_in_it_once(env: Env) -> None:
    csrf = await office_login(env)
    for bad in (
        "Watch the video of today's visit",  # no link at all
        "{link} or {link}",  # twice
        "Watch it:\n{link}",  # two lines
        "x" * 195 + "{link}",  # 201 characters
        "   ",
    ):
        r = await env.http.post("/office/settings", data={"csrf": csrf, "fr_email_line": bad})
        assert "err=" in r.headers["location"], bad
    assert (await get_settings(env.ctx))["fr_email_line"] == DEFAULT_SETTINGS["fr_email_line"]
    r = await env.http.post("/office/settings", data={"csrf": csrf, "fr_email_line": "x" * 194 + "{link}"})
    assert "msg=Saved." in r.headers["location"], "200 characters is allowed"


# == the phone is told nothing new =======================================================


async def test_the_phones_keys_are_what_they_were(env: Env, clock: Clock) -> None:
    headers, video_id = await released_in_time(env, clock)
    going = (await env.http.get("/v1/today", headers=headers)).json()
    await videos.run_outbox(env.ctx)
    there = (await env.http.get("/v1/today", headers=headers)).json()
    for body in (going, there):
        stop = next(s for s in body["stops"] if s["customerId"] == JANE)
        assert set(stop) == {
            "customerId", "name", "lastName", "address", "cityLine", "lat", "lng", "appointmentIds", "services",
            "order", "checkIn", "checkOut", "done", "video",
        }
        assert stop["video"] == {"id": video_id, "state": "no_email", "byYou": True}
        assert [set(v) for v in body["videos"]] == [{"id", "customerId", "state", "emailAt"}]
        assert set(body["config"]) == {
            "maxSeconds", "holdMinutes", "farMeters", "minBuild", "requireVideo", "recommendButton", "leaveReminders",
            "prompts", "officePhone",
        }
    video = await env.video(video_id)
    text = (await env.http.get("/v1/today", headers=headers)).text
    assert video.token not in text and "FieldRoutes" not in text and "completedAt" not in text

    # A missed one is with the office: the one optional key the phone already knows.
    await env.run(sa.update(db.videos).where(db.videos.c.id == video_id).values(email_skip=FR_MISSED))
    body = (await env.http.get("/v1/today", headers=headers)).json()
    stop = next(s for s in body["stops"] if s["customerId"] == JANE)
    assert stop["video"] == {"id": video_id, "state": "no_email", "byYou": True, "withOffice": True}
    assert [set(v) for v in body["videos"]] == [{"id", "customerId", "state", "emailAt", "withOffice"}]


async def test_fieldroutes_cannot_be_read_when_the_line_is_due_so_nothing_is_guessed(env: Env, clock: Clock) -> None:
    """FieldRoutes is down at the moment the video is let go. Whether the visit is still on
    the day cannot be said, so the video waits, exactly as it does with the setting off."""
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES)
    clock.forward(minutes=11)
    env.fake.down = True
    try:
        await a_minute(env)
    except FieldRoutesError:
        pytest.fail("a FieldRoutes outage must not fail the job")
    assert (await env.video(video_id)).state == "held" and env.fake.completions == [] and our_emails(env) == []


# == money: the read and the write belong together =======================================
#
# `appointment/complete` on a visit that is not completed completes it and makes an
# invoice. The server reads the visit first; these tests are about everything that could
# come between that read and the write, and about noticing what could not be prevented.


def what_of(request: httpx.Request) -> str:
    return "/".join(request.url.path.split("/")[-2:])


def fieldroutes_does(env: Env, change: Any, **client: Any) -> None:
    """Put something between the server and the fake FieldRoutes. `change(request, answer)`
    sees every request and returns what FieldRoutes answers; `answer()` is what the fake
    would have said. `client` are settings for the server's own FieldRoutes client."""
    inner = env.fake.handler

    def handler(request: httpx.Request) -> httpx.Response:
        return change(request, lambda: inner(request))

    env.fake.handler = handler
    if client:
        fr_client = FieldRoutesClient(
            subdomain="zest", auth_key="key", auth_token="token", transport=httpx.MockTransport(handler), **client
        )
    else:
        fr_client = _client_for(env.fake)
    env.ctx.fr = FieldRoutes(fr_client, env.ctx.cfg)


def asked_of_the_visit(env: Env) -> list[str]:
    return [f"{r.entity}/{r.action}" for r in env.fake.requests if r.entity == "appointment" and r.action != "search"]


async def test_the_write_is_sent_once_and_a_timeout_is_never_followed_by_a_second_send_without_a_fresh_read(
    env: Env, clock: Clock
) -> None:
    """The first send of appointment/complete dies on the wire: FieldRoutes never got it.
    While the server waits, the office sets the visit back to pending. The shared client
    would send the very same request again by itself, and that second send would complete
    the visit and invoice it."""
    _, video_id = await released_in_time(env, clock)
    sent: list[str] = []

    def the_first_send_is_lost(request: httpx.Request, answer: Any) -> httpx.Response:
        if what_of(request).startswith("appointment/"):
            sent.append(what_of(request))
        if what_of(request) == "appointment/complete" and sent.count("appointment/complete") == 1:
            env.fake.data["appointment"][9001].update(status="0", dateCompleted=NEVER)
            raise httpx.ReadTimeout("timed out", request=request)
        return answer()

    fieldroutes_does(env, the_first_send_is_lost)
    await videos.run_outbox(env.ctx)
    assert sent == ["appointment/get", "appointment/complete"], "one read, one send, and the failure handed back"
    assert env.fake.completed_by_api == [] and env.fake.invoices == []
    row = await line_of(env)
    assert row.attempts == 1 and row.done_at is None and row.failed_at is None, "it comes back, as any failed write does"

    # The next try is a new try: it reads first, and what it reads now says no.
    clock.forward(minutes=1)
    await videos.run_outbox(env.ctx)
    assert sent == ["appointment/get", "appointment/complete", "appointment/get"]
    assert env.fake.completed_by_api == [] and env.fake.invoices == [], "the pending visit was never completed by us"
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED)
    (flag,) = await env.flags(FR_MISSED)
    assert "FieldRoutes no longer shows that appointment as completed" in flag.detail


async def test_nothing_waits_between_reading_the_visit_and_writing_to_it(env: Env, clock: Clock) -> None:
    """The client keeps under FieldRoutes' calls-per-minute limit by sleeping, up to a
    minute. Between the read and the write that sleep would be a minute in which the visit
    can change. So the line needs room for all its calls before it starts, and if the
    room is gone after the read, nothing is written on that read."""
    _, video_id = await released_in_time(env, clock)

    # Room for two calls only: not even the read is made.
    tight = RateLimiter(limit=3, window=5)
    await tight.acquire()
    started = time.monotonic()
    fieldroutes_does(env, lambda request, answer: answer(), rate_limiter=tight)
    await videos.run_outbox(env.ctx)
    assert asked_of_the_visit(env) == [] and env.fake.completions == []
    assert time.monotonic() - started < 2, "refused at once, not after waiting for room"
    row = await line_of(env)
    assert row.attempts == 1 and "limit" in row.last_error and row.failed_at is None

    # Room at the start, and other calls take it while the visit is being read.
    class FullAfterTheRead(RateLimiter):
        asked = 0

        def free_slots(self) -> int:
            self.asked += 1
            return 3 if self.asked == 1 else 0

    fieldroutes_does(env, lambda request, answer: answer(), rate_limiter=FullAfterTheRead(limit=100000))
    clock.forward(minutes=1)
    started = time.monotonic()
    await videos.run_outbox(env.ctx)
    assert asked_of_the_visit(env) == ["appointment/get"], "read, and then not written: the write would have had to wait"
    assert time.monotonic() - started < 2, "and it did not sit waiting either"
    assert env.fake.completions == [] and (await line_of(env)).attempts == 2

    # With room, the next try reads again and writes at once.
    fieldroutes_does(env, lambda request, answer: answer())
    clock.forward(minutes=2)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert asked_of_the_visit(env) == ["appointment/get", "appointment/get", "appointment/complete", "appointment/get"]
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]


async def test_a_visit_set_back_to_pending_in_the_instant_between_the_read_and_the_write_is_told_to_the_office(
    env: Env, clock: Clock
) -> None:
    """The one moment nothing outside FieldRoutes can close: the visit is reopened after
    FieldRoutes answered the read and before it took the write, a fraction of a second.
    The call then completes the visit, as documented. The server cannot prevent that.
    It must not miss it: it reads the visit again and tells the office at once."""
    _, video_id = await released_in_time(env, clock)

    def reopened_just_then(request: httpx.Request, answer: Any) -> httpx.Response:
        response = answer()
        if what_of(request) == "appointment/get" and not env.fake.completions:
            env.fake.data["appointment"][9001].update(status="0", dateCompleted=NEVER)
        return response

    fieldroutes_does(env, reopened_just_then)
    await videos.run_outbox(env.ctx)
    assert env.fake.completed_by_api == [9001], "what FieldRoutes is documented to do with that call"

    (flag,) = await env.flags("fr_changed")
    assert flag.video_id == video_id and flag.customer_id == JANE
    assert flag.detail.startswith(
        "Pat's video for Jane Smith, 123 Main St: this server added the video link to the technician's comments on "
        "the visit, then looked at the visit again. More than the comments read differently: its completion time."
    )
    assert "This server sent the comments and nothing else." in flag.detail
    assert "Look at the visit and its invoice in FieldRoutes now, and at QuickBooks." in flag.detail
    (written,) = await events(env, "fr_email_line")
    assert written.detail == {"appointment": 9001, "changed": ["dateCompleted"]}
    await office_login(env)
    assert "A visit changed in FieldRoutes when a video link was added: check it" in await day_page(env)


async def test_if_fieldroutes_changes_more_than_the_comments_when_the_line_is_added_the_office_is_told(
    env: Env, clock: Clock
) -> None:
    """FieldRoutes' help page says the call keeps every field that is not sent. Nobody has
    seen it do so. If it moves the completion time or who completed the visit, the first
    line ever written says so, on the office page, instead of that going on unseen."""
    _, video_id = await released_in_time(env, clock)
    env.fake.recompletion_changes = {
        "dateCompleted": fr_time(clock.now()), "completedBy": "9", "dateUpdated": fr_time(clock.now()),
    }
    await videos.run_outbox(env.ctx)
    assert env.fake.completed_by_api == [] and len(env.fake.completions) == 1
    (flag,) = await env.flags("fr_changed")
    assert "More than the comments read differently: who completed it and its completion time." in flag.detail
    assert 'set "One email" on the Settings page back to "Our own email" and tell the owner' in flag.detail
    (written,) = await events(env, "fr_email_line")
    assert written.detail == {
        "appointment": 9001, "changed": ["completedBy", "dateCompleted"], "also_changed": ["dateUpdated"],
    }
    # The link itself is in, and the video is not taken back for it: the flag is the office's to read.
    video = await env.video(video_id)
    assert video.email_skip == FR_EMAIL and link_of(video) in env.fake.comments(9001)


async def test_a_field_that_only_says_when_the_visit_was_last_touched_is_written_down_and_raises_no_flag(
    env: Env, clock: Clock
) -> None:
    _, _ = await released_in_time(env, clock)
    env.fake.recompletion_changes = {"dateUpdated": fr_time(clock.now())}
    await videos.run_outbox(env.ctx)
    assert await env.flags("fr_changed") == []
    (written,) = await events(env, "fr_email_line")
    assert written.detail == {"appointment": 9001, "also_changed": ["dateUpdated"]}


async def test_what_fieldroutes_says_in_its_answer_is_kept(env: Env, clock: Clock) -> None:
    """The call "returns an array of system messages (usually soft errors or warnings)"."""
    _, _ = await released_in_time(env, clock)
    env.fake.complete_says = ["Invoice 4411 already exists for this appointment"]
    await videos.run_outbox(env.ctx)
    (written,) = await events(env, "fr_email_line")
    assert written.detail == {
        "appointment": 9001, "fieldroutes_said": ["Invoice 4411 already exists for this appointment"],
    }


async def test_a_read_that_comes_without_the_technicians_comments_is_not_taken_for_an_empty_comment(
    env: Env, clock: Clock
) -> None:
    """Nobody has seen a technician's typed words come back from the live API (all 50
    that were looked at were empty), and FieldRoutes' own spec was already wrong once
    about a field called `notes`. A read without the field must never be read as "he
    wrote nothing": the write would replace his words with our one line."""
    _, video_id = await released_in_time(env, clock)
    typed = "Sprayed the back fence line. Dog was out, did not treat the side yard."
    env.fake.data["appointment"][9001]["notes"] = typed

    def without_the_comments(request: httpx.Request, answer: Any) -> httpx.Response:
        response = answer()
        if what_of(request) != "appointment/get":
            return response
        body = response.json()
        body["appointments"] = [{k: v for k, v in r.items() if k != "notes"} for r in body["appointments"]]
        return httpx.Response(200, json=body)

    fieldroutes_does(env, without_the_comments)
    await videos.run_outbox(env.ctx)
    assert env.fake.completions == [] and env.fake.comments(9001) == typed, "his words are as he typed them"
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert "FieldRoutes did not send the technician's comments with the visit" in flag.detail
    assert "could wipe out his own words" in flag.detail


@pytest.mark.parametrize("status", [1.5, 1.9, True, "1.0", "1 ", " 1", 1], ids=repr)
async def test_only_the_status_one_is_completed(env: Env, clock: Clock, status: Any) -> None:
    """Every value arrives as a string and goes through the int helper. For this one
    answer that is not enough: 1.9 and `true` both come out of it as 1."""
    _, video_id = await released_in_time(env, clock)
    env.fake.data["appointment"][9001]["status"] = status
    await videos.run_outbox(env.ctx)
    if str(status).strip() == "1":
        assert len(env.fake.completions) == 1
    else:
        assert env.fake.completions == [] and (await env.video(video_id)).email_skip == FR_MISSED


ODD_WORDS = {
    "quotes and an ampersand": "Didn't treat Mr. O'Neil's \"back 40\" & side yard; 50% done <ok>",
    "emoji": "All done \U0001F44D\U0001F3FD see you in 2 weeks ❤️",
    "html": "<b>Bold</b> <script>alert(1)</script> <a href=\"https://example.com/x?a=1&b=2\">my link</a>",
    "windows line breaks": "Line one\r\nLine two\r\n\r\nLine four",
    "a link of his own": "See https://example.com/w/0123456789abcdef0123456789abcdef for the label",
    "plus and percent": "Used 2+2 oz at 100% +tax %41 %zz a=b&c=d",
    "very long": "word " * 1000,
}


@pytest.mark.parametrize("name", list(ODD_WORDS))
async def test_the_technicians_own_words_go_back_exactly_as_he_typed_them(env: Env, clock: Clock, name: str) -> None:
    typed = ODD_WORDS[name]
    _, video_id = await released_in_time(env, clock)
    env.fake.data["appointment"][9001]["notes"] = typed
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    (request,) = env.fr_calls("appointment", "complete")
    on_the_wire = urllib.parse.parse_qs(request.body, keep_blank_values=True)
    assert sorted(on_the_wire) == ["appointmentID", "authenticationKey", "authenticationToken", "completionNotes"]
    assert on_the_wire["completionNotes"] == [f"{typed.strip()}\n\n{line_for(video)}"]
    assert await env.flags("fr_changed") == [], "read back, and his words are still above the line"
    await the_last_look(env, clock)
    assert len(env.fr_calls("appointment", "complete")) == 1 and env.fake.comments(9001).count(line_for(video)) == 1


async def test_if_fieldroutes_stores_his_words_differently_from_how_they_were_sent_the_office_is_told(
    env: Env, clock: Clock
) -> None:
    """The server reads his words and sends them back with the line under them. If
    FieldRoutes does not store them as they were sent (an apostrophe turned into a code,
    a long comment cut short), nobody would know from its "success"."""
    _, video_id = await released_in_time(env, clock)
    env.fake.data["appointment"][9001]["notes"] = "Didn't spray the roses."
    env.fake.stores_comments_as = lambda text: text.replace("'", "&#39;")
    await videos.run_outbox(env.ctx)
    (flag,) = await env.flags("fr_changed")
    assert "The technician's own words read differently than before." in flag.detail
    assert 'Before: "Didn\'t spray the roses.". Now: "Didn&#39;t spray the roses.' in flag.detail
    assert "Put his words right in FieldRoutes if something was lost." in flag.detail
    assert "More than the comments read differently" not in flag.detail
    assert (await events(env, "fr_email_line"))[0].detail == {"appointment": 9001, "words_changed": True}


async def test_fieldroutes_says_saved_and_the_link_is_not_there_is_tried_once_more_and_then_left(
    env: Env, clock: Clock
) -> None:
    """An answer of "success" is not the line being in. The visit is read again, and when
    the link is not in the comments the server tries once more and then stops: the call
    runs FieldRoutes' completion logic every time, and is not made over and over for nothing."""
    _, video_id = await released_in_time(env, clock)
    env.fake.drops_comments = True
    for _ in range(6):
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    assert len(env.fake.completions) == 2 and env.fake.comments(9001) == ""
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert "FieldRoutes answered twice that it saved the technician's comments with the link" in flag.detail
    assert "It stopped trying." in flag.detail
    assert len(await events(env, "fr_email_not_taken")) == 2 and await events(env, "fr_email_line") == []
    await office_login(env)
    assert "In FieldRoutes' email" not in await day_page(env)


async def test_when_the_visit_cannot_be_read_right_after_the_write_the_page_still_claims_nothing(
    env: Env, clock: Clock
) -> None:
    _, video_id = await released_in_time(env, clock)
    reads = 0

    def no_answer_to_the_second_read(request: httpx.Request, answer: Any) -> httpx.Response:
        nonlocal reads
        if what_of(request) == "appointment/get":
            reads += 1
            if reads == 2:
                return httpx.Response(200, json={"success": False, "errorMessage": "FieldRoutes is down"})
        return answer()

    fieldroutes_does(env, no_answer_to_the_second_read)
    await videos.run_outbox(env.ctx)
    row = await line_of(env)
    assert len(env.fake.completions) == 1 and row.payload["written_at"] and row.done_at is None
    await office_login(env)
    assert "Link added for FieldRoutes' email" in await day_page(env)
    # A minute later it can be read: there. That was not the last look yet; that one is still made.
    clock.forward(minutes=1)
    await videos.run_outbox(env.ctx)
    row = await line_of(env)
    assert row.done_at is None and reads == 3
    row = await the_last_look(env, clock)
    assert row.done_at is not None and reads == 4 and len(env.fake.completions) == 1
    assert (await env.video(video_id)).email_skip == FR_EMAIL


async def test_the_deadline_is_looked_at_again_after_the_read_and_before_the_write(env: Env, clock: Clock) -> None:
    """FieldRoutes is slow to answer the read, and the line's time runs out meanwhile. A
    line written then would be in FieldRoutes and in no email."""
    _, video_id = await released_in_time(env, clock)
    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    clock.to(deadline - timedelta(seconds=30))

    def slow(request: httpx.Request, answer: Any) -> httpx.Response:
        if what_of(request) == "appointment/get" and not env.fr_calls("appointment", "get"):
            clock.forward(minutes=6)
        return answer()

    fieldroutes_does(env, slow)
    await videos.run_outbox(env.ctx)
    assert env.fake.completions == [], "read in time, answered too late: not written"
    await videos.run_outbox(env.ctx)  # the next pass finds it too late, looks, and tells the office
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and env.fake.completions == []
    assert env.fake.comments_in_the_email(9001, 30) == ""
    await office_login(env)
    page = await day_page(env)
    assert "In FieldRoutes' email" not in page and "Link added" not in page


async def test_a_video_pulled_while_fieldroutes_is_being_read_gets_no_line(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)

    async def pulled_meanwhile(request: httpx.Request) -> httpx.Response:
        if what_of(request) == "appointment/get":
            await videos.pull(env.ctx, video_id, OWNER, "wrong house")
        return real(request)

    real = env.fake.handler
    env.fake.handler = pulled_meanwhile
    env.ctx.fr = FieldRoutes(_client_for(env.fake), env.ctx.cfg)
    await videos.run_outbox(env.ctx)
    assert (await env.video(video_id)).state == "pulled"
    assert env.fake.completions == [] and env.fake.comments(9001) == "", "no dead link went into the customer's email"
    row = await line_of(env)
    assert row.done_at is not None and row.last_error.startswith("skipped:") and await env.flags(FR_MISSED) == []


async def test_a_customer_who_presses_stop_after_the_video_was_let_go_still_gets_no_line(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    await videos.opt_out(env.ctx, await env.video(video_id))
    for _ in range(3):
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", "opted_out")
    assert env.fake.completions == [] and env.fr_calls("appointment", "get") == [] and our_emails(env) == []
    assert await env.flags(FR_MISSED) == [], "settled as it would have been a minute earlier: nothing for the office"
    assert [e.detail for e in await events(env, "email_skipped")][-1] == {"reason": "opted_out", "was": FR_EMAIL}
    assert len(env.fr_calls("note", "create")) == 1, "the internal note is filed, as for any such customer"


# == the promise: one email, with the link in it, or the office is told ====================


async def test_a_line_saved_over_before_fieldroutes_sends_is_put_back_at_the_last_look(env: Env, clock: Clock) -> None:
    """The line is in, and FieldRoutes' email is still a quarter of an hour away. In that
    time the tech opens the finished visit in FieldRoutes' app and fixes his note (or a
    phone that was offline hands in its own copy late). That save has no line of ours.
    Two minutes before the last moment a line can be added, the server reads the visit
    once more and puts the line back under his new words."""
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert link_of(video) in env.fake.comments(9001)
    clock.forward(minutes=3)
    env.fake.saved_again(9001, "Sprayed the roses. Gate is latched.")

    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    while clock.now() < deadline + timedelta(minutes=8):  # the outbox, once a minute, until after FieldRoutes sent
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)

    assert env.fake.comments_in_the_email(9001, 30) == f"Sprayed the roses. Gate is latched.\n\n{line_for(video)}"
    assert len(env.fake.completions) == 2 and our_emails(env) == [] and await env.flags() == []
    row = await line_of(env)
    assert row.done_at is not None and row.payload["added_again_at"] and row.payload["looked_at"]
    assert [e.detail for e in await events(env, "fr_email_line_again")] == [{"appointment": 9001}]
    assert len(await events(env, "fr_email_line")) == 1
    await office_login(env)
    assert "In FieldRoutes' email" in await day_page(env)
    (stat,) = [r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera"]
    assert stat["sent"] == 1


async def test_a_line_that_is_gone_when_it_is_too_late_to_put_it_back_is_told_to_the_office(env: Env, clock: Clock) -> None:
    """The same, but the server could not look in time (it was down over those minutes).
    The customer's one email has no link. That must never pass quietly: the video becomes
    a hold, the page stops saying it went, and the stats do not count it."""
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    clock.forward(minutes=3)
    env.fake.saved_again(9001, "Sprayed the roses. Gate is latched.")
    clock.forward(minutes=20)  # the server is back after FieldRoutes' email went
    await videos.run_outbox(env.ctx)

    assert env.fake.comments_in_the_email(9001, 30) == "Sprayed the roses. Gate is latched."
    assert len(env.fake.completions) == 1, "nothing is written after the deadline"
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert "its link was added to the technician's comments on the visit at " in flag.detail
    assert "the link was gone: the comments were saved again in FieldRoutes without it" in flag.detail
    assert "FieldRoutes' email most likely goes without the link" in flag.detail
    assert flag.detail.endswith("Press Send it anyway to send our own email with the link, or Pull.")
    await office_login(env)
    page = await day_page(env)
    assert "In FieldRoutes' email" not in page and "Link added" not in page
    (stat,) = [r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera"]
    assert (stat["videos"], stat["sent"]) == (1, 0)


async def test_a_line_that_is_gone_from_a_visit_that_was_reopened_is_not_put_back(env: Env, clock: Clock) -> None:
    """Putting the line back is the same write, with the same guards. The office has set
    the visit back to pending since: the call would complete it again, so it is not made."""
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    env.fake.data["appointment"][9001].update(status="0", dateCompleted=NEVER, notes="")
    row = await the_last_look(env, clock)
    assert len(env.fake.completions) == 1 and env.fake.completed_by_api == [] and env.fake.invoices == []
    assert row.failed_at is not None and (await env.video(video_id)).email_skip == FR_MISSED
    (flag,) = await env.flags(FR_MISSED)
    assert "its link was added to the technician's comments on the visit at " in flag.detail
    assert "It could not be added again (FieldRoutes no longer shows that appointment as completed)" in flag.detail


async def test_switched_off_means_a_line_that_went_missing_is_not_put_back_either(env: Env, clock: Clock) -> None:
    """The owner sets the choice back to "Our own email". From that moment nothing more is
    written to any visit, also not to put back a line that was saved over."""
    _, video_id = await released_in_time(env, clock)
    await videos.run_outbox(env.ctx)
    env.fake.saved_again(9001, "Sprayed the roses.")
    await one_email(env, 0)
    row = await the_last_look(env, clock)
    assert len(env.fake.completions) == 1 and env.fake.comments(9001) == "Sprayed the roses."
    assert row.failed_at is not None and (await env.video(video_id)).email_skip == FR_MISSED
    (flag,) = await env.flags(FR_MISSED)
    assert "It could not be added again (the setting that puts the link into FieldRoutes' email was switched off since)" in flag.detail


async def test_fieldroutes_down_for_six_minutes_does_not_cost_the_video_its_place(env: Env, clock: Clock) -> None:
    """The line has 14 minutes. FieldRoutes does not answer for the first six. With the
    notes' waits (1, 5, 15 minutes) that was two tries and then "too late", with eight
    minutes left. A line is tried again within a minute or two."""
    _, video_id = await released_in_time(env, clock)
    start = clock.now()
    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    env.fake.down = True
    tried_at: list[int] = []
    for minute in range(15):
        if minute == 6:
            env.fake.down = False
        before = (await line_of(env)).attempts
        await videos.run_outbox(env.ctx)
        row = await line_of(env)
        if row.attempts != before or row.payload.get("written_at"):
            tried_at.append(minute)
        if row.payload.get("written_at"):
            break
        assert row.next_try_at < deadline, "a try is never put on the deadline itself while one still fits before it"
        clock.forward(minutes=1)
    assert tried_at == [0, 1, 3, 5, 8], "after a minute, then two, two, three"
    video = await env.video(video_id)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    assert clock.now() - start < timedelta(minutes=9) and line_for(video) in env.fake.comments_in_the_email(9001, 30)
    assert video.email_skip == FR_EMAIL and await env.flags(FR_MISSED) == []


async def test_the_last_try_is_a_minute_before_the_deadline_and_never_on_it(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    env.fake.down = True
    left: list[timedelta] = []
    while True:
        await videos.run_outbox(env.ctx)
        row = await line_of(env)
        left.append(deadline - row.next_try_at)
        if left[-1] <= timedelta(minutes=1):
            break
        clock.to(row.next_try_at + timedelta(seconds=1))
    assert left[-1] == timedelta(minutes=1), "the last try is planned for a minute before the deadline, not on it"
    assert len(left) >= 4 and all(more > timedelta(minutes=1) for more in left[:-1])

    # FieldRoutes is back for that last try, and the line still makes the email.
    env.fake.down = False
    clock.to(row.next_try_at + timedelta(seconds=1))
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert len(env.fake.completions) == 1 and video.email_skip == FR_EMAIL
    assert line_for(video) in env.fake.comments_in_the_email(9001, 30)


async def test_a_write_that_landed_and_whose_answer_was_lost_is_found_and_not_called_missed(env: Env, clock: Clock) -> None:
    """FieldRoutes takes the line and its answer is lost on the way back, three minutes
    before the deadline. The server hears an error. It must not tell the office the link
    is not in FieldRoutes' email: it is."""
    _, video_id = await released_in_time(env, clock)
    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    clock.to(deadline - timedelta(minutes=3))
    lost = True

    def the_answer_is_lost(request: httpx.Request, answer: Any) -> httpx.Response:
        nonlocal lost
        response = answer()  # FieldRoutes does the work
        if what_of(request) == "appointment/complete" and lost:
            lost = False
            return httpx.Response(504, text="Gateway Timeout")
        return response

    fieldroutes_does(env, the_answer_is_lost)
    await videos.run_outbox(env.ctx)
    assert len(env.fake.completions) == 1 and (await line_of(env)).attempts == 1
    for _ in range(8):
        clock.forward(minutes=1)
        await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert link_of(video) in env.fake.comments_in_the_email(9001, 30)
    assert (video.state, video.email_skip) == ("no_email", FR_EMAIL) and await env.flags() == []
    assert len(env.fake.completions) == 1, "the next try read first, found the line, and wrote nothing"
    assert [e.detail for e in await events(env, "fr_email_line")] == [{"appointment": 9001, "already_there": True}]
    assert (await line_of(env)).done_at is not None


async def test_too_late_for_one_appointment_the_line_goes_into_the_email_of_the_techs_next_one(env: Env, clock: Clock) -> None:
    """Three appointments in one visit, completed minutes apart, and FieldRoutes sends an
    email for each. The first one's email is seconds away when the video is let go, and
    by the time the outbox gets to it that one is too late. The other two emails are a
    quarter of an hour off. The video is not given up: it goes on the next one, after the
    server has read the first and seen that the link is not there."""
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    completes(env, clock, 9001, ago=24.5, comments="Mowed.")
    completes(env, clock, 9002, 9003, ago=10, comments="Picked up the leaves.")
    await emailer(env)
    assert (await line_of(env)).payload["appointment_id"] == 9001
    clock.forward(minutes=1)
    await videos.run_outbox(env.ctx)

    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_EMAIL) and await env.flags() == []
    assert env.fake.completions == [
        {"appointmentID": "9002", "completionNotes": f"Picked up the leaves.\n\n{line_for(video)}"}
    ]
    assert env.fake.comments(9001) == "Mowed." and env.fake.comments(9003) == "Picked up the leaves."
    row = await line_of(env)
    assert row.payload["appointment_id"] == 9002 and row.payload["passed"] == [9001]
    assert line_for(video) in env.fake.comments_in_the_email(9002, 30) and our_emails(env) == []
    # Never the same line on two appointments, whatever comes after.
    for _ in range(20):
        clock.forward(minutes=1)
        await videos.run_outbox(env.ctx)
    assert len(env.fake.completions) == 1 and (await line_of(env)).done_at is not None


async def test_too_late_for_every_one_of_them_the_flag_says_what_is_known(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    clock.to(deadline + timedelta(seconds=5))  # the outbox did not run in all that time
    await videos.run_outbox(env.ctx)
    (flag,) = await env.flags(FR_MISSED)
    assert flag.detail.startswith(
        "Pat's video for Jane Smith, 123 Main St was too late for FieldRoutes' email. The link was not added before "
        "that email was due (by "
    )
    assert "FieldRoutes did not answer" not in flag.detail and "server was down" not in flag.detail
    assert "This server looked at the visit in FieldRoutes at " in flag.detail
    assert env.fake.completions == [] and asked_of_the_visit(env) == ["appointment/get"]


async def no_answer_until_after_fieldroutes_sent(env: Env, clock: Clock) -> str:
    """FieldRoutes is down from before the first try until after its email was due."""
    _, video_id = await released_in_time(env, clock)
    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    env.fake.down = True
    while clock.now() < deadline + timedelta(minutes=7):
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    return video_id


async def test_when_fieldroutes_cannot_be_read_the_flag_says_the_server_does_not_know(env: Env, clock: Clock) -> None:
    """The server never wrote, as far as it knows. But "as far as it knows" is not much
    after a try that was cut off, or a backup: so the flag says it does not know, and to
    look. It does not say the link is not there."""
    video_id = await no_answer_until_after_fieldroutes_sent(env, clock)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and our_emails(env) == []
    (flag,) = await env.flags(FR_MISSED)
    assert flag.detail.startswith(
        "Pat's video for Jane Smith, 123 Main St: this server cannot tell whether its link is in FieldRoutes' email."
    )
    assert "FieldRoutes did not answer when this server tried to look" in flag.detail
    assert "look at the technician's comments on the visit in FieldRoutes before you send anything" in flag.detail
    assert "is not in" not in flag.detail and "was too late" not in flag.detail
    row = await line_of(env)
    assert row.attempts <= 14, "a handful of tries, not one a minute for ever"
    assert row.failed_at is None and row.payload["unsure"], "and it will look again when FieldRoutes is back"


async def test_once_fieldroutes_answers_again_a_cannot_tell_flag_is_made_to_say_what_is_so(env: Env, clock: Clock) -> None:
    video_id = await no_answer_until_after_fieldroutes_sent(env, clock)
    env.fake.down = False
    clock.forward(minutes=5)
    await videos.run_outbox(env.ctx)
    (flag,) = await env.flags(FR_MISSED)
    assert flag.detail.startswith("Pat's video for Jane Smith, 123 Main St was too late for FieldRoutes' email.")
    assert "This server looked at the visit in FieldRoutes at " in flag.detail and "cannot tell" not in flag.detail
    assert (await env.video(video_id)).email_skip == FR_MISSED and env.fake.completions == [], "it only read"
    row = await line_of(env)
    assert row.failed_at is not None and "unsure" not in row.payload
    for _ in range(3):
        clock.forward(minutes=6)
        await a_minute(env)
    assert env.fake.completions == [] and our_emails(env) == [] and len(await env.flags(FR_MISSED)) == 1


async def test_a_cannot_tell_video_whose_link_is_there_after_all_goes_back_to_in_and_its_flag_is_closed(
    env: Env, clock: Clock
) -> None:
    """A try got through just as FieldRoutes went down, and its answer was lost. When
    FieldRoutes is back the server finds its link in the visit: the customer has it. The
    office is no longer asked to decide, so nobody sends a second email for it."""
    _, video_id = await released_in_time(env, clock)
    video = await env.video(video_id)
    deadline = datetime.fromisoformat((await line_of(env)).payload["deadline"])
    lost = True

    def the_answer_is_lost_and_then_nothing(request: httpx.Request, answer: Any) -> httpx.Response:
        nonlocal lost
        if what_of(request) == "appointment/complete" and lost:
            lost = False
            answer()  # FieldRoutes does the work
            env.fake.down = True
            return httpx.Response(504, text="Gateway Timeout")
        return answer()

    fieldroutes_does(env, the_answer_is_lost_and_then_nothing)
    while clock.now() < deadline + timedelta(minutes=7):
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    assert (await env.video(video_id)).email_skip == FR_MISSED
    (flag,) = await env.flags(FR_MISSED)
    assert "cannot tell" in flag.detail and line_for(video) in env.fake.comments_in_the_email(9001, 30)

    env.fake.down = False
    clock.forward(minutes=5)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_EMAIL)
    assert await env.flags(FR_MISSED) == [] and our_emails(env) == [] and len(env.fake.completions) == 1
    (closed,) = await env.flags(FR_MISSED, open_only=False)
    assert closed.cleared_by == "system" and closed.note == "the link is in the technician's comments after all"
    await office_login(env)
    page = await day_page(env)
    assert "In FieldRoutes' email" in page and f'action="/office/video/{video_id}/send"' not in page
    headers = await login(env)
    stop = next(s for s in (await env.http.get("/v1/today", headers=headers)).json()["stops"] if s["customerId"] == JANE)
    assert stop["video"] == {"id": video_id, "state": "no_email", "byYou": True}, "no longer with the office"


# == a backup put back =====================================================================


async def test_after_a_backup_from_before_the_write_is_put_back_the_link_is_found_and_no_flag_invites_a_second_email(
    env: Env, clock: Clock
) -> None:
    """The backup falls between "going into FieldRoutes' email" and the write. The line
    is written, the email goes, and two days later the backup is put back. The server no
    longer knows it added the link. It looks before it says anything."""
    _, video_id = await released_in_time(env, clock)
    backup = await take_backup(env)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert line_for(video) in env.fake.comments_in_the_email(9001, 30)

    clock.forward(days=2)
    await restore_backup(env, backup)
    for _ in range(3):
        await a_minute(env)
        await five_minute_round(env)
        clock.forward(minutes=6)

    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_EMAIL)
    assert await env.flags(FR_MISSED) == [] and our_emails(env) == []
    assert len(env.fake.completions) == 1, "and nothing is written to FieldRoutes a second time"
    row = await line_of(env)
    assert row.done_at is not None and row.payload["found_at"]
    csrf = await office_login(env)
    page = await day_page(env)
    assert "In FieldRoutes' email" in page and f'action="/office/video/{video_id}/send"' not in page
    r = await env.http.post(f"/office/video/{video_id}/send", data={"csrf": csrf})
    assert "That video is not waiting to be sent." in html.unescape(r.headers["location"]).replace("%20", " ")
    assert our_emails(env) == []


async def test_after_a_backup_from_inside_the_techs_ten_minutes_is_put_back_the_link_is_found_too(
    env: Env, clock: Clock
) -> None:
    """Here the backup has the video still waiting. After the restore every FieldRoutes
    email for the visit has long gone, so by the clock the video "missed". The server
    reads the visit before it says so, and finds its own link from before the restore."""
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES, comments="Done this morning.")
    backup = await take_backup(env)
    clock.forward(minutes=11)
    await a_minute(env)
    video = await env.video(video_id)
    link = link_of(video)
    assert video.email_skip == FR_EMAIL and link in env.fake.comments_in_the_email(9001, 30)

    clock.forward(minutes=45)
    await restore_backup(env, backup)
    await a_minute(env)
    await five_minute_round(env)

    video = await env.video(video_id)
    assert link_of(video) == link and (video.state, video.email_skip) == ("no_email", FR_EMAIL)
    assert await env.flags(FR_MISSED) == [] and our_emails(env) == [] and len(env.fake.completions) == 1
    (settled,) = await events(env, "email_skipped")
    assert settled.detail == {"reason": FR_EMAIL, "appointment": 9001, "already_there": True}
    await office_login(env)
    page = await day_page(env)
    assert "In FieldRoutes' email" in page and f'action="/office/video/{video_id}/send"' not in page


# == a deploy rolled back =================================================================


async def test_a_row_closed_by_a_server_from_before_this_was_built_is_not_taken_for_a_line_that_went_in(
    env: Env, clock: Clock
) -> None:
    """The server from before this build marks an outbox row of a kind it does not know as
    done, and writes nothing. After a deploy was rolled back and forward again, such a row
    must not read "In FieldRoutes' email": it is put back in line."""
    _, video_id = await released_in_time(env, clock)
    row = await line_of(env)
    # What the old server's outbox does with it:
    await env.run(sa.update(db.fr_outbox).where(db.fr_outbox.c.id == row.id).values(done_at=db.now(), last_error=""))
    await office_login(env)
    page = await day_page(env)
    assert "In FieldRoutes' email" not in page and "Going into FieldRoutes' email" in page
    (stat,) = [r for r in await stats(env.ctx, 7) if r["name"] == "Pat Rivera"]
    assert stat["sent"] == 0

    await videos.run_outbox(env.ctx)  # rolled forward again, inside the line's time
    video = await env.video(video_id)
    assert env.fake.completions == [{"appointmentID": "9001", "completionNotes": line_for(video)}]
    assert "Link added for FieldRoutes' email" in await day_page(env)


async def test_such_a_row_found_after_its_time_is_read_and_told_to_the_office(env: Env, clock: Clock) -> None:
    _, video_id = await released_in_time(env, clock)
    row = await line_of(env)
    await env.run(sa.update(db.fr_outbox).where(db.fr_outbox.c.id == row.id).values(done_at=db.now(), last_error=""))
    clock.forward(minutes=40)
    await videos.run_outbox(env.ctx)
    await videos.run_outbox(env.ctx)
    video = await env.video(video_id)
    assert (video.state, video.email_skip) == ("no_email", FR_MISSED) and env.fake.completions == []
    (flag,) = await env.flags(FR_MISSED)
    assert "was too late for FieldRoutes' email" in flag.detail and "This server looked at the visit" in flag.detail


# == one line per video ====================================================================


async def test_pulled_and_put_back_on_the_same_customer_a_video_does_not_get_a_second_line(env: Env, clock: Clock) -> None:
    """The office pulls a video whose line is in, then puts it back on the same customer
    (Move is the only way to un-pull). The video has a new link. A second line would show
    the customer two links, the first one dead."""
    headers = await ready(env)
    await one_email(env, 60)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES, comments="All good.")
    clock.forward(minutes=11)
    await a_minute(env)
    first = link_of(await env.video(video_id))
    await videos.pull(env.ctx, video_id, OWNER, "pressed the wrong button")
    await videos.move(env.ctx, video_id, JANE, OWNER)
    for _ in range(3):
        clock.forward(minutes=1)
        await a_minute(env)

    video = await env.video(video_id)
    assert link_of(video) != first and (video.state, video.email_skip) == ("no_email", FR_MISSED)
    assert len(env.fake.completions) == 1 and env.fake.comments(9001) == f"All good.\n\nWatch the video of today's visit: {first}"
    assert [env.fake.comments(a) for a in (9002, 9003)] == ["All good.", "All good."], "nor a line on another appointment"
    (flag,) = await env.flags(FR_MISSED)
    assert "its link was already added to FieldRoutes' email for this customer once" in flag.detail
    assert "That first link is dead now. A second line would show the customer two links, so none was added." in flag.detail
    assert "before you send anything" not in flag.detail and our_emails(env) == []


async def test_a_helpers_line_and_the_leads_line_on_one_appointment_both_stay(env: Env, clock: Clock) -> None:
    """Two videos, two lines, one appointment: the second is written under the first."""
    env.fake.data["appointment"][9001]["additionalTechs"] = str(SAM)
    pat, sam = await ready(env), await ready(env, "sam@example.com")
    await one_email(env, 30)
    pats = await film(env, pat, JANE, recordedAt=just_now(clock))
    sams = await film(env, sam, JANE, recordedAt=just_now(clock))
    completes(env, clock, *JANES, comments="Trimmed the hedge.")
    clock.forward(minutes=11)
    await a_minute(env)
    lines = {line_for(await env.video(pats)), line_for(await env.video(sams))}
    assert len(lines) == 2 and len(env.fake.completions) == 2
    kept = env.fake.comments(9001).split("\n\n")
    assert kept[0] == "Trimmed the hedge." and set(kept[1:]) == lines
    for row in await outbox(env, "comment"):
        assert row.payload["written_at"]
    assert await env.flags() == []


# == what a wait costs =====================================================================


def the_throttle_follows(clock: Clock, monkeypatch: Any) -> None:
    """The server's own limit on how often a day is read runs on the machine's clock. Let
    it follow the test's clock, so that hours pass for it too."""
    start = time.monotonic()
    monkeypatch.setattr(route, "time", SimpleNamespace(monotonic=lambda: start + clock.ahead.total_seconds()))


def reads_since(env: Env, since: int) -> dict[str, int]:
    return dict(Counter(f"{r.entity}/{r.action}" for r in env.fake.requests[since:]))


@pytest.mark.parametrize("minutes, lists_an_hour", [(30, 4), (60, 2)])
async def test_a_video_waiting_all_night_for_its_visit_to_be_completed_costs_a_few_reads_an_hour(
    env: Env, clock: Clock, monkeypatch: Any, minutes: int, lists_an_hour: int
) -> None:
    """After work hours nothing else reads the day. The video looks at its visit only as
    often as it must to see a completion in time: every 15 minutes with FieldRoutes'
    email at 30, every 30 with it at 60. Before, it was every five minutes."""
    the_throttle_follows(clock, monkeypatch)
    env.reconfigure(work_start_hour=0, work_end_hour=0)  # it is night: the day's own refresh is not running
    not_completed(env, *JANES)
    headers = await ready(env)
    await one_email(env, minutes)
    first = await film(env, headers, JANE, recordedAt=just_now(clock))
    second = await film(env, headers, BOB, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    since = len(env.fake.requests)
    for _ in range(4 * 60):  # four hours of the once-a-minute jobs
        await videos.send_due(env.ctx)
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    assert (await env.video(first)).state == "held" and (await env.video(second)).state == "held"
    cost = reads_since(env, since)
    # Each look is the day's appointments, and the day's routes because one appointment
    # in the seed has no tech of its own (as real days have).
    assert set(cost) == {"appointment/search", "route/search"}, "no customer is read, however many videos wait"
    assert cost["appointment/search"] in (4 * lists_an_hour, 4 * lists_an_hour + 1)
    assert cost["route/search"] == cost["appointment/search"]


async def test_a_visit_completed_at_night_is_still_seen_in_time_for_fieldroutes_email(
    env: Env, clock: Clock, monkeypatch: Any
) -> None:
    the_throttle_follows(clock, monkeypatch)
    env.reconfigure(work_start_hour=0, work_end_hour=0)
    not_completed(env, *JANES)
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    for minute in range(120):
        if minute == 47:
            completes(env, clock, *JANES, comments="Closed out from home.")
        await videos.send_due(env.ctx)
        await videos.run_outbox(env.ctx)
        clock.forward(minutes=1)
    video = await env.video(video_id)
    assert video.email_skip == FR_EMAIL and await env.flags() == []
    assert env.fake.comments_in_the_email(9001, 30) == f"Closed out from home.\n\n{line_for(video)}"


async def test_in_work_hours_a_waiting_video_reads_nothing_of_its_own(env: Env, clock: Clock, monkeypatch: Any) -> None:
    """During the day the server reads today's list every five minutes anyway. A waiting
    video works from that list: no read of the day, of the customer or of the bucket."""
    the_throttle_follows(clock, monkeypatch)
    not_completed(env, *JANES)
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    bucket_asked = 0
    head = env.storage.head

    async def counted(key: str) -> Any:
        nonlocal bucket_asked
        bucket_asked += 1
        return await head(key)

    env.storage.head = counted  # type: ignore[method-assign]
    for _ in range(60):
        await jobs.refresh_day(env.ctx)  # the job loop runs it every five minutes; the throttle keeps it to that
        since = len(env.fake.requests)
        await videos.send_due(env.ctx)
        assert env.fake.requests[since:] == []
        clock.forward(minutes=1)
    assert (await env.video(video_id)).state == "held" and bucket_asked == 0


# == the owner's test in the go-live guide ================================================


async def test_the_second_run_of_the_owners_test_works_once_the_first_video_is_pulled(env: Env, clock: Clock) -> None:
    """The guide's test films the test customer twice in one day. One video per tech, per
    customer, per day: the second is refused until the first is pulled, so the guide says
    to pull it between the two runs."""
    del env.fake.data["appointment"][9003]
    not_completed(env, 9001, 9002)
    headers = await ready(env)
    await one_email(env, 30)
    first = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, 9001, comments="Test visit. Didn't find any leaks & the timer is set.\nSecond line.")
    clock.forward(minutes=13)
    await a_minute(env)
    one = await env.video(first)
    assert one.email_skip == FR_EMAIL
    assert env.fake.comments(9001) == (
        f"Test visit. Didn't find any leaks & the timer is set.\nSecond line.\n\n{line_for(one)}"
    )
    clock.forward(minutes=50)
    await a_minute(env)

    r = await env.http.post("/v1/videos", json=video_body(JANE, recordedAt=just_now(clock)), headers=headers)
    assert r.status_code == 409 and r.json()["error"] == "visit_has_video"

    csrf = await office_login(env)
    await env.http.post(f"/office/video/{first}/pull", data={"csrf": csrf, "reason": "first run of the test is done"})
    second = await film(env, headers, JANE, recordedAt=just_now(clock))
    completes(env, clock, 9002, comments="Second test visit.")
    clock.forward(minutes=13)
    await a_minute(env)
    two = await env.video(second)
    assert (two.state, two.email_skip) == ("no_email", FR_EMAIL)
    assert env.fake.comments(9002) == f"Second test visit.\n\n{line_for(two)}"
    assert line_for(two) not in env.fake.comments(9001) and env.fake.completed_by_api == []


# == words on the office page ==============================================================


async def test_with_one_email_on_every_held_row_says_that_send_it_anyway_is_a_second_email(env: Env, clock: Clock) -> None:
    """A video that waited too long for its visit to be completed is held as overdue.
    FieldRoutes still sends its own email when the visit is completed. So our own email,
    if the office sends it, comes beside that one, and the row says so."""
    not_completed(env, *JANES)
    headers = await ready(env)
    await one_email(env, 30)
    video_id = await film(env, headers, JANE, recordedAt=just_now(clock))
    clock.forward(hours=7)
    await a_minute(env)
    assert (await env.video(video_id)).email_skip == "overdue"
    await office_login(env)
    page = await day_page(env)
    assert "Send it anyway sends our own email with the link, beside the email FieldRoutes sends for the visit." in page

    await one_email(env, 0)
    assert "beside the email FieldRoutes sends" not in await day_page(env), "with the setting off the page is as it was"


async def test_a_flag_about_another_techs_video_knows_it_went_out_in_fieldroutes_email(env: Env, clock: Clock) -> None:
    """Pat's video went into FieldRoutes' email. Then the office gives the visit to Sam,
    who films it too. Sam's waits as a second video, and its flag must not call Pat's
    "on file" as if no customer had it."""
    pat, sam = await ready(env), await ready(env, "sam@example.com")
    await one_email(env, 30)
    pats = await film(env, pat, BOB, recordedAt=just_now(clock))
    completes(env, clock, BOBS)
    clock.forward(minutes=11)
    await a_minute(env)
    assert (await env.video(pats)).email_skip == FR_EMAIL
    env.fake.data["appointment"][BOBS]["assignedTech"] = str(SAM)
    await five_minute_round(env)
    assert (await env.http.get("/v1/today", headers=sam)).status_code == 200
    sams = await film(env, sam, BOB, recordedAt=just_now(clock))
    clock.forward(minutes=11)
    await a_minute(env)
    await five_minute_round(env)
    assert (await env.video(sams)).email_skip == "second_video"
    (flag,) = [f for f in await env.flags("second_video") if f.video_id == sams]
    assert "Pat's video went into FieldRoutes' email for the customer" in flag.detail and "on file" not in flag.detail
