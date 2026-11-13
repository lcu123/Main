"""One video per tech, per property, per day (the owner's decision of Nov 13, 2026).

When two techs each have appointments at the same property on the same day, each
sends his own video and the customer gets both. A tech who records again replaces
only his own. One tech with two or three appointments there still sends one video.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa

from video_helpers import Env, film, login, make_env, minutes_ago, office_login, today, video_body

from zest_video import db, jobs, videos

PAT, SAM = 20, 21
BOB = 502  # Pat mows there (appointment 9004); in these tests Sam treats the lawn too (9010)


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    # Sam's own appointment at Bob's, finished a while ago. Pat's (9004) is still open.
    e.fake.data["appointment"][9010] = {
        "appointmentID": "9010", "officeID": "7", "customerID": str(BOB), "routeID": "71", "type": "80",
        "status": "1", "assignedTech": str(SAM), "servicedBy": str(SAM), "employeeID": "22", "date": today(),
        "start": "07:00:00", "sequence": "1", "checkIn": minutes_ago(90), "checkOut": minutes_ago(70),
    }
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


async def both(env: Env) -> tuple[dict[str, str], dict[str, str]]:
    pat = await login(env)
    sam = await login(env, "sam@example.com")
    await env.http.get("/v1/today", headers=pat)
    env.mailer.sent.clear()
    return pat, sam


async def send_all(env: Env, *video_ids: str) -> None:
    for video_id in video_ids:
        await env.window_closed(video_id)
    while await videos.send_due(env.ctx):
        pass


async def stop_for(env: Env, headers: dict[str, str], customer_id: int = BOB) -> dict:
    body = (await env.http.get("/v1/today", headers=headers)).json()
    return next(s for s in body["stops"] if s["customerId"] == customer_id)


# -- both videos stand, and both go out -----------------------------------------------


async def test_two_techs_at_one_property_each_send_a_video_and_the_customer_gets_both(env: Env) -> None:
    pat, sam = await both(env)
    pats = await film(env, pat, BOB)
    sams = await film(env, sam, BOB, recordedAt=minutes_ago_iso(80))
    assert {(await env.video(v)).state for v in (pats, sams)} == {"held"}, "neither replaced the other"

    await send_all(env, pats, sams)
    assert {(await env.video(v)).state for v in (pats, sams)} == {"sent"}
    emails = env.mailer.sent
    assert [m.to for m in emails] == ["bob@example.com", "bob@example.com"], "one email per video"
    by_tech = {("Pat" if "Pat" in m.subject else "Sam"): m for m in emails}
    assert set(by_tech) == {"Pat", "Sam"}, "each email names its tech"
    # ...and its own service, never the other tech's.
    assert "Bi-Weekly Yard Maintenance" in by_tech["Pat"].html and "Lawn Care Essential" not in by_tech["Pat"].html
    assert "Lawn Care Essential" in by_tech["Sam"].html and "Bi-Weekly Yard Maintenance" not in by_tech["Sam"].html
    assert "Service: Lawn Care Essential" in by_tech["Sam"].text and "Technician: Sam" in by_tech["Sam"].text
    links = {(await env.video(v)).token for v in (pats, sams)}
    assert len(links) == 2 and all(any(t in m.html for m in emails) for t in links), "two different watch links"


async def test_each_video_is_tied_to_that_techs_own_appointments(env: Env) -> None:
    pat, sam = await both(env)
    pats, sams = await film(env, pat, BOB), await film(env, sam, BOB)
    assert (await env.video(pats)).appointment_ids == [9004]
    assert (await env.video(sams)).appointment_ids == [9010]

    # A score left on Sam's video goes to FieldRoutes against Sam's appointment.
    await send_all(env, pats, sams)
    token = (await env.video(sams)).token
    await env.http.post(f"/w/{token}/score", data={"stars": "5", "comment": "Great"})
    (review,) = [r for r in await env.all(sa.select(db.fr_outbox)) if r.kind == "review"]
    assert review.payload["appointment_id"] == 9010


async def test_one_tech_with_three_appointments_still_sends_one_video(env: Env) -> None:
    pat, _ = await both(env)
    video = await env.video(await film(env, pat, 501))
    assert video.appointment_ids == [9001, 9002, 9003]
    assert len(await env.all(sa.select(db.videos))) == 1


# -- recording again only ever touches your own ---------------------------------------


async def test_recording_again_replaces_only_that_techs_own_video(env: Env) -> None:
    pat, sam = await both(env)
    sams = await film(env, sam, BOB)
    first = await film(env, pat, BOB)
    second = await film(env, pat, BOB)
    assert (await env.video(first)).state == "replaced"
    assert (await env.video(second)).state == "held"
    assert (await env.video(sams)).state == "held", "Sam's video is his own and is never replaced by Pat's"
    assert (await env.video(sams)).object_key in env.storage.objects, "and its file is still in the bucket"


async def test_a_video_that_already_went_out_locks_only_its_own_tech(env: Env) -> None:
    pat, sam = await both(env)
    pats = await film(env, pat, BOB)
    await send_all(env, pats)

    again = await env.http.post("/v1/videos", json=video_body(BOB), headers=pat)
    assert again.status_code == 409 and again.json()["error"] == "visit_has_video"
    assert again.json()["existing"]["byYou"] is True

    # Sam is not locked out by Pat's sent video: his is a separate video.
    sams = await film(env, sam, BOB)
    assert (await env.video(sams)).state == "held"
    await send_all(env, sams)
    assert [m.to for m in env.mailer.sent] == ["bob@example.com"] * 2
    assert (await env.video(pats)).state == "sent", "and Pat's stays out"


async def test_a_tech_can_delete_only_his_own_video_at_a_shared_property(env: Env) -> None:
    pat, sam = await both(env)
    pats, sams = await film(env, pat, BOB), await film(env, sam, BOB)
    assert (await env.http.delete(f"/v1/videos/{sams}", headers=pat)).status_code in (400, 403, 404)
    assert (await env.video(sams)).state == "held"
    assert (await env.http.delete(f"/v1/videos/{pats}", headers=pat)).status_code == 200
    assert (await env.video(pats)).state == "deleted" and (await env.video(sams)).state == "held"


async def test_two_phones_of_one_tech_filming_the_same_stop_still_make_one_email(env: Env) -> None:
    """The same-instant double from one tech is still settled before any email; the other
    tech's video is no rival and goes out too."""
    pat, sam = await both(env)
    sams = await film(env, sam, BOB)
    older = await film(env, pat, BOB, recordedAt=minutes_ago_iso(30))
    newer = await film(env, pat, BOB, recordedAt=minutes_ago_iso(10))
    # Put the older one back, as if both registrations had slipped in together.
    await env.run(sa.update(db.videos).where(db.videos.c.id == older).values(
        state="held", ended_at=None, ended_by="", ended_reason="", email_due_at=db.now()))
    await send_all(env, older, newer, sams)
    states = {v: (await env.video(v)).state for v in (older, newer, sams)}
    assert states == {older: "replaced", newer: "sent", sams: "sent"}
    assert len(env.mailer.sent) == 2


