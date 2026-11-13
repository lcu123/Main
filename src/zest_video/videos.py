"""The life of a video: register, upload, hold, email, watch, score, pull, move.

States:
    awaiting_upload  registered by the phone, file not confirmed in the bucket
    held             in the bucket, inside the tech's undo window
    sent             the customer was emailed the link
    no_email         link is live but no email went (reason in `email_skip`)
    unmatched        filmed as "not on my list"; waiting for the office to pick the customer
    pulled           office or customer killed the link (file kept so it can be moved)
    deleted          the tech deleted it inside the window
    replaced         the tech recorded again for the same visit

Every step is safe to repeat: the phone retries on bad signal, so a second
register, complete or delete with the same ID must never make a second video
or a second email.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from . import db, security
from .auth import Device
from .core import ApiError, Ctx, audit, clear_flags, get_settings, raise_flag
from .fr import FieldRoutesError, clean, to_id, to_int
from .notify import DeliveryError, Email
from .render import render
from .route import (
    LIVE_STATES, demo_stops, iso, lead_techs, missing_key, public_state, sync_day, tech_view, time_check,
)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SIGNAL_VALUES = ("match", "mismatch", "unknown")
LOCKED_STATES = ("sent", "no_email")  # already went to the customer: the tech cannot change it
EMAIL_RETRY = timedelta(minutes=5)
EMAIL_MAX_ATTEMPTS = 6
PULLED_KEEP_DAYS = 30


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def watch_url(ctx: Ctx, token: str) -> str:
    return f"{ctx.cfg.base_url}/w/{token}"


def describe(v: Any) -> dict[str, Any]:
    return {"id": v.id, "state": public_state(v.state, v.first_viewed_at), "emailAt": iso(v.email_due_at)}


async def get_video(ctx: Ctx, video_id: str) -> Any | None:
    async with ctx.engine.connect() as conn:
        return (await conn.execute(sa.select(db.videos).where(db.videos.c.id == video_id))).first()


async def _drop_object(ctx: Ctx, video: Any) -> None:
    """Delete the file from the bucket. Best effort: the retention sweep retries."""
    try:
        await ctx.storage.delete(video.object_key)
    except Exception:
        return
    async with ctx.engine.begin() as conn:
        await conn.execute(sa.update(db.videos).where(db.videos.c.id == video.id).values(object_deleted=True))


async def _end(ctx: Ctx, video: Any, state: str, by: str, reason: str, *, drop_file: bool) -> bool:
    """Move a live video to an ended state exactly once."""
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video.id, db.videos.c.state.in_(LIVE_STATES))
            .values(state=state, ended_at=db.now(), ended_by=by, ended_reason=reason[:200])
        )
    if not result.rowcount:
        return False
    if drop_file:
        await _drop_object(ctx, video)
    return True


# -- phone: register, complete, delete ----------------------------------------


async def register(ctx: Ctx, device: Device, body: dict[str, Any]) -> dict[str, Any]:
    settings = await get_settings(ctx)
    video_id = str(body.get("id") or "").lower()
    if not UUID_RE.match(video_id):
        raise ApiError(400, "bad_request", "This video has a bad ID.")

    existing = await get_video(ctx, video_id)
    if existing is not None:
        if existing.tech_id != device.tech_id:
            raise ApiError(400, "bad_request", "This video belongs to someone else.")
        if existing.state == "awaiting_upload":
            return {"id": video_id, "state": "awaiting_upload", "upload": _upload(ctx, existing.object_key)}
        return {**describe(existing), "upload": None}

    recorded_at = _parse_time(body.get("recordedAt"))
    size = to_int(body.get("sizeBytes")) or 0
    try:
        duration = float(body.get("durationSec") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if recorded_at is None or size <= 0 or duration <= 0:
        raise ApiError(400, "bad_request", "This video is missing its time or size.")
    if size > ctx.cfg.max_upload_bytes:
        raise ApiError(400, "bad_request", "This video is too big to send.")
    if duration > settings["max_seconds"] + 3:
        raise ApiError(400, "bad_request", "This video is too long.")

    day = str(body.get("stopDate") or "")
    today = ctx.today()
    try:
        age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(day, "%Y-%m-%d")).days
    except ValueError as exc:
        raise ApiError(400, "bad_request", "This video has a bad date.") from exc
    if not -1 <= age <= 14:  # a phone that was offline for days still gets to send
        raise ApiError(400, "bad_request", "This video is too old to send.")

    raw_signals = body.get("signals") if isinstance(body.get("signals"), dict) else {}
    signals = {
        k: (raw_signals.get(k) if raw_signals.get(k) in SIGNAL_VALUES else "unknown")
        for k in ("spoken", "gps", "checkIn")
    }
    location = body.get("location") if isinstance(body.get("location"), dict) else {}
    not_on_list = bool(body.get("notOnMyList"))
    customer_id = None if not_on_list else to_id(body.get("customerId"))
    if not not_on_list and customer_id is None:
        raise ApiError(400, "bad_request", "Pick a stop for this video.")

    snapshot = {"customer_name": "", "address": "", "city_line": ""}
    raw_ids = body.get("appointmentIds")
    appointment_ids = [i for i in (to_id(x) for x in (raw_ids if isinstance(raw_ids, list) else [])[:50]) if i and i > 0]
    stop = None
    if customer_id is not None:
        if device.demo:
            sample = next((s for s in demo_stops(day) if s["customerId"] == customer_id), None)
            if sample is None:
                raise ApiError(400, "bad_request", "That stop is not on the demo list.")
            snapshot = {"customer_name": sample["name"], "address": sample["address"], "city_line": sample["cityLine"]}
        else:
            async with ctx.engine.connect() as conn:
                stop = (
                    await conn.execute(
                        sa.select(db.stops).where(db.stops.c.day == day, db.stops.c.customer_id == customer_id)
                    )
                ).first()
            if stop is None:
                raise ApiError(400, "bad_request", "That stop is not on the list for that day. Use Not On My List.")
            snapshot = {"customer_name": stop.name, "address": stop.address, "city_line": stop.city_line}
            # Tied to this tech's own appointments at the stop, not the other tech's.
            appointment_ids = tech_view(stop, device.tech_id).appointment_ids or appointment_ids

        # One video per tech per customer per day. Recording again inside the undo window
        # swaps this tech's old one out with no question; once it has gone to the customer
        # it is locked. Another tech's video for the same property is its own and is
        # never touched: the customer gets both.
        async with ctx.engine.connect() as conn:
            earlier = (
                await conn.execute(
                    sa.select(db.videos).where(
                        db.videos.c.day == day,
                        db.videos.c.customer_id == customer_id,
                        db.videos.c.tech_id == device.tech_id,
                        db.videos.c.state.in_(LIVE_STATES),
                        # Never this same video: an overlapping retry of this very
                        # registration must not retire its own twin.
                        db.videos.c.id != video_id,
                    )
                )
            ).all()
        for old in earlier:
            if old.state in LOCKED_STATES:
                raise ApiError(
                    409,
                    "visit_has_video",
                    "This video already went to the customer. Call the office to change it.",
                    existing={
                        "id": old.id,
                        "state": public_state(old.state, old.first_viewed_at),
                        "byYou": old.tech_id == device.tech_id,
                        "techFirstName": old.tech_first_name,
                    },
                )
        for old in earlier:
            if await _end(ctx, old, "replaced", f"tech:{device.tech_id}", f"recorded again as {video_id}", drop_file=True):
                await audit(ctx, "tech", device.tech_id, "video_replaced", video_id=old.id, customer_id=customer_id,
                            detail={"by": video_id})

    now = db.now()
    object_key = f"videos/{day}/{video_id}.mp4"
    try:
        await _insert_video(
            ctx,
            dict(
                id=video_id,
                token=security.new_watch_token(),
                tech_id=device.tech_id,
                tech_first_name=device.tech_first_name,
                device_id=device.id,
                demo=device.demo,
                day=day,
                customer_id=customer_id,
                appointment_ids=appointment_ids,
                not_on_list=not_on_list,
                state="awaiting_upload",
                object_key=object_key,
                size_bytes=size,
                etag="",
                duration_sec=duration,
                recorded_at=recorded_at,
                lat=_float(location.get("lat")),
                lng=_float(location.get("lng")),
                accuracy_m=_float(location.get("accuracyM")),
                signals=signals,
                override=bool(body.get("override")),
                recommend=bool(body.get("recommend")),
                spoken_text=clean(body.get("spokenText"))[:1000],
                time_check=time_check(tech_view(stop, device.tech_id) if stop is not None else None, recorded_at),
                created_at=now,
                email_to="",
                email_id="",
                email_skip="",
                ended_by="",
                ended_reason="",
                **snapshot,
            ),
        )
    except sa.exc.IntegrityError:
        # The same ID was registered a moment ago by a retry that overlapped this call
        # (a slow first request, then the phone trying again). Answer as the repeat it is.
        twin = await get_video(ctx, video_id)
        if twin is None or twin.tech_id != device.tech_id:
            raise ApiError(400, "bad_request", "This video belongs to someone else.") from None
        if twin.state == "awaiting_upload":
            return {"id": video_id, "state": "awaiting_upload", "upload": _upload(ctx, twin.object_key)}
        return {**describe(twin), "upload": None}
    await audit(
        ctx, "tech", device.tech_id, "video_registered", video_id=video_id, customer_id=customer_id,
        detail={"signals": signals, "override": bool(body.get("override")), "notOnMyList": not_on_list, "bytes": size},
    )
    if customer_id is not None and not device.demo:
        # The stop has a video on the way, so "finished with no video" no longer holds.
        await _clear_missing(ctx, day, customer_id, device.tech_id, stop, "a video arrived")
        if body.get("override"):
            wrong = [k for k, v in signals.items() if v == "mismatch"] or [k for k, v in signals.items() if v != "match"]
            await raise_flag(
                ctx, "wrong_house", f"override:{video_id}", day=day, tech_id=device.tech_id,
                customer_id=customer_id, video_id=video_id,
                detail=f"{device.tech_first_name} sent it past a warning. Did not agree: {', '.join(wrong)}.",
            )
    return {"id": video_id, "state": "awaiting_upload", "upload": _upload(ctx, object_key)}


async def _insert_video(ctx: Ctx, values: dict[str, Any]) -> None:
    async with ctx.engine.begin() as conn:
        await conn.execute(sa.insert(db.videos).values(**values))


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _upload(ctx: Ctx, object_key: str) -> dict[str, Any]:
    seconds = ctx.cfg.upload_url_seconds
    return {
        "method": "PUT",
        "url": ctx.storage.put_url(object_key, "video/mp4", seconds),
        "headers": {"Content-Type": "video/mp4"},
        "expiresAt": iso(db.now() + timedelta(seconds=seconds)),
    }


def email_due(recorded_at: datetime, uploaded_at: datetime, hold_minutes: int) -> datetime:
    """When the customer email may go: the tech's undo window is `hold_minutes` from the
    recording. A late upload whose window already passed goes a minute after it lands;
    a phone clock running fast can never push the email past one full window."""
    hold = timedelta(minutes=hold_minutes)
    due = min(recorded_at, uploaded_at) + hold
    return min(max(due, uploaded_at + timedelta(minutes=1)), uploaded_at + max(hold, timedelta(minutes=1)))


async def complete(ctx: Ctx, device: Device, video_id: str, body: dict[str, Any]) -> dict[str, Any]:
    video = await get_video(ctx, video_id) if UUID_RE.match(video_id) else None
    if video is None or video.tech_id != device.tech_id:
        raise ApiError(404, "bad_request", "We do not know this video.")
    if video.state != "awaiting_upload":
        if video.state in ("deleted", "replaced"):
            # Its upload may have landed after the file was removed. Remove it again.
            await _drop_object(ctx, video)
        return describe(video)

    stored = await ctx.storage.head(video.object_key)
    claimed = to_int(body.get("sizeBytes")) or video.size_bytes
    if stored is None or stored.size <= 0 or stored.size != claimed or stored.size > ctx.cfg.max_upload_bytes:
        raise ApiError(409, "not_uploaded", "The video did not finish sending. It will try again.")

    settings = await get_settings(ctx)
    now = db.now()
    state = "unmatched" if video.not_on_list else "held"
    due = None if video.not_on_list else email_due(video.recorded_at, now, settings["hold_minutes"])
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video_id, db.videos.c.state == "awaiting_upload")
            .values(state=state, uploaded_at=now, size_bytes=stored.size, etag=stored.etag, email_due_at=due)
        )
    if result.rowcount:
        await audit(ctx, "tech", device.tech_id, "video_uploaded", video_id=video_id, customer_id=video.customer_id,
                    detail={"bytes": stored.size})
        await clear_flags(ctx, db.flags.c.dedupe == f"stuck:{video_id}", "system", "it arrived")
        await _check_format(ctx, video, stored.size)
        if video.not_on_list and not video.demo:
            said = f' He said: "{video.spoken_text[:200]}"' if video.spoken_text else ""
            await raise_flag(
                ctx, "unmatched", f"unmatched:{video_id}", day=video.day, tech_id=video.tech_id, video_id=video_id,
                detail=f"{video.tech_first_name} filmed a stop that was not on the list.{said}",
            )
    fresh = await get_video(ctx, video_id)
    return describe(fresh)


async def delete_by_tech(ctx: Ctx, device: Device, video_id: str) -> dict[str, Any]:
    video = await get_video(ctx, video_id) if UUID_RE.match(video_id) else None
    if video is None or video.tech_id != device.tech_id:
        raise ApiError(404, "bad_request", "We do not know this video.")
    if video.state in ("deleted", "replaced"):
        return {"id": video_id, "state": video.state}
    if video.state not in ("awaiting_upload", "held", "unmatched"):
        raise ApiError(409, "too_late", "This video already went to the customer. Call the office to change it.")
    if video.state == "unmatched":
        # Not on the list: no email is waiting to close the window, so the same ten
        # minutes are counted here. After that it is the office's to match or pull.
        settings = await get_settings(ctx)
        closes = email_due(video.recorded_at, video.uploaded_at or video.created_at, settings["hold_minutes"])
        if db.now() > closes:
            raise ApiError(409, "too_late", "The time to delete this video has passed. Call the office to change it.")
    if not await _end(ctx, video, "deleted", f"tech:{device.tech_id}", "deleted by the tech", drop_file=True):
        raise ApiError(409, "too_late", "This video already went to the customer. Call the office to change it.")
    await audit(ctx, "tech", device.tech_id, "video_deleted", video_id=video_id, customer_id=video.customer_id)
    await clear_flags(ctx, db.flags.c.video_id == video_id, "system", "the tech deleted the video")
    return {"id": video_id, "state": "deleted"}


# -- the email ----------------------------------------------------------------


PLAYS_EVERYWHERE = {"avc1", "avc3"}  # H.264: every browser, phone and TV customers own
SAMPLE_PEEK = 256 * 1024


def video_format(data: bytes) -> str | None:
    """The video codec named in an MP4's track tables ('avc1' is H.264, 'hvc1'/'hev1' HEVC),
    or None when this slice of the file does not hold them."""
    found = []
    at = data.find(b"stsd")
    while at != -1:
        entry = data[at + 16: at + 20]  # stsd, 4 version/flags, 4 count, 4 entry size, then the format
        if len(entry) == 4 and entry.isalnum():
            found.append(entry.decode("ascii", "replace"))
        at = data.find(b"stsd", at + 4)
    video = [f for f in found if f not in ("mp4a", "ac-3", "ec-3", "alac", "Opus", "samr")]
    return video[0] if video else None


async def _check_format(ctx: Ctx, video: Any, size: int) -> None:
    """Tell the office when a phone sends a video that some customers cannot play.

    The app records H.264, which plays everywhere. If a phone ever sends HEVC instead (a
    changed setting, an iOS update, a bug in the export), customers on Windows Chrome,
    Firefox and older Android phones hear the sound over a black picture and nothing
    reports an error. Nothing is converted here; the video still goes out, because most
    phones and every Apple device play it. Best effort: an unreadable file raises nothing."""
    if video.demo:
        return
    try:
        data = await ctx.storage.read_range(video.object_key, 0, SAMPLE_PEEK) or b""
        fmt = video_format(data)
        if fmt is None and size > SAMPLE_PEEK:  # the track tables are at the end of this file
            fmt = video_format(await ctx.storage.read_range(video.object_key, size - SAMPLE_PEEK, SAMPLE_PEEK) or b"")
    except Exception:  # noqa: BLE001 - storage trouble must never fail an upload that is already confirmed
        return
    if fmt is None or fmt in PLAYS_EVERYWHERE:
        return
    name = "HEVC" if fmt in ("hvc1", "hev1", "dvh1", "dvhe") else fmt
    who = video.customer_name or "a stop not on the list"
    await raise_flag(
        ctx, "bad_format", f"format:{video.id}", day=video.day, tech_id=video.tech_id,
        customer_id=video.customer_id, video_id=video.id,
        detail=f"{video.tech_first_name}'s video for {who} was recorded as {name}, not H.264. Many Windows and "
        "older Android browsers show a black picture for it. Watch it yourself, and have that phone's app checked.",
    )


async def file_unchanged(ctx: Ctx, video: Any) -> bool:
    """Is the bucket still holding the file this video was confirmed with?

    The phone's upload link stays valid for a day so that a slow upload can finish,
    which means the same link could put different bytes over a video after the server
    confirmed it. Nothing a customer is sent or shown may be anything but the confirmed
    file, so before an email goes and before the player is handed a link, the object is
    compared with what `complete` recorded. A changed or missing file pulls the video
    and tells the office. A bucket that cannot be reached proves nothing: that case
    lets the video through."""
    if not video.etag:
        return True
    try:
        stored = await ctx.storage.head(video.object_key)
    except Exception:  # noqa: BLE001 - any storage failure means "cannot tell"
        return True
    if stored is not None and stored.etag == video.etag and stored.size == video.size_bytes:
        return True
    what = "is missing from storage" if stored is None else "was changed after it was uploaded"
    if await _end(ctx, video, "pulled", "system", f"the file {what}", drop_file=False):
        await audit(ctx, "system", "storage", "video_pulled", video_id=video.id, customer_id=video.customer_id,
                    detail={"reason": f"the file {what}"})
        if not video.demo:
            name = video.customer_name or "a stop not on the list"
            await raise_flag(
                ctx, "file_changed", f"file:{video.id}", day=video.day, tech_id=video.tech_id,
                customer_id=video.customer_id, video_id=video.id,
                detail=f"{video.tech_first_name}'s video for {name}: the file {what}. It was pulled and its link is dead.",
            )
    return False


async def send_due(ctx: Ctx) -> int:
    """Email every held video whose undo window has closed. Called once a minute."""
    now = db.now()
    async with ctx.engine.connect() as conn:
        due = (
            await conn.execute(
                sa.select(db.videos)
                .where(db.videos.c.state == "held", db.videos.c.email_due_at <= now)
                .order_by(db.videos.c.email_due_at)
                .limit(50)
            )
        ).all()
    # One FieldRoutes read for the whole pass instead of one per email: the daily quota
    # is shared with everything else that touches the office, and a busy afternoon can
    # have dozens of videos due at once.
    known: dict[int, dict[str, Any]] | None = None
    ids = sorted({v.customer_id for v in due if v.customer_id and v.customer_id > 0 and not v.demo})
    if len(ids) > 1 and ctx.cfg.email_mode != "off" and ctx.mailer.delivers:
        try:
            known = await ctx.fr.customers(ids)
        except FieldRoutesError:
            known = None  # each video then asks for itself, and waits if FieldRoutes is down

    sent = 0
    for video in due:
        # Take a lease first: if this process dies mid-send, the next pass retries
        # after the lease instead of two passes sending at once.
        async with ctx.engine.begin() as conn:
            claimed = await conn.execute(
                sa.update(db.videos)
                .where(db.videos.c.id == video.id, db.videos.c.state == "held", db.videos.c.email_due_at <= now)
                .values(email_due_at=now + EMAIL_RETRY)
            )
        if claimed.rowcount and await _deliver(ctx, video, known):
            sent += 1
    return sent


async def _finish(ctx: Ctx, video: Any, state: str, **values: Any) -> bool:
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos).where(db.videos.c.id == video.id, db.videos.c.state == "held").values(state=state, **values)
        )
    return bool(result.rowcount)


async def _deliver(ctx: Ctx, video: Any, known: dict[int, dict[str, Any]] | None = None) -> bool:
    """Decide whether this video's customer gets an email, send it, and file the link.
    `known` is this pass's customer records when they were fetched together."""
    cfg = ctx.cfg
    settings = await get_settings(ctx)
    link = watch_url(ctx, video.token)

    async def no_email(reason: str, *, flag: str = "", show_customer: bool = False) -> bool:
        if not await _finish(ctx, video, "no_email", email_skip=reason):
            return False
        await audit(ctx, "system", "email", "email_skipped", video_id=video.id, customer_id=video.customer_id,
                    detail={"reason": reason})
        if flag:
            await raise_flag(ctx, "no_email", f"no_email:{video.id}", day=video.day, tech_id=video.tech_id,
                             customer_id=video.customer_id, video_id=video.id, detail=flag)
        if not video.demo:
            await _after_send(ctx, video, link, show_customer=show_customer)
        return False

    if video.demo:
        return await no_email("demo")
    if video.customer_id is None:
        return False

    # One video per tech per customer per day is enforced at register, but two
    # registrations from the same tech arriving in the same instant could both get in.
    # Settle it here, before any email: a video that already went out wins; otherwise
    # the newest recording wins. Another tech's video is no rival: both go out.
    async with ctx.engine.connect() as conn:
        rivals = (
            await conn.execute(
                sa.select(db.videos).where(
                    db.videos.c.day == video.day,
                    db.videos.c.customer_id == video.customer_id,
                    db.videos.c.tech_id == video.tech_id,
                    db.videos.c.state.in_(LIVE_STATES),
                    db.videos.c.id != video.id,
                )
            )
        ).all()
    for rival in rivals:
        went_out = rival.state in LOCKED_STATES
        newer = (rival.recorded_at, rival.created_at, rival.id) > (video.recorded_at, video.created_at, video.id)
        if went_out or newer:
            if await _end(ctx, video, "replaced", "system", f"another video for this visit stands ({rival.id})",
                          drop_file=True):
                await audit(ctx, "system", "email", "video_replaced", video_id=video.id,
                            customer_id=video.customer_id, detail={"by": rival.id, "reason": "duplicate for the visit"})
            return False

    if not await file_unchanged(ctx, video):
        return False

    # Server's own time check, now that FieldRoutes has had time to record the check-in.
    try:
        await sync_day(ctx, video.day)
    except FieldRoutesError:
        pass
    async with ctx.engine.connect() as conn:
        stop = (
            await conn.execute(
                sa.select(db.stops).where(db.stops.c.day == video.day, db.stops.c.customer_id == video.customer_id)
            )
        ).first()
    view = tech_view(stop, video.tech_id) if stop is not None else None
    services = _join_words(view.services) if view is not None else ""
    checked = time_check(view, video.recorded_at)
    if checked != video.time_check:
        async with ctx.engine.begin() as conn:
            await conn.execute(sa.update(db.videos).where(db.videos.c.id == video.id).values(time_check=checked))
    if checked == "mismatch":
        await raise_flag(
            ctx, "wrong_house", f"time:{video.id}", day=video.day, tech_id=video.tech_id,
            customer_id=video.customer_id, video_id=video.id,
            detail=f"Recorded at {ctx.local(video.recorded_at):%-I:%M %p}, outside the check-in time for this stop.",
        )

    if cfg.email_mode == "off" or not ctx.mailer.delivers:
        # Off by choice, or no real sender is set up (the log mailer prints, it does not
        # deliver): either way the customer was not emailed, and the record must say so.
        return await no_email("email_off")
    if cfg.email_customer_ids is not None and video.customer_id not in cfg.email_customer_ids:
        return await no_email("not_in_test_list")

    async with ctx.engine.connect() as conn:
        opted_out = (
            await conn.execute(sa.select(db.email_optouts).where(db.email_optouts.c.customer_id == video.customer_id))
        ).first()
    if opted_out:
        return await no_email("opted_out")

    if known is not None:
        customer = known.get(video.customer_id)
    else:
        try:
            customer = (await ctx.fr.customers([video.customer_id])).get(video.customer_id)
        except FieldRoutesError:
            return False  # FieldRoutes is down; the lease brings this video back in a few minutes
    address = clean((customer or {}).get("email")).lower()
    if customer is None or "@" not in address or "." not in address.rpartition("@")[2]:
        return await no_email(
            "no_address", flag="No email on file. The link is saved on the account as a note.", show_customer=True
        )
    if settings["respect_fr_email_reminders"] and to_int(customer.get("emailReminders")) == 0:
        return await no_email(
            "fr_reminders_off",
            flag="Email reminders are off for this customer in FieldRoutes, so no email was sent.",
        )

    to = address
    def fill(text: str) -> str:
        return (
            text.replace("{tech}", video.tech_first_name)
            .replace("{address}", video.address)
            .replace("{service}", services or "service")
        )

    subject = fill(settings["email_subject"])
    intro = fill(settings["email_intro"])
    test_note = ""
    if cfg.email_mode == "test":
        if not cfg.email_test_to:
            return await no_email("email_off")
        to = cfg.email_test_to
        test_note = f"TEST MODE. In live mode this goes to {security.mask_email(address)}."
        subject = f"[TEST] {subject}"

    unsubscribe = f"{cfg.base_url}/u/{video.token}"
    context = dict(
        brand=cfg.brand_name, base_url=cfg.base_url, link=link, intro=intro, tech=video.tech_first_name,
        address=video.address, test_note=test_note, unsubscribe=unsubscribe, office_phone=cfg.office_phone,
        services=services,
    )
    email = Email(
        to=to,
        subject=subject,
        html=render("email_video.html", **context),
        text=(
            f"{test_note}\n\n" if test_note else ""
        )
        + f"{intro}\n\n"
        + (f"Service: {services}\nTechnician: {video.tech_first_name}\n\n" if services else "")
        + f"Watch your video:\n{link}\n\n{cfg.brand_name}\n\nStop these video emails: {unsubscribe}\n",
        reply_to=cfg.mail_reply_to,
        headers={"List-Unsubscribe": f"<{unsubscribe}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
        idempotency_key=f"video-{video.id}-{video.token[:8]}",
        tag="video",
    )
    try:
        email_id = await ctx.mailer.send(email)
    except DeliveryError as exc:
        async with ctx.engine.connect() as conn:
            tries = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(db.events)
                    .where(db.events.c.video_id == video.id, db.events.c.action == "email_failed")
                )
            ).scalar_one()
        await audit(ctx, "system", "email", "email_failed", video_id=video.id, customer_id=video.customer_id,
                    detail={"error": str(exc)[:300]})
        if tries + 1 >= EMAIL_MAX_ATTEMPTS:
            # Stop hammering; the office decides. The link itself is live.
            await no_email("send_failed", flag=f"The email could not be sent after {EMAIL_MAX_ATTEMPTS} tries: {exc}")
        return False

    if not await _finish(ctx, video, "sent", emailed_at=db.now(), email_to=to, email_id=email_id, email_skip=""):
        return False
    await audit(ctx, "system", "email", "email_sent", video_id=video.id, customer_id=video.customer_id,
                detail={"to": security.mask_email(to), "mode": cfg.email_mode})
    await _after_send(ctx, video, link, show_customer=False)
    return True


