"""The life of a video: register, upload, hold, email, watch, score, pull, move.

States:
    awaiting_upload  registered by the phone, file not confirmed in the bucket
    held             in the bucket, inside the tech's undo window
    sent             the customer was emailed the link
    no_email         link is live but no email went (reason in `email_skip`); with
                     `off_route`, `second_video` or `overdue` it is waiting for the office
                     (`held_back`): the visit left the day, another tech's video for it
                     already stands, or its email was due hours ago
    unmatched        filmed as "not on my list"; waiting for the office to pick the customer
    pulled           office or customer killed the link (file kept so it can be moved)
    deleted          the tech deleted it inside the window
    replaced         the tech recorded again for the same visit

Every step is safe to repeat: the phone retries on bad signal, so a second
register, complete or delete with the same ID must never make a second video
or a second email.

Every step is also safe to be killed in. The server can be stopped at any
moment (a deploy, a restart), so a change and what follows from it (the audit
entry, the office's flag, the FieldRoutes note in the outbox) are written in
one save: all of it is there afterwards, or none of it and the step runs again.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import sqlalchemy as sa

from . import db, security
from .auth import Device
from .core import ApiError, Ctx, add_event, add_flag, audit, clear_flags, close_flags, get_settings, raise_flag
from .fr import FieldRoutesError, clean, to_id, to_int
from .notify import AlreadyTaken, DeliveryError, Email
from .render import render
from .route import (
    HELD_BACK, IN_BUCKET, LIVE_STATES, OFF_ROUTE, OVERDUE, SECOND, covers, demo_stops, held_back, iso, lead_techs,
    missing_key, public_state, sync_day, tech_view, time_check,
)
from .storage import MISSING, UNKNOWN, plain_etag, words_not_a_file

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SIGNAL_VALUES = ("match", "mismatch", "unknown")
LOCKED_STATES = ("sent", "no_email")  # already went to the customer: the tech cannot change it
EMAIL_RETRY = timedelta(minutes=5)
EMAIL_MAX_ATTEMPTS = 6
PULLED_KEEP_DAYS = 30
# OFF_ROUTE, SECOND and OVERDUE (from route.py) are each a flag kind and the `email_skip`
# of a video waiting on the office for that reason; HELD_BACK is the three together.
ROUTE_HELD = (OFF_ROUTE, SECOND)  # held back by the route: let go by itself when the route says so
STILL_WAITING = "video_waiting"  # flag kind: held back, the reason is over, and it still waits
ROUTE_FLAGS = (OFF_ROUTE, SECOND, STILL_WAITING, OVERDUE)  # the flag kinds `review_route` keeps
FILE_DOUBT = "file_doubt"  # flag kind: the storage will not say whether a video's file is there
DOUBT_FLAG_AFTER = timedelta(minutes=30)  # how long an email waits on that before the office is told
FILED = ":was:"  # in a route flag's key once the thing it was about is over (see `_file_flags`)
PLACED_BY_OFFICE = ("video_moved", "video_send_anyway")  # audit entries that settle where a video belongs
# Why the server cleared a flag by itself. A flag cleared for one of these reasons comes
# back if the reason stops being true (`flag_again`); one a person cleared never does.
BACK_ON_ROUTE = "the stop is back on the route"
ROUTE_CHANGED_AGAIN = "the route changed again"
OFF_THIS_ROUTE = "taken off this tech's route in FieldRoutes"
VIDEO_ARRIVED = "a video arrived"
VIDEO_MOVED_HERE = "a video was moved here"
CLEARED_FOR_NOW = (BACK_ON_ROUTE, OFF_THIS_ROUTE, VIDEO_ARRIVED, VIDEO_MOVED_HERE)


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _locked_words(video: Any) -> str:
    """What a tech is told when he can no longer change a video himself."""
    if held_back(video):
        # Held back because the visit left the day: it has not gone to anyone yet.
        return "The office is looking at this video. Call the office to change it."
    return "This video already went to the customer. Call the office to change it."


def went_out(video: Any) -> bool:
    """Has this video gone as far as it will by itself: emailed, or filed with no email?"""
    return video.state == "sent" or (video.state == "no_email" and video.email_skip not in HELD_BACK)


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


async def _end(
    ctx: Ctx, video: Any, state: str, by: str, reason: str, *, drop_file: bool,
    event: tuple[str, str | int, str, dict[str, Any] | None] | None = None,
    flag: tuple[str, str, str] | None = None,
    clear: tuple[str, str] | None = None,
) -> bool:
    """Move a live video to an ended state exactly once.

    What follows from it is written in the same save, so a server killed right here leaves
    all of it or none of it: `event` is the audit entry (who, by name, what, detail),
    `flag` a flag for the office (kind, key, words), and `clear` closes this video's own
    open flags (by whom, why)."""
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video.id, db.videos.c.state.in_(LIVE_STATES))
            .values(state=state, ended_at=db.now(), ended_by=by, ended_reason=reason[:200])
        )
        if result.rowcount:
            if event is not None:
                actor_type, actor, action, detail = event
                await add_event(conn, actor_type, actor, action, video_id=video.id, customer_id=video.customer_id,
                                detail=detail)
            if clear is not None:
                await close_flags(conn, db.flags.c.video_id == video.id, *clear)
            if flag is not None:
                kind, dedupe, words = flag
                await add_flag(conn, kind, dedupe, day=video.day, tech_id=video.tech_id,
                               customer_id=video.customer_id, video_id=video.id, detail=words)
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
        # A repeat. The first try may have been cut off after the video was saved, so
        # finish whatever it left undone before answering.
        await _registered(ctx, existing)
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
            # A stop the office has since cancelled or given to someone else is still taken:
            # the tech filmed it from the list his phone had, and a video is never refused
            # for that. What happens to it is decided when the email is due (`_deliver`).
            if device.tech_id in (stop.tech_ids or []):
                # Tied to this tech's own appointments at the stop, not the other tech's.
                appointment_ids = tech_view(stop, device.tech_id).appointment_ids or appointment_ids
            else:
                # His list was out of date: this work is no longer his. Keep the appointments
                # his phone says he filmed, as far as the stop still has them. A phone that
                # names none gets none: guessing would tie his video to another tech's work,
                # and then it would be emailed for that work and count as that tech's video.
                # Tied to nothing, it waits for the office.
                known = {a.get("id") for a in (stop.appointments or []) if isinstance(a, dict)}
                appointment_ids = [i for i in appointment_ids if i in known]

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
                    _locked_words(old),
                    existing={
                        "id": old.id,
                        "state": public_state(old.state, old.first_viewed_at),
                        "byYou": old.tech_id == device.tech_id,
                        "techFirstName": old.tech_first_name,
                    },
                )
        for old in earlier:
            await _end(ctx, old, "replaced", f"tech:{device.tech_id}", f"recorded again as {video_id}", drop_file=True,
                       event=("tech", device.tech_id, "video_replaced", {"by": video_id}))

    now = db.now()
    object_key = f"videos/{day}/{video_id}.mp4"
    override_flag = None
    if customer_id is not None and not device.demo and body.get("override"):
        wrong = [k for k, v in signals.items() if v == "mismatch"] or [k for k, v in signals.items() if v != "match"]
        override_flag = f"{device.tech_first_name} sent it past a warning. Did not agree: {', '.join(wrong)}."
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
            registered={"signals": signals, "override": bool(body.get("override")), "notOnMyList": not_on_list,
                        "bytes": size},
            override_flag=override_flag,
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
    if customer_id is not None and not device.demo:
        # The stop has a video on the way, so "finished with no video" no longer holds.
        await _clear_missing(
            ctx, day, customer_id, device.tech_id, appointment_ids, stop, VIDEO_ARRIVED, state="awaiting_upload"
        )
    return {"id": video_id, "state": "awaiting_upload", "upload": _upload(ctx, object_key)}


async def _insert_video(
    ctx: Ctx, values: dict[str, Any], *, registered: dict[str, Any], override_flag: str | None = None
) -> None:
    """Save a new video, its audit entry and, when the tech sent it past a warning, the
    office's flag: one save, so none of the three can be there without the others."""
    async with ctx.engine.begin() as conn:
        await conn.execute(sa.insert(db.videos).values(**values))
        await add_event(conn, "tech", values["tech_id"], "video_registered", video_id=values["id"],
                        customer_id=values["customer_id"], detail=registered)
        if override_flag:
            await add_flag(conn, "wrong_house", f"override:{values['id']}", day=values["day"],
                           tech_id=values["tech_id"], customer_id=values["customer_id"], video_id=values["id"],
                           detail=override_flag)


