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
from .route import LIVE_STATES, in_work_hours, stops_for_day, sync_day

NUDGE_AFTER = timedelta(minutes=30)


async def techs_with_the_app(ctx: Ctx) -> set[int]:
    """Techs who have a signed-in phone. Nobody else can be expected to film a stop, which
    is what makes a one-tech pilot possible without flagging everyone else's route."""
    async with ctx.engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.select(db.devices.c.tech_id)
                .join(db.techs, db.techs.c.id == db.devices.c.tech_id)
                .where(db.devices.c.revoked_at.is_(None), db.techs.c.active.is_(True), db.techs.c.demo.is_(False))
                .distinct()
            )
        ).all()
    return {r.tech_id for r in rows}


async def refresh_day(ctx: Ctx) -> None:
    """Keep today's stops fresh during work hours, so check-ins and finished stops show up.
    Until a tech has the app there is nobody to chase, so no FieldRoutes reads are spent."""
    if in_work_hours(ctx) and await techs_with_the_app(ctx):
        await sync_day(ctx, ctx.today())


async def flag_missing(ctx: Ctx) -> int:
    """A stop that finished a while ago and still has no video: tell the office.

    A video that is filmed but stuck on a phone with no signal looks the same from
    here until it registers; the flag clears itself the moment that video arrives.
    """
    settings = await get_settings(ctx)
    if not settings["require_video"]:
        return 0
    day = ctx.today()
    cutoff = db.now() - timedelta(minutes=settings["missing_alert_minutes"])
    skip = set(settings["skip_service_ids"] or [])
    have = {v.customer_id for v in await _videos_any_state(ctx, day)}
    equipped = await techs_with_the_app(ctx)
    raised = 0
    for stop in await stops_for_day(ctx, day):
        if not stop.done or stop.done_seen_at is None or stop.done_seen_at > cutoff:
            continue
        if stop.customer_id in have:
            continue
        if not equipped.intersection(stop.tech_ids or []):
            continue  # no tech on this stop has the app yet
        types = {a.get("type") for a in stop.appointments}
        if types and types <= skip:
            continue  # every service at this stop is switched off for videos
        tech_id = (stop.tech_ids or [None])[0]
        if await raise_flag(
            ctx, "no_video", f"no_video:{day}:{stop.customer_id}", day=day, tech_id=tech_id,
            customer_id=stop.customer_id, detail=f"{stop.name}, {stop.address}: finished with no video.",
        ):
            raised += 1
    return raised


async def _videos_any_state(ctx: Ctx, day: str) -> list[Any]:
    """Videos that count as 'this stop was filmed': live ones, plus pulled ones (the office
    already knows about those)."""
    async with ctx.engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    sa.select(db.videos.c.customer_id).where(
                        db.videos.c.day == day, db.videos.c.state.in_((*LIVE_STATES, "pulled"))
                    )
                )
            ).all()
        )


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
