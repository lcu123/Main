"""Shared context plus the three small things every other module leans on:
owner-editable settings, the audit trail, and office flags.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from . import db
from .config import Config
from .fr import FieldRoutes
from .notify import Mailer
from .security import RateLimiter
from .storage import Storage


class ApiError(Exception):
    """An error with a plain-words message meant for the person on the other end."""

    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra


# Owner-editable settings and their defaults. The brief's open decisions land here so
# changing one is a form on the office page, not a redeploy.
DEFAULT_SETTINGS: dict[str, Any] = {
    "max_seconds": 15,
    "hold_minutes": 10,
    "far_meters": 150,
    "min_build": 1,
    "require_video": True,
    "recommend_button": True,
    # Off for version one: the leave-a-stop reminder needs "Always" location, which is the
    # permission App Review questions most. The owner can turn it on once the app is approved.
    "leave_reminders": False,
    "missing_alert_minutes": 60,
    "stuck_hours": 3,
    "retention_months": 12,
    "skip_service_ids": [],  # service types that do not need a video (e.g. weekly mowing)
    "respect_fr_email_reminders": True,
    # A video for a visit FieldRoutes no longer has on that day (cancelled, a no-show, or
    # moved to another day) waits for the office instead of emailing the customer by itself.
    # So does a second video for one visit, when a stop changed hands and both techs filmed it.
    "hold_off_route": True,
    # A video whose email was due more than this many hours ago is not emailed by itself: it
    # waits for the office (Send it anyway, or Pull). That is what happens after the server
    # or FieldRoutes was down for hours, or after a database backup was put back, when the
    # late emails would otherwise all go out at once, whatever the hour, and some of them
    # for a second time. 0 switches the rule off.
    "overdue_hours": 6,
    # Which email carries the video link. 0 is our own email, as it always was. 30 or 60:
    # FieldRoutes' own after-service email carries it, and the office has FieldRoutes set to
    # send that email this many minutes after a visit is completed. The server then adds
    # one line with the link to the technician's comments on the completed appointment
    # (the email prints them where its template says {{techComments}}) and sends no email
    # of its own. A video that cannot make it into that email waits for the office.
    "fr_email_minutes": 0,
    "fr_email_line": "Watch the video of today's visit: {link}",
    "email_subject": "Your service video from {tech}",
    "email_intro": "{tech} just finished at {address} and recorded a short video for you: "
    "what was done and what is recommended.",
    "prompts": ["Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate"],
}

SETTING_LIMITS: dict[str, tuple[int, int]] = {
    "max_seconds": (5, 60),
    "hold_minutes": (0, 120),
    "far_meters": (50, 2000),
    "min_build": (1, 100000),
    "missing_alert_minutes": (10, 720),
    "stuck_hours": (1, 48),
    "retention_months": (1, 60),
    "overdue_hours": (0, 168),
}

# Settings that take one of a few values, not a range.
SETTING_CHOICES: dict[str, tuple[int, ...]] = {"fr_email_minutes": (0, 30, 60)}

# The one margin of the "link in FieldRoutes' email" setting, in minutes. The line has to
# be in FieldRoutes this long before FieldRoutes' email is due, or it is not written.
FR_EMAIL_MARGIN_MINUTES = 5
# Once the line is in, the server looks at the visit one more time this many minutes
# before that last moment, and adds the line again if it has gone (see `videos`).
FR_EMAIL_LOOK_MINUTES = 2
FR_EMAIL_LINE_MAX = 200


def longest_hold(fr_email_minutes: int) -> int:
    """The longest tech's hold that still fits inside FieldRoutes' delay. The video is let
    go when the hold ends; after that the server needs the margin, and as much again for
    its own rounds (it reads the route every five minutes and writes once a minute)."""
    return fr_email_minutes - 2 * FR_EMAIL_MARGIN_MINUTES


def fr_email_line(settings: dict[str, Any], link: str) -> str:
    """The one line that goes into the technician's comments, with the link in it."""
    words = str(settings.get("fr_email_line") or "")
    if words.count("{link}") != 1:  # a saved value always has it; this is for one that was never checked
        words = DEFAULT_SETTINGS["fr_email_line"]
    return words.replace("{link}", link)


@dataclass
class Ctx:
    cfg: Config
    engine: AsyncEngine
    fr: FieldRoutes
    storage: Storage
    mailer: Mailer
    texter: Any
    pusher: Any
    limiter: RateLimiter = field(default_factory=RateLimiter)
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_sync: dict[str, float] = field(default_factory=dict)  # day -> monotonic time
    # Monotonic clocks count from boot, so "never" must be -inf, not 0: on a machine that
    # booted a minute ago, `monotonic() - 0` looks like a sync that just happened.
    last_tech_sync: float = float("-inf")
    _settings: tuple[float, dict[str, Any]] | None = None

    def today(self) -> str:
        return datetime.now(self.cfg.tz).date().isoformat()

    def local(self, when: datetime) -> datetime:
        return when.astimezone(self.cfg.tz)

    def local_date(self, when: datetime) -> date:
        return self.local(when).date()