async def _registered(ctx: Ctx, video: Any) -> None:
    """What follows a registration and is not written with it: the stop has a video on
    the way, so its 'finished with no video' flag is cleared. Safe to run again, and run
    again on every repeat of the phone's call, so a first try that was cut off after the
    video was saved is finished by the second."""
    if video.customer_id is None or video.demo or video.state not in LIVE_STATES:
        return
    async with ctx.engine.connect() as conn:
        stop = (
            await conn.execute(
                sa.select(db.stops).where(db.stops.c.day == video.day, db.stops.c.customer_id == video.customer_id)
            )
        ).first()
    await _clear_missing(
        ctx, video.day, video.customer_id, video.tech_id, video.appointment_ids, stop, VIDEO_ARRIVED, state=video.state
    )


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
        elif video.state in ("held", "unmatched"):
            # A repeat, minutes after the first. The first try may have been cut off after
            # the video was confirmed and before its file was looked at, so look again.
            await _check_format(ctx, video, video.size_bytes)
        return describe(video)

    stored = await ctx.storage.head(video.object_key)
    claimed = to_int(body.get("sizeBytes")) or video.size_bytes
    if stored is None or stored.size <= 0 or stored.size != claimed or stored.size > ctx.cfg.max_upload_bytes:
        raise ApiError(409, "not_uploaded", "The video did not finish sending. It will try again.")

    settings = await get_settings(ctx)
    now = db.now()
    state = "unmatched" if video.not_on_list else "held"
    due = None if video.not_on_list else email_due(video.recorded_at, now, settings["hold_minutes"])
    # One save: the video is confirmed, the audit trail says so, the "stuck on the phone"
    # flag is closed, and the office is told when there is a customer to pick. A server
    # killed here leaves all of that or none of it; with none, the phone confirms again.
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video_id, db.videos.c.state == "awaiting_upload")
            .values(state=state, uploaded_at=now, size_bytes=stored.size, etag=stored.etag, email_due_at=due)
        )
        if result.rowcount:
            await add_event(conn, "tech", device.tech_id, "video_uploaded", video_id=video_id,
                            customer_id=video.customer_id, detail={"bytes": stored.size})
            await close_flags(conn, db.flags.c.dedupe == f"stuck:{video_id}", "system", "it arrived")
            if video.not_on_list and not video.demo:
                said = f' He said: "{video.spoken_text[:200]}"' if video.spoken_text else ""
                await add_flag(
                    conn, "unmatched", f"unmatched:{video_id}", day=video.day, tech_id=video.tech_id,
                    video_id=video_id, detail=f"{video.tech_first_name} filmed a stop that was not on the list.{said}",
                )
    if result.rowcount:
        await _check_format(ctx, video, stored.size)
    fresh = await get_video(ctx, video_id)
    return describe(fresh)


async def delete_by_tech(ctx: Ctx, device: Device, video_id: str) -> dict[str, Any]:
    video = await get_video(ctx, video_id) if UUID_RE.match(video_id) else None
    if video is None or video.tech_id != device.tech_id:
        raise ApiError(404, "bad_request", "We do not know this video.")
    if video.state in ("deleted", "replaced"):
        return {"id": video_id, "state": video.state}
    if video.state not in ("awaiting_upload", "held", "unmatched"):
        raise ApiError(409, "too_late", _locked_words(video))
    if video.state == "unmatched":
        # Not on the list: no email is waiting to close the window, so the same ten
        # minutes are counted here. After that it is the office's to match or pull.
        settings = await get_settings(ctx)
        closes = email_due(video.recorded_at, video.uploaded_at or video.created_at, settings["hold_minutes"])
        if db.now() > closes:
            raise ApiError(409, "too_late", "The time to delete this video has passed. Call the office to change it.")
    if not await _end(
        ctx, video, "deleted", f"tech:{device.tech_id}", "deleted by the tech", drop_file=True,
        event=("tech", device.tech_id, "video_deleted", None), clear=("system", "the tech deleted the video"),
    ):
        raise ApiError(409, "too_late", "This video already went to the customer. Call the office to change it.")
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


FILE_OK, FILE_PULLED, FILE_UNSURE = "ok", "pulled", "unsure"


async def file_check(ctx: Ctx, video: Any) -> str:
    """Is the bucket still holding the file this video was confirmed with?

    The phone's upload link stays valid for a day so that a slow upload can finish,
    which means the same link could put different bytes over a video after the server
    confirmed it. Nothing a customer is sent or shown may be anything but the confirmed
    file, so before an email goes and before the player is handed a link, the object is
    compared with what `complete` recorded.

    Pulling takes a video away from its customer and cannot be undone by itself. So a
    video is pulled only on an answer that can only be the bucket's own:

    FILE_PULLED: the bucket said in its own words that it has no such file (`storage.ask`),
        or it answered with a file that has another ETag than the confirmed one. The
        video is pulled and the office is told.
    FILE_OK: it is the confirmed file (the same ETag). Also the answer when the bucket
        cannot be reached or has an error: that proves nothing, and the video is let through.
    FILE_UNSURE: something answered, but not in a way that can only be the bucket's. A
        bare "not found", a page of words where a file should be, an answer with no ETag
        on it. That can be whatever stands in front of the bucket having a bad minute,
        with the file safe behind it. Nothing is pulled on that."""
    if not video.etag:
        return FILE_OK
    try:
        stored = await ctx.storage.head(video.object_key)
    except Exception:  # noqa: BLE001 - any storage failure means "cannot tell"
        return FILE_OK
    if stored is None:
        # A HEAD's "not found" carries no word from the bucket itself. Ask again in a way
        # that does, and believe "not there" only when the bucket says so.
        ask = getattr(ctx.storage, "ask", None)
        if ask is not None:
            try:
                answer = await ask(video.object_key)
            except Exception:  # noqa: BLE001 - `ask` should never raise; if it does, nothing is known
                answer = UNKNOWN
            if answer != MISSING:
                return FILE_UNSURE
        what = "is missing from storage"
    else:
        if stored.etag and stored.etag == plain_etag(video.etag):
            return FILE_OK  # the same file, whatever else the answer says about it
        if not stored.etag or words_not_a_file(getattr(stored, "content_type", "")):
            # No ETag to compare, or the answer is a page of words (one that says "we
            # will be right back"): this is not the bucket speaking about our file.
            return FILE_UNSURE
        what = "was changed after it was uploaded"
    name = video.customer_name or "a stop not on the list"
    await _end(
        ctx, video, "pulled", "system", f"the file {what}", drop_file=False,
        event=("system", "storage", "video_pulled", {"reason": f"the file {what}"}),
        flag=None if video.demo else (
            "file_changed", f"file:{video.id}",
            f"{video.tech_first_name}'s video for {name}: the file {what}. It was pulled and its link is dead.",
        ),
    )
    await clear_flags(ctx, db.flags.c.dedupe == f"{FILE_DOUBT}:{video.id}", "system", "the storage answered")
    return FILE_PULLED


async def file_unchanged(ctx: Ctx, video: Any) -> bool:
    """May this video be played? Not when its file was changed or is gone for certain
    (it is pulled then). On an answer that settles nothing it plays: if the file really
    is missing the player fails, which takes nothing away from anyone."""
    return await file_check(ctx, video) != FILE_PULLED


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
        # after the lease instead of two passes sending at once. The lease runs from this
        # moment, not from when the pass began: in a slow pass (FieldRoutes timing out on
        # one video after another) a lease stamped with the pass's start would already
        # be over when it is taken, and a second server would take the same video.
        at = db.now()
        async with ctx.engine.begin() as conn:
            claimed = await conn.execute(
                sa.update(db.videos)
                .where(db.videos.c.id == video.id, db.videos.c.state == "held", db.videos.c.email_due_at <= at)
                .values(email_due_at=at + EMAIL_RETRY)
            )
        if claimed.rowcount and await _deliver(ctx, video, known):
            sent += 1
    return sent