# -- what each phone is shown ---------------------------------------------------------


async def test_each_tech_sees_his_own_share_of_the_stop_and_only_his_own_video(env: Env) -> None:
    pat, sam = await both(env)
    mine, his = await stop_for(env, pat), await stop_for(env, sam)
    assert mine["appointmentIds"] == [9004] and his["appointmentIds"] == [9010]
    assert mine["services"] == ["Bi-Weekly Yard Maintenance"] and his["services"] == ["Lawn Care Essential"]
    assert (mine["done"], his["done"]) == (False, True), "Sam finished his part; Pat has not"
    assert mine["checkOut"] is None and his["checkOut"] is not None
    assert mine["video"] is None and his["video"] is None

    sams = await film(env, sam, BOB)
    assert (await stop_for(env, pat))["video"] is None, "Pat still owes his own video; Sam's is not his"
    assert (await stop_for(env, sam))["video"] == {"id": sams, "state": "held", "byYou": True}
    assert [v["id"] for v in (await env.http.get("/v1/today", headers=pat)).json()["videos"]] == []


async def test_a_helper_with_no_appointment_of_his_own_sees_the_leads_video_and_may_add_his(env: Env) -> None:
    # Sam rides along on Jane's stop: listed as an additional tech, no appointment of his own.
    env.fake.data["appointment"][9001]["additionalTechs"] = str(SAM)
    pat, sam = await both(env)
    shared = await stop_for(env, sam, 501)
    assert shared["appointmentIds"] == [9001, 9002, 9003] and shared["video"] is None

    pats = await film(env, pat, 501)
    assert (await stop_for(env, sam, 501))["video"] == {"id": pats, "state": "held", "byYou": False}
    sams = await film(env, sam, 501)
    assert (await env.video(pats)).state == "held", "the helper's video never replaces the lead's"
    assert (await stop_for(env, sam, 501))["video"]["id"] == sams


# -- the server keeps score per tech --------------------------------------------------