def _join_words(items: list[str]) -> str:
    """["Lawn Care", "Pest Control"] -> "Lawn Care and Pest Control"."""
    items = [i for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


async def _clear_missing(ctx: Ctx, day: str, customer_id: int, tech_id: int, stop: Any, note: str) -> None:
    """A video is on the way, so 'finished with no video' no longer holds for this tech at
    this stop. A helper's video stands in for whoever leads the stop."""
    await clear_flags(ctx, db.flags.c.dedupe == missing_key(day, customer_id, tech_id), "system", note)
    if stop is not None and tech_id not in lead_techs(stop):
        await clear_flags(ctx, db.flags.c.dedupe.like(f"no_video:{day}:{customer_id}:%"), "system", note)


async def _after_send(ctx: Ctx, video: Any, link: str, *, show_customer: bool) -> None:
    """File the link on the customer's FieldRoutes account, and open the follow-up when
    the tech said he recommended something."""
    if video.customer_id is None or video.customer_id <= 0:
        return
    await enqueue_fr(
        ctx, "note", f"note:{video.id}:{video.token[:8]}", video.customer_id, video.id,
        {"text": f"Service video from {video.tech_first_name}, {video.day}: {link}", "day": video.day,
         "show_customer": show_customer},
    )
    if video.recommend:
        await enqueue_fr(
            ctx, "task", f"task:{video.id}", video.customer_id, video.id,
            {"text": f"{video.tech_first_name} recommended something in the service video. Watch it and follow up: {link}",
             "due": ctx.today()},
        )
        await raise_flag(ctx, "recommend", f"recommend:{video.id}", day=video.day, tech_id=video.tech_id,
                         customer_id=video.customer_id, video_id=video.id,
                         detail=f"{video.tech_first_name} recommended something. Watch the video and follow up.")


async def enqueue_fr(ctx: Ctx, kind: str, dedupe: str, customer_id: int, video_id: str | None, payload: dict[str, Any]) -> None:
    now = db.now()
    try:
        async with ctx.engine.begin() as conn:
            await conn.execute(
                sa.insert(db.fr_outbox).values(
                    dedupe=dedupe, kind=kind, customer_id=customer_id, video_id=video_id, payload=payload,
                    attempts=0, next_try_at=now, last_error="", created_at=now,
                )
            )
    except sa.exc.IntegrityError:
        pass  # already queued: a retry must not write twice


# -- customer: watch, score, not my home ---------------------------------------

WATCHABLE = ("held", "sent", "no_email")


async def by_token(ctx: Ctx, token: str) -> Any | None:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        return None
    async with ctx.engine.connect() as conn:
        return (await conn.execute(sa.select(db.videos).where(db.videos.c.token == token))).first()


def is_watchable(video: Any | None) -> bool:
    return video is not None and video.state in WATCHABLE and not video.object_deleted


async def mark_played(ctx: Ctx, video: Any) -> None:
    now = db.now()
    async with ctx.engine.begin() as conn:
        await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video.id)
            .values(view_count=db.videos.c.view_count + 1, first_viewed_at=sa.func.coalesce(db.videos.c.first_viewed_at, now))
        )
    await audit(ctx, "customer", "link", "video_played", video_id=video.id, customer_id=video.customer_id)