async def _finish(
    ctx: Ctx, video: Any, state: str, action: str, detail: dict[str, Any], *,
    flag: tuple[str, str, str] | None = None, link: str = "", show_customer: bool = False, **values: Any,
) -> bool:
    """Write down what was decided for a held video, and everything that follows from it,
    in one save: the audit entry (`action`, `detail`), a flag for the office (kind, key,
    words), and with `link` the FieldRoutes note and the follow-up task (`_follow_up`).

    One save, so a server killed at any moment leaves all of it or none of it. Never a
    video marked sent with no note queued, or "no email" with no flag. With none of it the
    video is still held and the next pass decides again; an email that had already been
    handed over is then offered a second time under the same key, and the mail service
    takes it only once. False when another pass settled this video first."""
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos).where(db.videos.c.id == video.id, db.videos.c.state == "held").values(state=state, **values)
        )
        if not result.rowcount:
            return False
        await add_event(conn, "system", "email", action, video_id=video.id, customer_id=video.customer_id, detail=detail)
        if flag is not None:
            kind, dedupe, words = flag
            await add_flag(conn, kind, dedupe, day=video.day, tech_id=video.tech_id, customer_id=video.customer_id,
                           video_id=video.id, detail=words)
        if link:
            await _follow_up(conn, ctx, video, link, show_customer=show_customer)
    return True


def _ago(late: timedelta) -> str:
    """How long, the way a person says it: "35 minutes", "7 hours", "2 days"."""
    minutes = max(1, round(late.total_seconds() / 60))
    if minutes < 90:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = round(minutes / 60)
    return f"{hours} hours" if hours < 48 else f"{round(hours / 24)} days"


async def _due_since(ctx: Ctx, video: Any, settings: dict[str, Any]) -> datetime:
    """When this video's email was first due. Not `email_due_at`: that moves on with every
    try. It is the end of the tech's ten minutes, or later than that the last time a
    person at the office put the video back in line (Send it anyway, or a move to another
    customer).

    Only a person's say-so starts the count again. The route letting a video go does
    not: a video that waited for the route and is found waiting again after a backup was
    put back must not look "on time" because the route lets it go a second time."""
    due = email_due(video.recorded_at, video.uploaded_at or video.created_at, settings["hold_minutes"])
    async with ctx.engine.connect() as conn:
        again = (
            await conn.execute(
                sa.select(sa.func.max(db.events.c.at)).where(
                    db.events.c.video_id == video.id, db.events.c.action.in_(PLACED_BY_OFFICE)
                )
            )
        ).scalar_one_or_none()
    return max(due, again) if again is not None else due


async def _overdue(ctx: Ctx, video: Any, settings: dict[str, Any]) -> timedelta | None:
    """How late this video's email is, when that is more than the owner allows
    (`overdue_hours`; 0 switches the rule off). None when it may go.

    Such an email is not sent by itself. It became this late because the server or
    FieldRoutes was down for hours, or because a database backup was put back, which
    brings videos back as "waiting" that were emailed, or pulled, days ago. Sent now it
    would arrive at whatever hour the server happened to come back, and after a restore
    it would be a second email, or a video the office had pulled."""
    limit = timedelta(hours=to_int(settings.get("overdue_hours")) or 0)
    if limit <= timedelta(0):
        return None
    emails_on = ctx.cfg.email_mode != "off" and ctx.mailer.delivers
    if not emails_on and not ctx.cfg.fr_writes:
        # Customer emails are off and so are FieldRoutes writes: nothing can go out late
        # and nothing can be written twice. The video is closed as "emails are off", for
        # good. This is the state GO-LIVE.md asks for before a backup is put back.
        return None
    now = db.now()
    first_due = email_due(video.recorded_at, video.uploaded_at or video.created_at, settings["hold_minutes"])
    if now - first_due <= limit:
        return None  # on time by the plain reckoning: nothing more to look up
    late = now - await _due_since(ctx, video, settings)
    return late if late > limit else None