# -- settings -----------------------------------------------------------------


async def get_settings(ctx: Ctx) -> dict[str, Any]:
    if ctx._settings and time.monotonic() - ctx._settings[0] < 30:
        return ctx._settings[1]
    async with ctx.engine.connect() as conn:
        rows = (await conn.execute(sa.select(db.settings.c.key, db.settings.c.value))).all()
    merged = dict(DEFAULT_SETTINGS)
    for key, value in rows:
        if key in DEFAULT_SETTINGS and value is not None:
            merged[key] = value
    ctx._settings = (time.monotonic(), merged)
    return merged


def coerce_setting(key: str, raw: Any) -> Any:
    """Turn a form value into the right type, or raise ValueError with plain words."""
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        return raw in (True, "1", "on", "true", "yes")
    if isinstance(default, int):
        try:
            value = int(str(raw).strip())
        except ValueError as exc:
            raise ValueError(f"{key} must be a whole number") from exc
        if key in SETTING_CHOICES:
            if value not in SETTING_CHOICES[key]:
                # The one such setting today is which email carries the video link.
                raise ValueError(
                    f"{key} must be {' or '.join(str(c) for c in SETTING_CHOICES[key])}: our own email (0), or "
                    "FieldRoutes' own email 30 or 60 minutes after a visit."
                )
            return value
        low, high = SETTING_LIMITS.get(key, (0, 10**9))
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high}")
        return value
    if key == "skip_service_ids":
        items = raw if isinstance(raw, list) else str(raw).replace(",", " ").split()
        try:
            return sorted({int(str(x).strip()) for x in items if str(x).strip()})
        except ValueError as exc:
            raise ValueError("service IDs must be numbers") from exc
    if key == "prompts":
        lines = raw if isinstance(raw, list) else str(raw).splitlines()
        prompts = [str(p).replace("\x00", "").strip()[:40] for p in lines if str(p).replace("\x00", "").strip()]
        if not 1 <= len(prompts) <= 6:
            raise ValueError("give between 1 and 6 prompts, one per line")
        return prompts
    text = str(raw).replace("\x00", "").strip()
    if not text:
        raise ValueError(f"{key} cannot be empty")
    if key == "fr_email_line":
        # It is printed inside FieldRoutes' email, in the middle of the technician's own words.
        if "\n" in text or "\r" in text:
            raise ValueError("The line for FieldRoutes' email must be one line.")
        if len(text) > FR_EMAIL_LINE_MAX:
            raise ValueError(f"The line for FieldRoutes' email can be {FR_EMAIL_LINE_MAX} characters at most.")
        if text.count("{link}") != 1:
            raise ValueError("The line for FieldRoutes' email must have {link} in it, once: that is where the link goes.")
        return text
    return text[:500]


FR_EMAIL_TRIO = ("hold_minutes", "fr_email_minutes", "overdue_hours")


def fr_email_problem(settings: dict[str, Any], changing: str = "") -> str:
    """Why these settings cannot stand together, in one plain sentence. "" when they can.

    With the video link going into FieldRoutes' email (`fr_email_minutes` 30 or 60), two
    other settings have to fit it:
    - the tech's hold must end early enough for the line to be written before FieldRoutes
      sends (`longest_hold`), or every video would be let go too late;
    - "Overdue hours" must not be 0. A video then waits for its visit to be completed in
      FieldRoutes, and that number is the only limit on the wait. With no limit a visit
      that is never completed would keep a video waiting, and FieldRoutes being read
      for it, without end.
    `changing` is the one setting being saved, so the sentence can speak about that one."""
    hold, minutes, overdue = (int(settings[key]) for key in FR_EMAIL_TRIO)
    if minutes == 0:
        return ""
    most = longest_hold(minutes)
    if hold > most:
        if changing == "hold_minutes":
            return (
                f"The tech's hold cannot be {hold} minutes while the video link goes into FieldRoutes' email "
                f"{minutes} minutes after a visit: {most} minutes is the most."
            )
        if changing == "fr_email_minutes":
            return (
                f"FieldRoutes' email cannot carry the video link at {minutes} minutes while the tech's hold is "
                f"{hold} minutes: make the hold {most} minutes or less first."
            )
        return f"A hold of {hold} minutes does not fit inside FieldRoutes' {minutes} minutes: {most} minutes is the most."
    if overdue == 0:
        if changing == "fr_email_minutes":
            return (
                "FieldRoutes' email cannot carry the video link while Overdue hours is 0: a video waits for its "
                "visit to be completed in FieldRoutes, and that number is the only limit on the wait. Set it to 1 "
                "or more first."
            )
        return (
            "Overdue hours cannot be 0 while the video link goes into FieldRoutes' email: a video waits for its "
            "visit to be completed in FieldRoutes, and this number is the only limit on that wait."
        )
    return ""