async def get_score(ctx: Ctx, video_id: str) -> Any | None:
    async with ctx.engine.connect() as conn:
        return (await conn.execute(sa.select(db.scores).where(db.scores.c.video_id == video_id))).first()


async def save_score(ctx: Ctx, video: Any, stars: int, comment: str) -> None:
    if not 1 <= stars <= 5:
        raise ApiError(400, "bad_request", "Pick 1 to 5 stars.")
    comment = comment.replace("\x00", "").strip()[:1000]
    now = db.now()
    first = False
    for attempt in (1, 2):
        try:
            async with ctx.engine.begin() as conn:
                updated = await conn.execute(
                    sa.update(db.scores).where(db.scores.c.video_id == video.id).values(stars=stars, comment=comment)
                )
                first = updated.rowcount == 0
                if first:
                    await conn.execute(
                        sa.insert(db.scores).values(video_id=video.id, stars=stars, comment=comment, created_at=now)
                    )
            break
        except sa.exc.IntegrityError:
            # Two Sends in the same instant (a double tap on a slow link): the other one
            # inserted first. Go round once more; this time the row is there to update, so
            # the customer sees the thank-you and FieldRoutes still gets one review.
            if attempt == 2:
                raise
    await audit(ctx, "customer", "link", "score_saved", video_id=video.id, customer_id=video.customer_id,
                detail={"stars": stars, "first": first})
    if video.demo or video.customer_id is None:
        return
    appointment_id = next((i for i in video.appointment_ids or [] if i), None)
    if first and appointment_id:
        # FieldRoutes takes one review per save; a changed score stays here and is not re-sent.
        await enqueue_fr(ctx, "review", f"review:{video.id}", video.customer_id, video.id,
                         {"appointment_id": appointment_id, "stars": stars, "feedback": comment, "at": now.isoformat()})
    if stars <= 3:
        said = f': "{comment[:300]}"' if comment else "."
        await raise_flag(ctx, "low_score", f"low_score:{video.id}", day=video.day, tech_id=video.tech_id,
                         customer_id=video.customer_id, video_id=video.id, detail=f"{stars} of 5 stars{said}")