async def _deliver(ctx: Ctx, video: Any, known: dict[int, dict[str, Any]] | None = None) -> bool:
    """Decide whether this video's customer gets an email, send it, and file the link.
    `known` is this pass's customer records when they were fetched together."""
    cfg = ctx.cfg
    settings = await get_settings(ctx)
    link = watch_url(ctx, video.token)
    off_route = False  # set below when the video and the route do not agree

    def against_route() -> dict[str, bool]:
        # Noted on the audit entry, so that the five-minute round can tell the office if
        # this server is stopped before it has (`jobs.watch_route`).
        return {"off_route": True} if off_route else {}

    async def no_email(reason: str, *, flag: str = "", show_customer: bool = False) -> bool:
        if not await _finish(
            ctx, video, "no_email", "email_skipped", {"reason": reason, **against_route()}, email_skip=reason,
            flag=("no_email", f"no_email:{video.id}", flag) if flag else None,
            link="" if video.demo else link, show_customer=show_customer,
        ):
            return False
        if off_route:
            await review_route(ctx, [video.id])
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
            await _end(ctx, video, "replaced", "system", f"another video for this visit stands ({rival.id})",
                       drop_file=True,
                       event=("system", "email", "video_replaced", {"by": rival.id, "reason": "duplicate for the visit"}))
            return False

    async def against_the_saved_route() -> tuple[Any, list[Any], set[str]]:
        """The stop as it was last saved, the other techs' videos for it, and which of
        all these videos the office itself placed. Reads nothing from FieldRoutes."""
        async with ctx.engine.connect() as conn:
            saved = (
                await conn.execute(
                    sa.select(db.stops).where(db.stops.c.day == video.day, db.stops.c.customer_id == video.customer_id)
                )
            ).first()
            theirs = (
                await conn.execute(
                    sa.select(db.videos).where(
                        db.videos.c.day == video.day,
                        db.videos.c.customer_id == video.customer_id,
                        db.videos.c.tech_id != video.tech_id,
                        db.videos.c.state.in_(LIVE_STATES),
                    )
                )
            ).all()
        return saved, list(theirs), await placed_by_office(ctx, [video.id, *(o.id for o in theirs)])

    # An email that is hours late does not go by itself: it waits for the office. The
    # flag says how late, and also what the saved route says about the visit, so that
    # nobody presses Send for a visit that has since left the day without being told.
    late = await _overdue(ctx, video, settings)
    if late is not None:
        stop, others, by_office = await against_the_saved_route()
        where, rival = ("ok", None) if video.id in by_office else standing(video, stop, others, by_office)
        what = _late_what(where, rival)
        await _finish(
            ctx, video, "no_email", "email_skipped", {"reason": OVERDUE, "hours_late": round(late.total_seconds() / 3600, 1)},
            email_skip=OVERDUE,
            flag=(OVERDUE, f"{OFF_ROUTE}:{video.id}:{what}",
                  _overdue_words(ctx, video, late, stop=stop, where=where, rival=rival)),
        )
        return False

    checked = await file_check(ctx, video)
    if checked == FILE_PULLED:
        return False
    if checked == FILE_UNSURE:
        # Something answered for the file, but not in a way that can only be the bucket's.
        # Nothing is pulled on that. The email waits all the same: a link to a file that
        # may not be there cannot be taken back. The lease brings the video back in a few
        # minutes, and the office is told if that goes on.
        waited = db.now() - await _due_since(ctx, video, settings)
        if waited >= DOUBT_FLAG_AFTER:
            name = video.customer_name or "a stop not on the list"
            await raise_flag(
                ctx, FILE_DOUBT, f"{FILE_DOUBT}:{video.id}", day=video.day, tech_id=video.tech_id,
                customer_id=video.customer_id, video_id=video.id,
                detail=f"{video.tech_first_name}'s video for {name}: for {_ago(waited)} the storage has not given a "
                "clear answer about its file. It said \"not found\", or answered with something that is not the "
                "file, and neither proves the file is gone. Nothing was pulled and the customer has not been "
                "emailed. Press watch: if it plays, the storage is having trouble and the email goes by itself "
                "when it answers. If it does not play, pull the video.",
            )
        return False
    if db.now() - video.created_at >= DOUBT_FLAG_AFTER:
        # Old enough to have been waiting on the storage: that question is settled now.
        await clear_flags(ctx, db.flags.c.dedupe == f"{FILE_DOUBT}:{video.id}", "system", "the storage answered")

    # Server's own time check, now that FieldRoutes has had time to record the check-in.
    route_known = True
    try:
        await sync_day(ctx, video.day)
    except FieldRoutesError:
        route_known = False  # the saved list is old, and FieldRoutes did not give a new one
    stop, others, by_office = await against_the_saved_route()
    view = tech_view(stop, video.tech_id) if stop is not None else None

    # The route may have changed since he filmed. Two kinds of video are not emailed by
    # themselves; each is kept, the office is told, and the office sends it or pulls it:
    # - one for a visit FieldRoutes no longer has on that day (cancelled, deleted, a
    #   no-show, moved to another day);
    # - a second video for one visit, when the stop changed hands and both techs filmed it.
    # If what held it back ends, `review_route` lets it go. A stop that only changed hands
    # still goes out, with a flag. A video the office itself put on its customer is not
    # second-guessed, and the owner can switch the hold off (the flag is raised all the same).
    placed = video.id in by_office
    hold = settings["hold_off_route"] and not placed
    if hold and not route_known:
        # Nobody can say right now whether the visit is still on the day: the read failed,
        # or one failed in the last minute and FieldRoutes is being left alone. An email
        # for a cancelled visit cannot be taken back, so this one waits, the same as when
        # the customer's address cannot be read. The lease brings it back in a few minutes.
        return False
    where, rival = standing(video, stop, others, by_office)
    off_route = (where != "ok" or rival is not None) and not placed
    if hold and (where == "gone" or rival is not None):
        skip = OFF_ROUTE if where == "gone" else SECOND
        if await _finish(
            ctx, video, "no_email", "email_skipped",
            {"reason": skip, **({"other": rival.id} if rival is not None else {})}, email_skip=skip,
        ):
            # The office's flag for it. A server killed before this line loses nothing:
            # the five-minute round looks at every held-back video and raises it then.
            await review_route(ctx, [video.id])
        return False
    mine_now = view.appointment_ids if view is not None else []
    if where == "ok" and mine_now and mine_now != list(video.appointment_ids or []):
        # One of his appointments there was cancelled, or one was added, since he filmed:
        # tie the video to the ones he has now, so a score lands on a live appointment.
        async with ctx.engine.begin() as conn:
            await conn.execute(sa.update(db.videos).where(db.videos.c.id == video.id).values(appointment_ids=mine_now))

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
        # One email per video, per link, per inbox. The inbox is part of the key so that
        # "I already have one under this key" can only ever be about this same inbox: an
        # email that went to the test inbox must not stand for the customer's.
        idempotency_key=f"video-{video.id}-{video.token[:8]}-{security.digest(to)[:8]}",
        tag="video",
    )

    async def failed_before() -> int:
        async with ctx.engine.connect() as conn:
            return (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(db.events)
                    .where(db.events.c.video_id == video.id, db.events.c.action == "email_failed")
                )
            ).scalar_one()

    already = False
    try:
        email_id = await ctx.mailer.send(email)
    except AlreadyTaken as exc:
        # The mail service holds an email for this video and this inbox already, worded
        # differently from this one. What that means depends on what this server knows:
        if await failed_before():
            # An earlier try was written down as failed. Whether that one reached the
            # customer after all, nobody here can say, and the mail service will take
            # nothing more under this key. So no more tries, the record says no email is
            # known to have gone, and the office is asked to look.
            await audit(ctx, "system", "email", "email_failed", video_id=video.id, customer_id=video.customer_id,
                        detail={"error": str(exc)[:300]})
            await no_email(
                "send_failed",
                flag="The mail service says it already has an email for this video, and this server has no record "
                "of one going out: its earlier tries were written down as failed. The customer may or may not have "
                "it. Ask the customer, or look in the mail service's list of sent emails. The link is saved on the "
                "account as a note.",
            )
            return False
        # No try of ours ever failed. Then the email was handed over once, and the server
        # was stopped before it could write that down; the wording changed before this
        # try (the owner edited the subject line, a service was added to the visit). The
        # customer has the email, so this is "sent". Written down as a failure, six tries
        # later the record would say "no email" and invite someone to send a second one.
        email_id, already = "", True
    except DeliveryError as exc:
        tries = await failed_before()
        await audit(ctx, "system", "email", "email_failed", video_id=video.id, customer_id=video.customer_id,
                    detail={"error": str(exc)[:300]})
        if tries + 1 >= EMAIL_MAX_ATTEMPTS:
            # Stop hammering; the office decides. The link itself is live.
            await no_email("send_failed", flag=f"The email could not be sent after {EMAIL_MAX_ATTEMPTS} tries: {exc}")
        return False

    if not await _finish(
        ctx, video, "sent", "email_sent",
        {"to": security.mask_email(to), "mode": cfg.email_mode, **against_route(),
         **({"already_with_the_mail_service": True} if already else {})},
        link=link,
        emailed_at=db.now(), email_to=to, email_id=email_id, email_skip="",
    ):
        return False
    if off_route:
        # Told at the moment it goes, not on the next round. (A server killed before this
        # line: the audit entry says the video went out against the route, and
        # `jobs.watch_route` looks once more at every such video of the last hour.)
        await review_route(ctx, [video.id])
    return True


def _late_what(where: str, rival: Any) -> str:
    """The last part of the key of an overdue video's flag: that its email was late, and
    what the route says on top of that. A flag is told once for each thing it says, so
    when the route changes under a waiting video the office gets a new flag for it."""
    if where == "gone":
        return f"{OVERDUE}:gone"
    if rival is not None:
        return f"{OVERDUE}:second:{rival.id[:8]}"
    return OVERDUE


def _overdue_words(
    ctx: Ctx, video: Any, late: timedelta, *, stop: Any = None, where: str = "ok", rival: Any = None
) -> str:
    """What the office reads on the flag of a video held because its email was long overdue.

    Every sentence has to be true whatever made it late. After a backup was put back the
    server no longer knows that a video was emailed days ago, so it never says that
    nobody was emailed: it says it has no record of an email, and where to look."""
    name = video.customer_name or "a stop not on the list"
    where_at = f", {video.address}" if video.address else ""
    emails_on = ctx.cfg.email_mode != "off" and ctx.mailer.delivers
    words = (
        f"{video.tech_first_name}'s video for {name}{where_at}. Its email was {_ago(late)} late, so it was not sent "
        "by itself. That happens when the server, FieldRoutes or the storage was down, or when a backup was put back. "
    )
    if where == "gone":
        if stop is None or stop.gone:
            words += "FieldRoutes no longer has that visit on this day (cancelled, a no-show, or moved to another day). "
        else:
            words += (
                f"FieldRoutes no longer has work for {video.tech_first_name} at that stop on this day "
                "(cancelled, moved, or given to another tech). "
            )
    elif rival is not None:
        other = rival.tech_first_name or "another tech"
        has = "the customer already has it" if rival.state == "sent" else "it is on file"
        words += f"{other} filmed the same visit too, and {has}. "
    if emails_on:
        return words + (
            "This server has no record of an email for it. If a backup was put back, the customer may already have "
            "it: look for the video note on the customer's account in FieldRoutes before you send. "
            "Press Send it anyway, or Pull."
        )
    # Customer emails are off: letting it go saves the link on the account and emails nobody.
    return words + (
        "This server has no record of an email or a saved link for it. If a backup was put back, the link may "
        "already be on the customer's account in FieldRoutes: look before you keep it. Press Keep it, or Pull."
    )


def _join_words(items: list[str]) -> str:
    """["Lawn Care", "Pest Control"] -> "Lawn Care and Pest Control"."""
    items = [i for i in items if i]
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


async def _clear_missing(
    ctx: Ctx, day: str, customer_id: int, tech_id: int, appointment_ids: Any, stop: Any, note: str, *, state: str
) -> None:
    """A video is on the way, so 'finished with no video' no longer holds for this tech at
    this stop, nor for anyone it stands in for: a helper's video stands in for whoever
    leads the stop (see `route.covers`). `state` is the video's."""
    keys = [missing_key(day, customer_id, tech_id)]
    if stop is not None:
        keys += [
            missing_key(day, customer_id, lead)
            for lead in lead_techs(stop)
            if lead != tech_id and covers(stop, lead, tech_id, appointment_ids, state)
        ]
    await clear_flags(ctx, db.flags.c.dedupe.in_(keys), "system", note)


# -- when the route changes under a video ---------------------------------------