async def test_a_missing_video_is_flagged_for_the_tech_who_owes_it(env: Env) -> None:
    pat, sam = await both(env)
    await film(env, pat, BOB)  # Pat filmed; Sam finished 70 minutes ago and has not
    await env.finished(BOB, ago=timedelta(minutes=70), force=False)
    await env.finished(501, ago=timedelta(minutes=5), force=False)  # Jane: Pat only just finished
    assert await jobs.flag_missing(env.ctx) == 1
    (flag,) = await env.flags("no_video")
    assert (flag.customer_id, flag.tech_id) == (BOB, SAM), "Pat's video does not cover Sam's visit"

    # Sam's own video arriving is what clears it.
    await env.http.post("/v1/videos", json=video_body(BOB), headers=sam)
    assert await env.flags("no_video") == []
    assert await jobs.flag_missing(env.ctx) == 0


async def test_one_techs_finished_work_is_chased_even_while_the_other_is_still_there(env: Env) -> None:
    await both(env)
    await env.finished(BOB, ago=timedelta(hours=2), force=False)  # Sam done; Pat's appointment still open
    stop = await env.one(sa.select(db.stops).where(db.stops.c.customer_id == BOB))
    assert stop.done is False, "the stop as a whole is not finished"
    assert await jobs.flag_missing(env.ctx) == 1
    (flag,) = await env.flags("no_video")
    assert flag.tech_id == SAM


async def test_anothers_techs_video_never_clears_your_flag(env: Env) -> None:
    pat, sam = await both(env)
    await env.finished(BOB, ago=timedelta(hours=2))
    assert await jobs.flag_missing(env.ctx) >= 2
    assert {f.tech_id for f in await env.flags("no_video") if f.customer_id == BOB} == {PAT, SAM}
    await film(env, pat, BOB)
    assert {f.tech_id for f in await env.flags("no_video") if f.customer_id == BOB} == {SAM}


async def test_a_helpers_video_covers_the_stop_for_the_lead(env: Env) -> None:
    env.fake.data["appointment"][9001]["additionalTechs"] = str(SAM)
    pat, sam = await both(env)
    await env.finished(501, ago=timedelta(hours=2), force=False)
    assert await jobs.flag_missing(env.ctx) == 1
    await film(env, sam, 501)
    assert [f for f in await env.flags("no_video") if f.customer_id == 501] == []
    assert await jobs.flag_missing(env.ctx) == 0


# -- the office page ------------------------------------------------------------------


async def test_the_office_page_lists_a_shared_property_under_each_tech_with_his_own_video(env: Env) -> None:
    pat, sam = await both(env)
    sams = await film(env, sam, BOB)
    await send_all(env, sams)
    await env.finished(BOB, ago=timedelta(minutes=5))

    from zest_video.http_office import board, stats

    data = await board(env.ctx, today())
    rows = {name: {r["stop"].customer_id: r for r in rows} for name, rows in data["groups"]}
    assert rows["Sam Ortiz"][BOB]["video"].id == sams and rows["Sam Ortiz"][BOB]["label"] == "Sent"
    assert rows["Pat Rivera"][BOB]["video"] is None and rows["Pat Rivera"][BOB]["label"] == "No video"
    assert rows["Sam Ortiz"][BOB]["view"].services == ["Lawn Care Essential"]
    assert data["others"] == [], "Sam's video sits on his own row, not in the loose pile"

    await office_login(env)
    page = (await env.http.get("/office")).text
    assert page.count("Bob Jones") >= 2, "the property shows once under each tech"

    per_tech = {r["name"]: r for r in await stats(env.ctx, 7)}
    assert (per_tech["Sam Ortiz"]["videos"], per_tech["Sam Ortiz"]["missed"]) == (1, 0)
    assert per_tech["Pat Rivera"]["missed"] >= 1, "Pat's missing video counts against Pat, not Sam"


async def test_the_office_can_move_a_video_onto_a_property_another_tech_already_filmed(env: Env) -> None:
    pat, sam = await both(env)
    sams = await film(env, sam, BOB)
    pats_wrong = await film(env, pat, 501)  # Pat picked Jane by mistake; it was Bob's
    csrf = await office_login(env)
    r = await env.http.post(f"/office/video/{pats_wrong}/move", data={"csrf": csrf, "customer_id": str(BOB)})
    assert r.status_code == 303 and "err=" not in r.headers["location"], r.headers["location"]
    moved = await env.video(pats_wrong)
    assert (moved.customer_id, moved.appointment_ids) == (BOB, [9004]), "tied to Pat's own appointment there"
    assert (await env.video(sams)).state == "held"

    # But not on top of the same tech's own video for that property.
    pats_jane = await film(env, pat, 501)
    r = await env.http.post(f"/office/video/{pats_jane}/move", data={"csrf": csrf, "customer_id": str(BOB)})
    assert "err=" in r.headers["location"] and "Pat" in r.headers["location"]
    assert (await env.video(pats_jane)).customer_id == 501


def minutes_ago_iso(minutes: float) -> str:
    from datetime import datetime, timezone

    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