async def not_my_home(ctx: Ctx, video: Any) -> None:
    if await _end(ctx, video, "pulled", "customer", "customer said: not my home", drop_file=False):
        await audit(ctx, "customer", "link", "not_my_home", video_id=video.id, customer_id=video.customer_id)
        await raise_flag(
            ctx, "not_my_home", f"not_my_home:{video.id}", day=video.day, tech_id=video.tech_id,
            customer_id=video.customer_id, video_id=video.id,
            detail=f"The person who opened the link for {video.customer_name} said it is not their home. "
            "The link is dead. Move the video to the right customer.",
        )


async def has_opted_out(ctx: Ctx, video: Any) -> bool:
    if video.customer_id is None or video.customer_id <= 0:
        return False
    async with ctx.engine.connect() as conn:
        return (
            await conn.execute(sa.select(db.email_optouts).where(db.email_optouts.c.customer_id == video.customer_id))
        ).first() is not None


async def opt_out(ctx: Ctx, video: Any) -> None:
    if video.customer_id is None or video.customer_id <= 0:
        return
    try:
        async with ctx.engine.begin() as conn:
            await conn.execute(sa.insert(db.email_optouts).values(customer_id=video.customer_id, at=db.now()))
    except sa.exc.IntegrityError:
        return
    await audit(ctx, "customer", "link", "email_opt_out", video_id=video.id, customer_id=video.customer_id)