def route_state(video: Any, stop: Any) -> str:
    """Where a video stands against the day as FieldRoutes has it now.

    'ok'          the tech still has work at the stop.
    'other_tech'  the work he filmed is still on the day, under another tech.
    'gone'        the work he filmed is not on that day any more: cancelled, deleted,
                  marked a no-show, or moved to another day."""
    if stop is None or stop.gone:
        return "gone"
    if video.tech_id in (stop.tech_ids or []):
        return "ok"
    live = {a.get("id") for a in (stop.appointments or []) if isinstance(a, dict)}
    return "other_tech" if live.intersection(video.appointment_ids or []) else "gone"


async def placed_by_office(ctx: Ctx, video_ids: Iterable[str]) -> set[str]:
    """Which of these videos the office itself put on their customer (matched, moved, or
    said 'send it anyway'). The route has no more say over those.

    One 'send it anyway' is not that: the one that answers a flag which only said the
    email was late (`_answers_only_lateness`). The office then said "send it, late as it
    is", and was never told the visit had left the day. Such a video is still checked
    against the route before it goes."""
    ids = sorted({i for i in video_ids if i})
    if not ids:
        return set()
    async with ctx.engine.connect() as conn:
        rows = (
            await conn.execute(
                sa.select(db.events.c.video_id, db.events.c.action, db.events.c.detail)
                .where(db.events.c.video_id.in_(ids), db.events.c.action.in_(PLACED_BY_OFFICE))
            )
        ).all()
    return {r.video_id for r in rows if not _answers_only_lateness(r)}


def _answers_only_lateness(event: Any) -> bool:
    """Was this 'send it anyway' pressed on a video held only for a late email, with no
    word on its flag about the route? (`send_anyway` writes down what was being answered.)"""
    detail = event.detail if isinstance(event.detail, dict) else {}
    return event.action == "video_send_anyway" and detail.get("held") == OVERDUE and not detail.get("route_told")


async def flag_again(ctx: Ctx, kind: str, dedupe: str, *, detail: str, **who: Any) -> bool:
    """Raise a flag, or bring back one the server cleared by itself for a reason that no
    longer holds: the office moved a stop away and then back again, or a video arrived
    and the tech then deleted it. A flag a person cleared stays cleared: they have seen
    it. True when a flag was raised or brought back."""
    if await raise_flag(ctx, kind, dedupe, detail=detail, **who):
        return True
    async with ctx.engine.begin() as conn:
        back = await conn.execute(
            sa.update(db.flags)
            .where(db.flags.c.dedupe == dedupe, db.flags.c.cleared_at.is_not(None))
            .where(db.flags.c.cleared_by == "system", db.flags.c.note.in_(CLEARED_FOR_NOW))
            .values(cleared_at=None, cleared_by="", note="", detail=detail[:1000], created_at=db.now())
        )
        if not back.rowcount:  # still open: keep its words true
            await conn.execute(
                sa.update(db.flags)
                .where(db.flags.c.dedupe == dedupe, db.flags.c.cleared_at.is_(None), db.flags.c.detail != detail[:1000])
                .values(detail=detail[:1000])
            )
    return bool(back.rowcount)


def standing(
    video: Any, stop: Any, others: Iterable[Any], by_office: Iterable[str] = ()
) -> tuple[str, Any | None]:
    """Where a video stands against the route (`route_state`'s answer), and the other
    tech's video that makes this one a second video for one visit, if there is one.

    That happens when the office moved the work from one tech to another and both of
    them filmed it. The customer gets one of the two by itself, never both:
    - the video of the tech who has the work now goes, and the earlier tech's waits;
    - unless the earlier tech's already went. Then the later one waits.
    Whether two videos are for one visit is `route.covers`' answer, the same one the
    phone and the flags use. A video the office itself sent or placed (`by_office`) is
    not counted against another: the office chose to have both."""
    where = route_state(video, stop)
    if stop is None or where == "gone":
        return where, None
    by_office = set(by_office)
    for other in others:
        if other.id == video.id or other.tech_id == video.tech_id or other.id in by_office:
            continue
        if other.day != video.day or other.customer_id != video.customer_id or other.state not in LIVE_STATES:
            continue
        if where == "other_tech":
            # This one would stand in for the tech who has the work now, and that tech has
            # filmed it himself. His is safe in the bucket and is not itself waiting.
            if (
                other.state in IN_BUCKET and not held_back(other) and route_state(other, stop) == "ok"
                and covers(stop, other.tech_id, video.tech_id, video.appointment_ids, "held")
            ):
                return where, other
        elif (
            went_out(other) and route_state(other, stop) == "other_tech"
            and covers(stop, video.tech_id, other.tech_id, other.appointment_ids, other.state)
        ):
            # The work is his now, but the video of the tech who had it before already went.
            return where, other
    return where, None


def _what(flag: Any) -> str:
    """The last part of a route flag's key: what it is about."""
    parts = str(flag.dedupe).split(":", 2)
    return parts[2] if len(parts) > 2 else ""


def short_day(day: str) -> str:
    try:
        return datetime.strptime(day, "%Y-%m-%d").strftime("%b %-d")
    except ValueError:
        return day


def _now_with(video: Any, stop: Any) -> int:
    """The tech who has the filmed work now: the tech on the appointment the video is tied to."""
    tied = set(video.appointment_ids or [])
    for a in (stop.appointments or []) if stop is not None else []:
        if isinstance(a, dict) and a.get("id") in tied:
            return to_int(a.get("tech")) or 0
    return 0


def _route_words(
    ctx: Ctx, video: Any, what: str, *, stop: Any, where: str, rival: Any, moved_to: str | None, names: dict[int, str]
) -> str:
    """What the office reads on a route flag. Plain words, and true at the moment of asking."""
    tech = video.tech_first_name
    who = f"{tech} filmed {video.customer_name}, {video.address}"
    emails_on = ctx.cfg.email_mode != "off" and ctx.mailer.delivers
    if emails_on:
        press = "No email went out. Press Send it anyway, or Pull."
    else:
        # Customer emails are off: letting it go saves the link on the account and emails nobody.
        press = "No email went out and nothing was saved on the account. Press Keep it, or Pull."
    if moved_to:
        gone = f"FieldRoutes has moved that visit to {short_day(moved_to)}"
    elif stop is None or stop.gone:
        gone = "FieldRoutes no longer has that visit on this day (cancelled, a no-show, or moved to another day)"
    else:
        gone = (
            f"FieldRoutes no longer has work for {tech} at that stop on this day "
            "(cancelled, moved, or given to another tech)"
        )
    other = (rival.tech_first_name if rival is not None else "") or "another tech"
    kind = what.split(":")[0]
    if kind == "held":
        return f"{who}, but {gone}. {press}"
    if kind == "back":
        return (
            f"{who}. It was held back, and that flag was cleared without sending or pulling the video. "
            f"What held it back is over, but nothing goes out until the office says so. {press}"
        )
    if kind == "second":
        if not held_back(video):  # the owner switched the hold off, so both went
            return f"{who}, and {other}'s video for the same visit went out too. Pull one if that is wrong."
        if where == "other_tech":
            return f"{who}, but FieldRoutes now has that stop under {other}, and {other} filmed it too. {press}"
        first = f"the customer already has {other}'s video" if rival.state == "sent" else f"{other}'s video is on file"
        return f"{who}, but {other} filmed the same visit before the stop was moved to {tech}, and {first}. {press}"
    if kind == "day":
        has = "the customer has the video" if video.state == "sent" else "its link is on file"
        return (
            f"{who} on {short_day(video.day)}, and {has}. FieldRoutes now has that same visit on "
            f"{short_day(moved_to or '')}, so the app asks for it to be filmed again. "
            "Tell the tech if the first video is enough."
        )
    if kind == "tech":
        lead = _now_with(video, stop)
        now_with = names.get(lead) or "another tech"
        if stop is not None and covers(stop, lead, video.tech_id, video.appointment_ids, video.state):
            return (
                f"{who}, but FieldRoutes now has that stop under {now_with}. {tech}'s video stands for the "
                f"visit, and {now_with} is not asked for a second one. Pull it if that is wrong."
            )
        return (
            f"{who}, but FieldRoutes now has that work under {now_with}. {now_with} has other work of his own "
            f"at that stop, so he is still asked for his own video."
        )
    if video.state == "sent":
        return f"{who}, and the customer has the video. {gone}. Pull it if the visit did not happen."
    return f"{who}, but {gone}. Pull it if the visit did not happen."


