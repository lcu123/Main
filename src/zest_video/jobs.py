"""The server keeps score, not the phone.

One loop, one tick a minute. It knows from FieldRoutes which stops finished,
knows which videos arrived, and chases the gap: nudge the tech when a video is
stuck on his phone, tell the office when a stop finished with no video.

Every job is written to be run again safely, so a crash mid-tick or a second
tick overlapping the first cannot send two emails or raise two flags.
"""

from __future__ import annotations

import asyncio
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

import sqlalchemy as sa

from . import db, videos
from .auth import sync_techs
from .core import Ctx, clear_flags, get_settings, raise_flag
from .fr import FieldRoutesError
from .notify import DeliveryError
from .route import (
    LIVE_STATES, covers, helper_techs, in_work_hours, lead_techs, missing_key, stops_for_day, sync_day, tech_view,
)

NUDGE_AFTER = timedelta(minutes=30)


async def techs_with_the_app(ctx: Ctx, *, since_before: datetime | None = None) -> set[int]:
    """Techs who have a signed-in phone. Nobody else can be expected to film a stop, which
    is what makes a one-tech pilot possible without flagging everyone else's route.
    `since_before` keeps only phones signed in before that moment: a tech who got the app
    this morning is not chased for yesterday."""
    query = (
        sa.select(db.devices.c.tech_id)
        .join(db.techs, db.techs.c.id == db.devices.c.tech_id)
        .where(db.devices.c.revoked_at.is_(None), db.techs.c.active.is_(True), db.techs.c.demo.is_(False))
        .distinct()
    )
    if since_before is not None:
        query = query.where(db.devices.c.created_at < since_before)
    async with ctx.engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    return {r.tech_id for r in rows}


async def refresh_day(ctx: Ctx) -> None:
    """Keep today's stops fresh during work hours, so check-ins and finished stops show up.
    Until a tech has the app there is nobody to chase, so no FieldRoutes reads are spent."""
    if in_work_hours(ctx) and await techs_with_the_app(ctx):
        await sync_day(ctx, ctx.today())


MOVED_LOOK_BACK_DAYS = 14  # as far back as a phone may still hand in a video (`videos.register`)


async def watch_route(ctx: Ctx) -> int:
    """Check the videos against the route as FieldRoutes has it now, and tell the office
    when the two no longer agree: the office cancelled a visit, moved it to another day,
    or gave the stop to another tech, after (or just before) it was filmed.

    It reads nothing from FieldRoutes: it works from the stops the last refresh saved. A
    video still inside its 10 minutes is left to the emailer, which decides when the
    email is due and tells the office itself at that moment. This round picks the videos
    worth another look and hands them to `videos.review_route`:
    - today's and yesterday's, the two days that are still read (yesterday once, at 3 a.m.);
    - any video waiting for the office, and any with an open flag, whatever its day;
    - an earlier video for a customer who is on today's list: the office may have rolled
      a visit that was already filmed to today;
    - a video that went out in the last hour while it and the route did not agree,
      whatever its day. The emailer tells the office itself the moment such a video goes;
      this is for the one it sent just before the server was stopped.
    """
    today = ctx.today()
    day = datetime.strptime(today, "%Y-%m-%d").date()
    yesterday = (day - timedelta(days=1)).isoformat()
    since = (day - timedelta(days=MOVED_LOOK_BACK_DAYS)).isoformat()
    went = db.videos.c.state.in_(("sent", "no_email"))
    async with ctx.engine.connect() as conn:
        # The emailer notes on its own audit entry when a video went out against the route.
        just_went = sorted(
            {
                e.video_id
                for e in (
                    await conn.execute(
                        sa.select(db.events.c.video_id, db.events.c.detail).where(
                            db.events.c.at >= db.now() - timedelta(hours=1),
                            db.events.c.action.in_(("email_sent", "email_skipped")),
                        )
                    )
                ).all()
                if e.video_id and isinstance(e.detail, dict) and e.detail.get("off_route")
            }
        )
        flagged = sorted(
            {
                f.video_id
                for f in (
                    await conn.execute(
                        sa.select(db.flags.c.video_id).where(
                            db.flags.c.kind.in_(videos.ROUTE_FLAGS), db.flags.c.cleared_at.is_(None)
                        )
                    )
                ).all()
                if f.video_id
            }
        )
        here_today = [
            s.customer_id
            for s in (
                await conn.execute(
                    sa.select(db.stops.c.customer_id).where(db.stops.c.day == today, db.stops.c.gone.is_(False))
                )
            ).all()
        ]
        rows = (
            await conn.execute(
                sa.select(db.videos.c.id).where(
                    db.videos.c.demo.is_(False),
                    db.videos.c.customer_id.is_not(None),
                    sa.or_(
                        sa.and_(db.videos.c.id.in_(just_went or [""]), went),
                        sa.and_(db.videos.c.day.in_((today, yesterday)), went),
                        sa.and_(db.videos.c.state == "no_email", db.videos.c.email_skip.in_(videos.HELD_BACK)),
                        db.videos.c.id.in_(flagged or [""]),
                        sa.and_(
                            db.videos.c.day >= since, db.videos.c.day < today, went,
                            db.videos.c.customer_id.in_(here_today or [0]),
                        ),
                    ),
                )
            )
        ).all()
    return await videos.review_route(ctx, [r.id for r in rows], read_since=yesterday, just_went=just_went)