# -- office: pull, move -------------------------------------------------------


async def pull(ctx: Ctx, video_id: str, by: str, reason: str) -> None:
    video = await get_video(ctx, video_id) if UUID_RE.match(video_id) else None
    if video is None:
        raise ApiError(404, "bad_request", "No such video.")
    if not await _end(ctx, video, "pulled", by, reason or "pulled by the office", drop_file=False):
        raise ApiError(409, "bad_request", "That video is already gone.")
    await audit(ctx, "office", by, "video_pulled", video_id=video_id, customer_id=video.customer_id,
                detail={"reason": reason})


async def move(ctx: Ctx, video_id: str, customer_id: int, by: str) -> None:
    """Give a video to the right customer. The old link dies (new token) and the new
    customer gets the email on the next pass, with no second undo window."""
    video = await get_video(ctx, video_id) if UUID_RE.match(video_id) else None
    if video is None:
        raise ApiError(404, "bad_request", "No such video.")
    if video.state not in (*LIVE_STATES, "pulled") or video.state == "awaiting_upload":
        raise ApiError(409, "bad_request", "That video cannot be moved.")
    if video.object_deleted:
        raise ApiError(409, "bad_request", "That video's file is gone, so it cannot be moved.")
    if video.customer_id == customer_id and video.state != "pulled":
        raise ApiError(400, "bad_request", "It is already on that customer.")

    async with ctx.engine.connect() as conn:
        stop = (
            await conn.execute(
                sa.select(db.stops).where(db.stops.c.day == video.day, db.stops.c.customer_id == customer_id)
            )
        ).first()
        clash = (
            await conn.execute(
                sa.select(db.videos.c.id).where(
                    db.videos.c.day == video.day,
                    db.videos.c.customer_id == customer_id,
                    db.videos.c.tech_id == video.tech_id,
                    db.videos.c.state.in_(LIVE_STATES),
                    db.videos.c.id != video_id,
                )
            )
        ).first()
    if clash:
        raise ApiError(
            409, "bad_request", f"That customer already has a video from {video.tech_first_name} for that day. Pull it first."
        )

    if stop is not None:
        snapshot = dict(customer_name=stop.name, address=stop.address, city_line=stop.city_line,
                        appointment_ids=tech_view(stop, video.tech_id).appointment_ids)
    else:
        try:
            customer = (await ctx.fr.customers([customer_id])).get(customer_id)
        except FieldRoutesError as exc:
            raise ApiError(503, "server_error", f"FieldRoutes did not answer: {exc}") from exc
        if customer is None:
            raise ApiError(400, "bad_request", f"FieldRoutes has no customer {customer_id}.")
        from .route import _city_line, _customer_name  # same naming rules as the day's stops

        snapshot = dict(customer_name=_customer_name(customer)[0], address=clean(customer.get("address")),
                        city_line=_city_line(customer), appointment_ids=[])

    now = db.now()
    async with ctx.engine.begin() as conn:
        await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video_id)
            .values(
                customer_id=customer_id, token=security.new_watch_token(), state="held", not_on_list=False,
                email_due_at=now, emailed_at=None, email_to="", email_id="", email_skip="",
                first_viewed_at=None, view_count=0, ended_at=None, ended_by="", ended_reason="", **snapshot,
            )
        )
        await conn.execute(sa.delete(db.scores).where(db.scores.c.video_id == video_id))
    await audit(ctx, "office", by, "video_moved", video_id=video_id, customer_id=customer_id,
                detail={"from": video.customer_id, "to": customer_id})
    await clear_flags(ctx, db.flags.c.video_id == video_id, by, f"moved to customer {customer_id}")
    await _clear_missing(ctx, video.day, customer_id, video.tech_id, stop, "a video was moved here")