async def _file_flags(ctx: Ctx, flags: Iterable[Any], note: str) -> None:
    """Close route flags whose moment is over and put them out of the way.

    A flag's key is what stops the same flag being raised twice. Changing the key of a
    closed one is what lets the same thing, if it happens again later, raise a flag of
    its own, even when a person cleared the last one. Who cleared it stays on record."""
    for flag in flags:
        async with ctx.engine.begin() as conn:  # closed and put away in one save, never half
            await close_flags(conn, db.flags.c.id == flag.id, "system", note)
            await _put_away(conn, flag)


async def _put_away(conn: Any, flag: Any) -> None:
    """Change a closed flag's key so that it no longer stands in the way of a new one."""
    if FILED not in flag.dedupe:
        await conn.execute(
            sa.update(db.flags)
            .where(db.flags.c.id == flag.id, db.flags.c.dedupe == flag.dedupe)
            .values(dedupe=f"{flag.dedupe}{FILED}{flag.id}")
        )


async def review_route(
    ctx: Ctx, video_ids: Iterable[str], *, read_since: str | None = None, just_went: Iterable[str] = ()
) -> int:
    """Check videos against the route as the saved stops have it, and keep the office's
    flag for each one true. Reads nothing from FieldRoutes. The emailer calls it for one
    video the moment that video's email is decided; `jobs.watch_route` calls it for the
    recent ones on every round. Safe to run again.

    `read_since` is the oldest day FieldRoutes is still read for by itself. A video for a
    day before that, with no flag so far, was checked when its email went and its day's
    saved stops have not changed since. The one thing that can still happen to it is its
    visit turning up on a later day's list, so that is all that is looked for. `just_went`
    names videos that went out in the last hour against the route: those get the whole
    check whatever their day, in case the server was stopped between sending one and
    telling the office.

    One flag per video is open at most: the one that says what is true now. When the
    route changes again, or the video and the route agree again, that flag is closed and
    filed, so the next thing gets a flag of its own. A flag a person cleared stays
    cleared for the thing they saw, not for the video.

    A video held back for the office goes out by itself when what held it back is over
    (the visit is back on the day, the other tech's video is gone). Not when a person
    has cleared its flag, though: to them that read as dealt with, so it keeps waiting
    for Send or Pull and the office is told so.

    Returns how many flags were raised and videos let go."""
    fresh = {i for i in just_went if i}
    ids = sorted({i for i in video_ids if i})
    if not ids:
        return 0
    settings = await get_settings(ctx)
    async with ctx.engine.connect() as conn:
        rows = [
            v for v in (await conn.execute(sa.select(db.videos).where(db.videos.c.id.in_(ids)))).all()
            if not v.demo and v.customer_id is not None
        ]
        if not rows:
            return 0
        customers = sorted({v.customer_id for v in rows})
        stop_rows = (
            await conn.execute(
                sa.select(db.stops).where(
                    db.stops.c.customer_id.in_(customers), db.stops.c.day >= min(v.day for v in rows)
                )
            )
        ).all()
        video_rows = (
            await conn.execute(
                sa.select(db.videos).where(
                    db.videos.c.customer_id.in_(customers),
                    db.videos.c.day.in_(sorted({v.day for v in rows})),
                    db.videos.c.state.in_(LIVE_STATES),
                )
            )
        ).all()
        flag_rows = (
            await conn.execute(
                sa.select(db.flags).where(db.flags.c.kind.in_(ROUTE_FLAGS), db.flags.c.video_id.in_([v.id for v in rows]))
            )
        ).all()
        names = {t.id: t.fname for t in (await conn.execute(sa.select(db.techs.c.id, db.techs.c.fname))).all()}
    placed = await placed_by_office(ctx, [*(v.id for v in rows), *(v.id for v in video_rows)])
    stops = {(s.day, s.customer_id): s for s in stop_rows}
    current: dict[str, list[Any]] = {}
    for flag in flag_rows:
        if FILED not in flag.dedupe:
            current.setdefault(flag.video_id, []).append(flag)

    changed = 0
    for video in rows:
        mine = current.get(video.id, [])
        if video.state not in LIVE_STATES:  # pulled, deleted or replaced: nothing left to decide
            await _file_flags(ctx, mine, "the video is gone")
            continue
        if video.state not in ("sent", "no_email"):
            continue  # its email is not decided yet; the emailer looks at the route when it is
        stop = stops.get((video.day, video.customer_id))
        where, rival, moved_to = "ok", None, None
        if video.id not in placed:  # the office put it there itself: the route has no more say
            if read_since is None or video.day >= read_since or mine or held_back(video) or video.id in fresh:
                where, rival = standing(video, stop, video_rows, placed)
            # The same appointment on a later day's list: the office moved the visit there.
            tied = set(video.appointment_ids or [])
            later = [
                s.day for s in stop_rows
                if s.customer_id == video.customer_id and s.day > video.day and not s.gone
                and tied.intersection(a.get("id") for a in (s.appointments or []) if isinstance(a, dict))
            ]
            moved_to = max(later) if later else None

        what = ""
        late: timedelta | None = None
        if held_back(video) and video.email_skip == OVERDUE:
            # Held because its email was hours late. Nothing on the route ends that hold:
            # only the office does, with Send it anyway or Pull. What the route says is
            # put on its flag, so the office knows it before pressing either.
            what = _late_what(where, rival)
        elif held_back(video):
            if where == "gone" or rival is not None:
                what = "held" if where == "gone" else f"second:{rival.id[:8]}"
                skip = OFF_ROUTE if where == "gone" else SECOND
                if video.email_skip != skip:  # held back for one reason, still held for the other
                    async with ctx.engine.begin() as conn:
                        await conn.execute(
                            sa.update(db.videos)
                            .where(db.videos.c.id == video.id, db.videos.c.state == "no_email")
                            .where(db.videos.c.email_skip.in_(ROUTE_HELD))
                            .values(email_skip=skip)
                        )
            elif (late := await _overdue(ctx, video, settings)) is not None:
                # What held it back is over, but by now its email is hours late. The route
                # does not let such a video go: it could be 3 a.m., and after a backup was
                # put back it could be a second email, or a video the office had pulled.
                # From here on it waits as any late video does.
                if not await _hold_as_late(ctx, video, late):
                    continue  # something else settled it in this very moment
                what = OVERDUE
            elif any(_what(f) == "back" or (f.cleared_at is not None and f.cleared_by != "system") for f in mine):
                what = "back"  # a person cleared its flag: it does not go by itself any more
            else:
                # What held it back is over and nobody has touched it: the email goes on the next pass.
                if await release(ctx, video):
                    changed += 1
                await _file_flags(ctx, mine, BACK_ON_ROUTE)
                continue
        elif moved_to:
            what = f"day:{moved_to}"
        elif where == "gone":
            what = "gone"
        elif where == "other_tech":
            what = f"tech:{_now_with(video, stop)}"
        elif rival is not None:
            what = f"second:{rival.id[:8]}"  # both went out: the owner has the hold switched off
        if not what:
            await _file_flags(ctx, mine, BACK_ON_ROUTE)
            continue

        key = f"{OFF_ROUTE}:{video.id}:{what}"
        await _file_flags(ctx, [f for f in mine if f.dedupe != key], ROUTE_CHANGED_AGAIN)
        same = next((f for f in mine if f.dedupe == key), None)
        if what.split(":")[0] == OVERDUE:
            # Told once for each thing it says. Its words carry how late the email was at
            # that moment, so they are not written again on every round.
            if same is None:
                if late is None:
                    late = db.now() - await _due_since(ctx, video, settings)
                detail = _overdue_words(ctx, video, late, stop=stop, where=where, rival=rival)
                if await raise_flag(ctx, OVERDUE, key, day=video.day, tech_id=video.tech_id,
                                    customer_id=video.customer_id, video_id=video.id, detail=detail):
                    changed += 1
            continue
        detail = _route_words(ctx, video, what, stop=stop, where=where, rival=rival, moved_to=moved_to, names=names)
        if same is None:
            kind = {"second": SECOND, "back": STILL_WAITING}.get(what.split(":")[0], OFF_ROUTE)
            if await raise_flag(ctx, kind, key, day=video.day, tech_id=video.tech_id, customer_id=video.customer_id,
                                video_id=video.id, detail=detail):
                changed += 1
        elif same.cleared_at is None and same.detail != detail[:1000]:
            async with ctx.engine.begin() as conn:  # still open: keep its words true
                await conn.execute(sa.update(db.flags).where(db.flags.c.id == same.id).values(detail=detail[:1000]))
    return changed