async def flag_missing(ctx: Ctx, day: str | None = None) -> int:
    """A tech's work at a stop finished a while ago and he still has no video: tell the office.

    One video per tech per property: when two techs each have appointments at a stop,
    each owes his own. A helper's video stands in for the lead's, and so does one left by
    the tech who had the work before the office moved it (`route.covers`). A video that
    is filmed but stuck on a phone with no signal looks the same from here until it
    registers; the flag clears itself the moment that video arrives.

    The flag follows the route. When the office takes a stop off the day or gives it to
    another tech, the flag for the tech who no longer has it clears itself, and comes
    back if the stop does. A flag also clears when a video that stands in for his turns
    up some other way (it finished uploading, or the stop was moved onto him), so the
    office page never chases a stop the phone shows as filmed. `day` is today unless told
    otherwise; a day that is over gets no waiting time.
    """
    settings = await get_settings(ctx)
    if not settings["require_video"]:
        return 0
    today = ctx.today()
    day = day or today
    if day == today:
        cutoff = db.now() - timedelta(minutes=settings["missing_alert_minutes"])
        equipped = await techs_with_the_app(ctx)
    else:
        cutoff = db.now()
        equipped = await techs_with_the_app(ctx, since_before=_end_of(ctx, day))
    skip = set(settings["skip_service_ids"] or [])
    filmed: dict[int, list[Any]] = {}
    for v in await _videos_any_state(ctx, day):
        filmed.setdefault(v.customer_id, []).append(v)
    raised = 0
    on_route: set[str] = set()
    filmed_now: list[str] = []
    stops = await stops_for_day(ctx, day)
    before: dict[int, Any] | None = None  # appointment -> a video that went out for it on an earlier day
    for stop in stops:
        helpers = helper_techs(stop)
        here = filmed.get(stop.customer_id, [])
        for tech_id in lead_techs(stop) or list((stop.tech_ids or [])[:1]):
            key = missing_key(day, stop.customer_id, tech_id)
            on_route.add(key)
            if any(covers(stop, tech_id, v.tech_id, v.appointment_ids, v.state) for v in here):
                filmed_now.append(key)
                continue  # he filmed it, or a video that stands in for his is there
            view = tech_view(stop, tech_id)
            if not view.done or view.done_seen_at is None or view.done_seen_at > cutoff:
                continue
            if not equipped.intersection([tech_id, *helpers]):
                continue  # nobody who could film this has the app yet
            if view.types and view.types <= skip:
                continue  # every service he did here is switched off for videos
            # The office may have moved a visit that was already filmed to this day. Whether
            # that first video is enough is the office's call, so the flag says it is there.
            if before is None:
                before = await _filmed_before(ctx, day, [s.customer_id for s in stops])
            first = next((before[a] for a in view.appointment_ids if a in before), None)
            already = (
                f" A video for this same visit went out on {videos.short_day(first.day)} "
                f"({first.tech_first_name}'s), before the visit was moved to this day."
                if first is not None else ""
            )
            if await videos.flag_again(
                ctx, "no_video", key, day=day, tech_id=tech_id, customer_id=stop.customer_id,
                detail=f"{stop.name}, {stop.address}: finished with no video.{already}",
            ):
                raised += 1

    async with ctx.engine.connect() as conn:
        still_open = [
            f.dedupe
            for f in (
                await conn.execute(
                    sa.select(db.flags.c.dedupe).where(
                        db.flags.c.kind == "no_video", db.flags.c.day == day, db.flags.c.cleared_at.is_(None)
                    )
                )
            ).all()
        ]
    # A flag for a stop that has a video after all (one that stands in for his turned up).
    arrived = [key for key in still_open if key in set(filmed_now)]
    if arrived:
        await clear_flags(ctx, db.flags.c.dedupe.in_(arrived), "system", videos.VIDEO_ARRIVED)
    # A flag for a stop the office has since taken off the day, or off this tech's route,
    # chases nobody: clear it, and say why.
    stale = [key for key in still_open if key not in on_route]
    if stale:
        await clear_flags(ctx, db.flags.c.dedupe.in_(stale), "system", videos.OFF_THIS_ROUTE)
    return raised