# -- FieldRoutes outbox --------------------------------------------------------

OUTBOX_BACKOFF = [1, 5, 15, 60, 180, 360, 720, 1440]  # minutes


async def run_outbox(ctx: Ctx) -> int:
    from .fr import WriteBlocked

    now = db.now()
    async with ctx.engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.select(db.fr_outbox)
                .where(db.fr_outbox.c.done_at.is_(None), db.fr_outbox.c.failed_at.is_(None))
                .where(db.fr_outbox.c.next_try_at <= now)
                .order_by(db.fr_outbox.c.id)
                .limit(25)
            )
        ).all()
    done = 0
    for row in rows:
        p = row.payload
        values: dict[str, Any]
        try:
            if row.kind == "note":
                await ctx.fr.create_note(row.customer_id, p["text"], day=p["day"], show_customer=bool(p.get("show_customer")))
            elif row.kind == "review":
                await ctx.fr.create_review(row.customer_id, int(p["appointment_id"]), int(p["stars"]), p.get("feedback", ""),
                                           datetime.fromisoformat(p["at"]))
            elif row.kind == "task":
                await ctx.fr.create_task(row.customer_id, p["text"], due=p["due"])
            values = {"done_at": db.now(), "last_error": ""}
            done += 1
        except WriteBlocked as exc:
            values = {"done_at": db.now(), "last_error": f"skipped: {exc}"}
        except FieldRoutesError as exc:
            attempts = row.attempts + 1
            if attempts >= len(OUTBOX_BACKOFF):
                values = {"attempts": attempts, "failed_at": db.now(), "last_error": str(exc)[:500]}
                await raise_flag(
                    ctx, "fr_write_failed", f"fr:{row.id}", day=ctx.today(), customer_id=row.customer_id,
                    video_id=row.video_id, detail=f"Could not save the {row.kind} to FieldRoutes: {exc}",
                )
            else:
                values = {
                    "attempts": attempts,
                    "next_try_at": db.now() + timedelta(minutes=OUTBOX_BACKOFF[attempts]),
                    "last_error": str(exc)[:500],
                }
        async with ctx.engine.begin() as conn:
            await conn.execute(sa.update(db.fr_outbox).where(db.fr_outbox.c.id == row.id).values(**values))
    return done