async def release(ctx: Ctx, video: Any) -> bool:
    """A video that was held back goes ahead: what held it back is over (the stop is back
    on the day, or the other video for the visit is gone). It is emailed on the next pass,
    once. Only a hold the route made can end this way; one for an overdue email cannot.
    `review_route` calls this only while the email is not yet hours late."""
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video.id, db.videos.c.state == "no_email", db.videos.c.email_skip.in_(ROUTE_HELD))
            .values(state="held", email_due_at=db.now(), email_skip="")
        )
        if result.rowcount:
            await add_event(conn, "system", "route", "video_released", video_id=video.id, customer_id=video.customer_id)
    return bool(result.rowcount)


async def _hold_as_late(ctx: Ctx, video: Any, late: timedelta) -> bool:
    """A video the route was holding back keeps waiting, now because its email is hours
    late. The audit entry is written with the change. Its flag is `review_route`'s to
    raise; a server stopped in between raises it on its next round."""
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video.id, db.videos.c.state == "no_email", db.videos.c.email_skip.in_(ROUTE_HELD))
            .values(email_skip=OVERDUE)
        )
        if result.rowcount:
            await add_event(conn, "system", "route", "email_skipped", video_id=video.id, customer_id=video.customer_id,
                            detail={"reason": OVERDUE, "hours_late": round(late.total_seconds() / 3600, 1),
                                    "was": video.email_skip})
    return bool(result.rowcount)


async def send_anyway(ctx: Ctx, video_id: str, by: str) -> None:
    """The office looked at a video that was held back (its stop left the day, it is a
    second video for one visit, or its email was long overdue) and wants the customer to
    have it. It goes on the next pass, like any other."""
    video = await get_video(ctx, video_id) if UUID_RE.match(video_id) else None
    if video is None:
        raise ApiError(404, "bad_request", "No such video.")
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.videos)
            .where(db.videos.c.id == video_id, db.videos.c.state == "no_email", db.videos.c.email_skip.in_(HELD_BACK))
            .values(state="held", email_due_at=db.now(), email_skip="")
        )
        if result.rowcount:
            # Its flags are answered, in the same save. They are put away too, so that if
            # this video ever has to wait for the office again, that gets a flag of its own.
            mine = sa.and_(db.flags.c.kind.in_(ROUTE_FLAGS), db.flags.c.video_id == video_id)
            flags = (await conn.execute(sa.select(db.flags.c.id, db.flags.c.dedupe).where(mine))).all()
            # Written with the change itself: the emailer reads this entry as the office's
            # say-so, and must never see the video ready to go without it. It also says
            # what the office was answering. A flag that only said "the email was late" is
            # no word on the route, so the route is still checked before such a video goes
            # (`placed_by_office`); one that said the visit had left the day is.
            told = any(FILED not in f.dedupe and _what(f).startswith(f"{OVERDUE}:") for f in flags)
            await add_event(conn, "office", by, "video_send_anyway", video_id=video_id, customer_id=video.customer_id,
                            detail={"held": video.email_skip, "route_told": told})
            await close_flags(conn, mine, by, "sent anyway")
            for flag in flags:
                await _put_away(conn, flag)
    if not result.rowcount:
        raise ApiError(409, "bad_request", "That video is not waiting to be sent.")


async def _follow_up(conn: Any, ctx: Ctx, video: Any, link: str, *, show_customer: bool) -> None:
    """Queue the link for the customer's FieldRoutes account, and open the follow-up when
    the tech said he recommended something. Written inside the save that settles the video
    (`conn`), so the link is never left unfiled for a video that went out."""
    if video.customer_id is None or video.customer_id <= 0:
        return
    await _queue_fr(
        conn, "note", f"note:{video.id}:{video.token[:8]}", video.customer_id, video.id,
        {"text": f"Service video from {video.tech_first_name}, {video.day}: {link}", "day": video.day,
         "show_customer": show_customer},
    )
    if video.recommend:
        await _queue_fr(
            conn, "task", f"task:{video.id}", video.customer_id, video.id,
            {"text": f"{video.tech_first_name} recommended something in the service video. Watch it and follow up: {link}",
             "due": ctx.today()},
        )
        await add_flag(conn, "recommend", f"recommend:{video.id}", day=video.day, tech_id=video.tech_id,
                       customer_id=video.customer_id, video_id=video.id,
                       detail=f"{video.tech_first_name} recommended something. Watch the video and follow up.")


async def _queue_fr(
    conn: Any, kind: str, dedupe: str, customer_id: int, video_id: str | None, payload: dict[str, Any]
) -> bool:
    """Put one FieldRoutes write in the outbox, inside a save that is already open. A write
    that is already queued under the same key is left alone: a repeat must not write twice."""
    now = db.now()
    return await db.insert_once(
        conn, db.fr_outbox,
        dict(dedupe=dedupe, kind=kind, customer_id=customer_id, video_id=video_id, payload=payload,
             attempts=0, next_try_at=now, last_error="", created_at=now),
    )


async def enqueue_fr(ctx: Ctx, kind: str, dedupe: str, customer_id: int, video_id: str | None, payload: dict[str, Any]) -> None:
    async with ctx.engine.begin() as conn:
        await _queue_fr(conn, kind, dedupe, customer_id, video_id, payload)


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
        await add_event(conn, "customer", "link", "video_played", video_id=video.id, customer_id=video.customer_id)


async def get_score(ctx: Ctx, video_id: str) -> Any | None:
    async with ctx.engine.connect() as conn:
        return (await conn.execute(sa.select(db.scores).where(db.scores.c.video_id == video_id))).first()


async def save_score(ctx: Ctx, video: Any, stars: int, comment: str) -> None:
    if not 1 <= stars <= 5:
        raise ApiError(400, "bad_request", "Pick 1 to 5 stars.")
    comment = comment.replace("\x00", "").strip()[:1000]
    now = db.now()
    real = not video.demo and video.customer_id is not None
    appointment_id = await _review_appointment(ctx, video) if real else None
    for attempt in (1, 2):
        try:
            # One save: the score, its audit entry, the review for FieldRoutes and the
            # office's flag for a low score. A server killed here leaves all of it or none
            # of it, and with none the customer's page still asks for the score.
            async with ctx.engine.begin() as conn:
                updated = await conn.execute(
                    sa.update(db.scores).where(db.scores.c.video_id == video.id).values(stars=stars, comment=comment)
                )
                first = updated.rowcount == 0
                if first:
                    await conn.execute(
                        sa.insert(db.scores).values(video_id=video.id, stars=stars, comment=comment, created_at=now)
                    )
                await add_event(conn, "customer", "link", "score_saved", video_id=video.id,
                                customer_id=video.customer_id, detail={"stars": stars, "first": first})
                if real and appointment_id:
                    # FieldRoutes takes one review per save: the first score is the one that
                    # is sent, and a changed score stays here. The key makes that so. It is
                    # tried on every save and not only the first, so a review that was not
                    # queued the first time (no appointment to file it on yet) still is.
                    await _queue_fr(
                        conn, "review", f"review:{video.id}", video.customer_id, video.id,
                        {"appointment_id": appointment_id, "stars": stars, "feedback": comment, "at": now.isoformat()},
                    )
                if real and stars <= 3:
                    said = f': "{comment[:300]}"' if comment else "."
                    await add_flag(conn, "low_score", f"low_score:{video.id}", day=video.day, tech_id=video.tech_id,
                                   customer_id=video.customer_id, video_id=video.id, detail=f"{stars} of 5 stars{said}")
            break
        except sa.exc.IntegrityError:
            # Two Sends in the same instant (a double tap on a slow link): the other one
            # inserted first. Go round once more; this time the row is there to update, so
            # the customer sees the thank-you and FieldRoutes still gets one review.
            if attempt == 2:
                raise