def _end_of(ctx: Ctx, day: str) -> datetime:
    """The first moment after an office-local day."""
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=ctx.cfg.tz) + timedelta(days=1)


async def _videos_any_state(ctx: Ctx, day: str) -> list[Any]:
    """Videos that count as 'this stop was filmed': live ones, plus pulled ones (the office
    already knows about those). A pulled one counts for the tech who filmed it and his
    helper or lead, not for a tech the stop was moved to afterwards (`route.covers`)."""
    async with ctx.engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    sa.select(
                        db.videos.c.customer_id, db.videos.c.tech_id, db.videos.c.appointment_ids, db.videos.c.state
                    ).where(db.videos.c.day == day, db.videos.c.state.in_((*LIVE_STATES, "pulled")))
                )
            ).all()
        )


async def _filmed_before(ctx: Ctx, day: str, customer_ids: list[int]) -> dict[int, Any]:
    """For these customers: appointment -> the newest video that went out for it on an
    earlier day. An appointment has one date, so finding it on `day` after it was filmed
    on another means the office moved the visit."""
    since = (datetime.strptime(day, "%Y-%m-%d").date() - timedelta(days=MOVED_LOOK_BACK_DAYS)).isoformat()
    async with ctx.engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.select(db.videos)
                .where(db.videos.c.demo.is_(False), db.videos.c.customer_id.in_(customer_ids or [0]))
                .where(db.videos.c.day >= since, db.videos.c.day < day, db.videos.c.state.in_(("sent", "no_email")))
                .order_by(db.videos.c.day)
            )
        ).all()
    out: dict[int, Any] = {}
    for v in rows:
        if videos.went_out(v):
            for appointment_id in v.appointment_ids or []:
                out[appointment_id] = v
    return out


async def chase_stuck(ctx: Ctx) -> None:
    """Registered but never finished uploading: push the tech at 30 minutes, flag the
    office at `stuck_hours`."""
    settings = await get_settings(ctx)
    now = db.now()
    async with ctx.engine.connect() as conn:
        waiting = (
            await conn.execute(
                sa.select(db.videos).where(
                    db.videos.c.state == "awaiting_upload", db.videos.c.created_at <= now - NUDGE_AFTER
                )
            )
        ).all()
    for video in waiting:
        if video.demo:
            continue
        if video.stuck_nudged_at is None:
            await _nudge(ctx, video)
        if now - video.created_at >= timedelta(hours=settings["stuck_hours"]):
            name = video.customer_name or "a stop not on the list"
            await raise_flag(
                ctx, "stuck_upload", f"stuck:{video.id}", day=video.day, tech_id=video.tech_id,
                customer_id=video.customer_id, video_id=video.id,
                detail=f"{video.tech_first_name}'s video for {name} has been stuck on the phone "
                f"for over {settings['stuck_hours']} hours.",
            )


async def _nudge(ctx: Ctx, video: Any) -> None:
    async with ctx.engine.begin() as conn:
        claimed = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video.id, db.videos.c.stuck_nudged_at.is_(None))
            .values(stuck_nudged_at=db.now())
        )
        if not claimed.rowcount:
            return
        device = (await conn.execute(sa.select(db.devices).where(db.devices.c.id == video.device_id))).first()
    if device is None or not device.apns_token or device.revoked_at is not None or not ctx.pusher.configured:
        return
    try:
        alive = await ctx.pusher.send(
            device.apns_token,
            title="A video is waiting to send",
            body="Open Zest Video so it can finish sending.",
            sandbox=bool(device.apns_sandbox),
        )
    except DeliveryError:
        return
    if not alive:
        async with ctx.engine.begin() as conn:
            await conn.execute(sa.update(db.devices).where(db.devices.c.id == device.id).values(apns_token=""))