async def set_setting(ctx: Ctx, key: str, raw: Any, by: str, *, alongside: dict[str, Any] | None = None) -> None:
    """Save one setting. `alongside` holds other settings that are being saved in the same
    go (the office page saves a whole form), so two that depend on each other are judged
    as the pair they will be."""
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"unknown setting {key}")
    value = coerce_setting(key, raw)
    settings = await get_settings(ctx)
    current = settings.get(key)
    if current == value:
        return
    if key in FR_EMAIL_TRIO:
        problem = fr_email_problem({**settings, **(alongside or {}), key: value}, changing=key)
        if problem:
            raise ValueError(problem)
    now = db.now()
    async with ctx.engine.begin() as conn:
        updated = await conn.execute(
            sa.update(db.settings)
            .where(db.settings.c.key == key)
            .values(value=value, updated_at=now, updated_by=by)
        )
        if updated.rowcount == 0:
            await conn.execute(sa.insert(db.settings).values(key=key, value=value, updated_at=now, updated_by=by))
    ctx._settings = None
    await audit(ctx, "office", by, "setting_changed", detail={"key": key, "from": current, "to": value})


# -- audit --------------------------------------------------------------------


async def add_event(
    conn: Any,
    actor_type: str,
    actor: str | int,
    action: str,
    *,
    video_id: str | None = None,
    customer_id: int | None = None,
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> None:
    """One audit entry, written inside a save that is already open (`conn`). An entry that
    belongs to a change is written with the change, so a server killed between the two can
    never leave the one without the other."""
    await conn.execute(
        sa.insert(db.events).values(
            at=at or db.now(),
            actor_type=actor_type,
            actor=str(actor),
            action=action,
            video_id=video_id,
            customer_id=customer_id,
            detail=detail,
        )
    )


async def audit(
    ctx: Ctx,
    actor_type: str,
    actor: str | int,
    action: str,
    *,
    video_id: str | None = None,
    customer_id: int | None = None,
    detail: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> None:
    async with ctx.engine.begin() as conn:
        await add_event(conn, actor_type, actor, action, video_id=video_id, customer_id=customer_id, detail=detail, at=at)


# -- flags --------------------------------------------------------------------

FLAG_WORDS = {
    "no_video": "Stop finished with no video",
    "file_changed": "Video file changed or missing",
    "bad_format": "Video may not play for every customer",
    "stuck_upload": "Video stuck on the phone",
    "wrong_house": "May be the wrong house",
    "not_my_home": "Customer said: not my home",
    "unmatched": "Not on the list: needs a customer",
    "no_email": "No email went out",
    "off_route": "Video for a stop that is no longer on the route",
    "second_video": "Two videos for one visit",
    "video_waiting": "Video still waiting for the office",
    "overdue": "Video held: its email was long overdue",
    "fr_missed": "Video not in FieldRoutes' email: the office decides",
    "fr_changed": "A visit changed in FieldRoutes when a video link was added: check it",
    "file_doubt": "Cannot tell whether a video's file is there",
    "email_failed": "Email could not be sent",
    "email_bounced": "Email bounced",
    "low_score": "Low score",
    "recommend": "Tech recommended something",
    "fr_write_failed": "FieldRoutes save failed",
}


async def add_flag(
    conn: Any,
    kind: str,
    dedupe: str,
    *,
    day: str,
    tech_id: int | None = None,
    customer_id: int | None = None,
    video_id: str | None = None,
    detail: str = "",
) -> bool:
    """Raise a flag once, inside a save that is already open (`conn`). False when the
    same flag already exists (cleared or not)."""
    return await db.insert_once(
        conn,
        db.flags,
        dict(
            dedupe=dedupe,
            kind=kind,
            day=day,
            tech_id=tech_id,
            customer_id=customer_id,
            video_id=video_id,
            detail=detail[:1000],
            created_at=db.now(),
            cleared_by="",
            note="",
        ),
    )


async def raise_flag(
    ctx: Ctx,
    kind: str,
    dedupe: str,
    *,
    day: str,
    tech_id: int | None = None,
    customer_id: int | None = None,
    video_id: str | None = None,
    detail: str = "",
) -> bool:
    """Raise a flag once. Returns False when the same flag already exists (cleared or not)."""
    async with ctx.engine.begin() as conn:
        return await add_flag(
            conn, kind, dedupe, day=day, tech_id=tech_id, customer_id=customer_id, video_id=video_id, detail=detail
        )


async def close_flags(conn: Any, where: Any, by: str, note: str = "") -> int:
    """Clear open flags inside a save that is already open (`conn`)."""
    values: dict[str, Any] = {"cleared_at": db.now(), "cleared_by": by}
    if note:
        values["note"] = note[:1000]
    result = await conn.execute(sa.update(db.flags).where(db.flags.c.cleared_at.is_(None)).where(where).values(**values))
    return result.rowcount or 0


async def clear_flags(ctx: Ctx, where: Any, by: str, note: str = "") -> int:
    async with ctx.engine.begin() as conn:
        return await close_flags(conn, where, by, note)