async def _review_appointment(ctx: Ctx, video: Any) -> int | None:
    """The appointment a score is filed against in FieldRoutes: one of the video's own that
    is still on the stop. One the office cancelled after the email went is passed over."""
    ids = [i for i in (video.appointment_ids or []) if i]
    async with ctx.engine.connect() as conn:
        stop = (
            await conn.execute(
                sa.select(db.stops).where(db.stops.c.day == video.day, db.stops.c.customer_id == video.customer_id)
            )
        ).first()
    if stop is not None and not stop.gone:
        live = [a.get("id") for a in (stop.appointments or []) if isinstance(a, dict)]
        still = [i for i in ids if i in live]
        if not still and video.tech_id in (stop.tech_ids or []):
            still = tech_view(stop, video.tech_id).appointment_ids
        if still:
            return still[0]
    return ids[0] if ids else None


async def not_my_home(ctx: Ctx, video: Any) -> None:
    await _end(
        ctx, video, "pulled", "customer", "customer said: not my home", drop_file=False,
        event=("customer", "link", "not_my_home", None),
        flag=(
            "not_my_home", f"not_my_home:{video.id}",
            f"The person who opened the link for {video.customer_name} said it is not their home. "
            "The link is dead. Move the video to the right customer.",
        ),
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
    # The audit entry is saved with the opt-out: it is the list the owner reads before a
    # database restore, to know which customers to put back.
    async with ctx.engine.begin() as conn:
        if await db.insert_once(conn, db.email_optouts, dict(customer_id=video.customer_id, at=db.now())):
            await add_event(conn, "customer", "link", "email_opt_out", video_id=video.id, customer_id=video.customer_id)


# -- office: pull, move -------------------------------------------------------


async def pull(ctx: Ctx, video_id: str, by: str, reason: str) -> None:
    video = await get_video(ctx, video_id) if UUID_RE.match(video_id) else None
    if video is None:
        raise ApiError(404, "bad_request", "No such video.")
    if not await _end(ctx, video, "pulled", by, reason or "pulled by the office", drop_file=False,
                      event=("office", by, "video_pulled", {"reason": reason})):
        raise ApiError(409, "bad_request", "That video is already gone.")


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
        # Written with the move itself: the emailer reads this entry as the office's own
        # choice of customer (see `placed_by_office`), and must never see one without the other.
        await add_event(conn, "office", by, "video_moved", video_id=video_id, customer_id=customer_id,
                        detail={"from": video.customer_id, "to": customer_id}, at=now)
        # The video's flags were about where it was. They are answered by the move, in the
        # same save, and put away so that the new place can raise its own.
        flags = (
            await conn.execute(sa.select(db.flags.c.id, db.flags.c.dedupe, db.flags.c.kind).where(db.flags.c.video_id == video_id))
        ).all()
        await close_flags(conn, db.flags.c.video_id == video_id, by, f"moved to customer {customer_id}")
        for flag in flags:
            if flag.kind in ROUTE_FLAGS:
                await _put_away(conn, flag)
    await _clear_missing(
        ctx, video.day, customer_id, video.tech_id, snapshot["appointment_ids"], stop, VIDEO_MOVED_HERE, state="held"
    )


# -- FieldRoutes outbox --------------------------------------------------------

OUTBOX_BACKOFF = [1, 5, 15, 60, 180, 360, 720, 1440]  # minutes
OUTBOX_CLAIM = timedelta(minutes=10)  # how long one server's claim on a row keeps every other server off it
WRITING = "writing"  # a row's `last_error` from the moment it is claimed until its write has an answer


async def run_outbox(ctx: Ctx) -> int:
    """Make the FieldRoutes writes that are waiting: notes, tasks and reviews. Once a minute.

    Nothing may be written twice. Two things could do that, and each has its own guard:

    - Two servers. At a deploy the new server is up before the old one is stopped, and for
      a moment both run this job on the same rows. So a row is claimed first, with an
      update that only one of them can win, and the loser leaves the row alone.
    - A server stopped in the middle of a write. FieldRoutes may have taken the write, or
      not; nobody can say. The claim marks the row as being written, and a row found still
      marked that way is not tried again. The office is told to look instead."""
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
        # Both the claim and "was this row cut off?" are decided by the row as it is in the
        # database at this moment, never by the copy read at the top of the pass, and the
        # claim runs from this moment, not from when the pass began. In a slow pass
        # (FieldRoutes timing out on row after row) the top of the pass can be more than a
        # claim's length ago: a claim stamped with that time would be over as it is made,
        # and the old copy would not show that another server has the row right now.
        at = db.now()
        waiting = sa.and_(
            db.fr_outbox.c.id == row.id, db.fr_outbox.c.done_at.is_(None), db.fr_outbox.c.failed_at.is_(None),
            db.fr_outbox.c.next_try_at <= at,
        )
        flag = ""
        async with ctx.engine.begin() as conn:
            claimed = await conn.execute(
                sa.update(db.fr_outbox)
                .where(waiting, db.fr_outbox.c.last_error != WRITING)
                .values(next_try_at=at + OUTBOX_CLAIM, last_error=WRITING)
            )
            if not claimed.rowcount:
                # Not ours to write. Either another server has it, or it is still marked
                # as being written although that claim has run out: the server that held
                # it was stopped in the middle. FieldRoutes may have taken the write or
                # not, so it is not tried again. Given up and flagged in one save, and
                # only one server can win this update too.
                cut_off = await conn.execute(
                    sa.update(db.fr_outbox)
                    .where(waiting, db.fr_outbox.c.last_error == WRITING)
                    .values(failed_at=at, last_error="stopped in the middle of the write; not tried again")
                )
                if cut_off.rowcount:
                    await add_flag(
                        conn, "fr_write_failed", f"fr:{row.id}", day=ctx.today(), customer_id=row.customer_id,
                        video_id=row.video_id,
                        detail=f"The server was stopped while it was saving this {row.kind} to FieldRoutes, so it "
                        "may or may not be there. It was not tried again, so that nothing is written twice. Look at "
                        f"the customer's account in FieldRoutes. If it is missing, add it by hand: {_by_hand(row)}",
                    )
        if not claimed.rowcount:
            continue
        p = row.payload
        values: dict[str, Any]
        try:
            if row.kind == "note":
                await ctx.fr.create_note(row.customer_id, p["text"], day=p["day"], show_customer=bool(p.get("show_customer")))
            elif row.kind == "review":
                await ctx.fr.create_review(row.customer_id, int(p["appointment_id"]), int(p["stars"]),
                                           p.get("feedback", ""), datetime.fromisoformat(p["at"]))
            elif row.kind == "task":
                await ctx.fr.create_task(row.customer_id, p["text"], due=p["due"])
            values = {"done_at": db.now(), "last_error": ""}
            done += 1
        except WriteBlocked as exc:
            values = {"done_at": db.now(), "last_error": f"skipped: {exc}"}
        except FieldRoutesError as exc:
            # The row itself is asked how often it has been tried: it may have been tried
            # by another server since this pass read it.
            async with ctx.engine.connect() as conn:
                tried = (
                    await conn.execute(sa.select(db.fr_outbox.c.attempts).where(db.fr_outbox.c.id == row.id))
                ).scalar_one_or_none()
            attempts = (tried if tried is not None else row.attempts) + 1
            if attempts >= len(OUTBOX_BACKOFF):
                values = {"attempts": attempts, "failed_at": db.now(), "last_error": str(exc)[:500]}
                flag = f"Could not save the {row.kind} to FieldRoutes: {exc}"
            else:
                values = {
                    "attempts": attempts,
                    "next_try_at": db.now() + timedelta(minutes=OUTBOX_BACKOFF[attempts]),
                    "last_error": str(exc)[:500],
                }
        # The answer and the office's flag, when there is one, in one save.
        async with ctx.engine.begin() as conn:
            await conn.execute(sa.update(db.fr_outbox).where(db.fr_outbox.c.id == row.id).values(**values))
            if flag:
                await add_flag(conn, "fr_write_failed", f"fr:{row.id}", day=ctx.today(), customer_id=row.customer_id,
                               video_id=row.video_id, detail=flag)
    return done


def _by_hand(row: Any) -> str:
    """What an outbox row would have written, for an office person to type in."""
    p = row.payload if isinstance(row.payload, dict) else {}
    if row.kind == "review":
        return f"{p.get('stars')} stars, \"{p.get('feedback', '')}\""[:400]
    return str(p.get("text", ""))[:400]