async def daily(ctx: Ctx) -> None:
    """Once a day: re-check who is still an active tech, and clear out what is too old."""
    try:
        await sync_techs(ctx)
    except FieldRoutesError:
        pass
    ctx.fr.forget_service_names()
    settings = await get_settings(ctx)
    now = db.now()

    # Work closed out after hours (a tech completing his appointments from home) is never
    # seen by the day's refreshes, which stop with work hours. One more look at yesterday,
    # then the same check with no waiting time. Costs one read of the day, and none when
    # nobody had the app.
    yesterday = (ctx.local(now).date() - timedelta(days=1)).isoformat()
    if settings["require_video"] and await techs_with_the_app(ctx, since_before=_end_of(ctx, yesterday)):
        try:
            await sync_day(ctx, yesterday, force=True)
        except FieldRoutesError:
            pass  # the saved stops for yesterday still stand
        await flag_missing(ctx, yesterday)

    # Videos past their keep time, demo videos after a day, pulled files after a month,
    # and any file whose earlier delete did not go through.
    keep = timedelta(days=30 * settings["retention_months"])
    async with ctx.engine.connect() as conn:
        old = (
            await conn.execute(
                sa.select(db.videos).where(
                    db.videos.c.object_deleted.is_(False),
                    sa.or_(
                        db.videos.c.created_at < now - keep,
                        sa.and_(db.videos.c.demo.is_(True), db.videos.c.created_at < now - timedelta(days=1)),
                        sa.and_(
                            db.videos.c.state == "pulled",
                            db.videos.c.ended_at < now - timedelta(days=videos.PULLED_KEEP_DAYS),
                        ),
                        db.videos.c.state.in_(("deleted", "replaced")),
                        sa.and_(
                            db.videos.c.state == "awaiting_upload", db.videos.c.created_at < now - timedelta(days=15)
                        ),
                    ),
                )
            )
        ).all()
    for video in old:
        if video.state in LIVE_STATES:
            await videos._end(ctx, video, "pulled", "system", "kept as long as the settings allow", drop_file=True)
        else:
            await videos._drop_object(ctx, video)

    # A video deleted or replaced while its upload was still in the air can leave a file
    # behind: the upload lands after our delete. Deleting is safe to repeat, so sweep the
    # last few days of them once more.
    async with ctx.engine.connect() as conn:
        late = (
            await conn.execute(
                sa.select(db.videos).where(
                    db.videos.c.state.in_(("deleted", "replaced")),
                    db.videos.c.ended_at > now - timedelta(days=3),
                )
            )
        ).all()
    for video in late:
        await videos._drop_object(ctx, video)

    async with ctx.engine.begin() as conn:
        await conn.execute(sa.delete(db.login_codes).where(db.login_codes.c.created_at < now - timedelta(days=2)))
        await conn.execute(sa.delete(db.office_sessions).where(db.office_sessions.c.expires_at < now))
        old_day = (ctx.local(now).date() - timedelta(days=120)).isoformat()
        await conn.execute(sa.delete(db.stops).where(db.stops.c.day < old_day))
        await conn.execute(sa.delete(db.events).where(db.events.c.at < now - timedelta(days=730)))
        await conn.execute(sa.delete(db.fr_outbox).where(db.fr_outbox.c.created_at < now - timedelta(days=90)))
    # Yesterday's "no video" flags that got a late video are already cleared; drop stale sync marks.
    today = ctx.today()
    for day in [d for d in ctx.last_sync if d != today]:
        ctx.last_sync.pop(day, None)
    await clear_flags(
        ctx,
        sa.and_(db.flags.c.kind == "stuck_upload", db.flags.c.created_at < now - timedelta(days=30)),
        "system",
        "older than 30 days",
    )


JOBS: list[tuple[str, int, Callable[[Ctx], Awaitable[Any]]]] = [
    # name, every N seconds, job
    ("send_due", 60, videos.send_due),
    ("outbox", 60, videos.run_outbox),
    ("refresh_day", 300, refresh_day),
    ("watch_route", 300, watch_route),
    ("flag_missing", 300, flag_missing),
    ("chase_stuck", 300, chase_stuck),
    ("daily", 24 * 3600, daily),
]


async def run_forever(ctx: Ctx, *, tick: float = 20.0) -> None:
    last: dict[str, float] = {}
    last_daily: str | None = None
    while True:
        now = time.monotonic()
        for name, every, job in JOBS:
            if name == "daily":
                # Run at the first tick after 3 a.m. local, and once at startup.
                local = datetime.now(ctx.cfg.tz)
                stamp = local.date().isoformat()
                if last_daily == stamp or (last_daily is not None and local.hour < 3):
                    continue
                last_daily = stamp
            elif now - last.get(name, -1e9) < every:
                continue
            last[name] = now
            try:
                await job(ctx)
            except asyncio.CancelledError:
                raise
            except FieldRoutesError as exc:
                print(f"[jobs] {name}: FieldRoutes: {exc}", file=sys.stderr, flush=True)
            except Exception:
                print(f"[jobs] {name} failed:\n{traceback.format_exc()}", file=sys.stderr, flush=True)
        await asyncio.sleep(tick)
