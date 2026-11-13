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
from sqlalchemy.exc import IntegrityError
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
}


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
    return text[:500]


async def set_setting(ctx: Ctx, key: str, raw: Any, by: str) -> None:
    if key not in DEFAULT_SETTINGS:
        raise ValueError(f"unknown setting {key}")
    value = coerce_setting(key, raw)
    current = (await get_settings(ctx)).get(key)
    if current == value:
        return
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


# -- flags --------------------------------------------------------------------

FLAG_WORDS = {
    "no_video": "Stop finished with no video",
    "stuck_upload": "Video stuck on the phone",
    "wrong_house": "May be the wrong house",
    "not_my_home": "Customer said: not my home",
    "unmatched": "Not on the list: needs a customer",
    "no_email": "No email went out",
    "email_failed": "Email could not be sent",
    "email_bounced": "Email bounced",
    "low_score": "Low score",
    "recommend": "Tech recommended something",
    "fr_write_failed": "FieldRoutes save failed",
}


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
    try:
        async with ctx.engine.begin() as conn:
            await conn.execute(
                sa.insert(db.flags).values(
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
                )
            )
    except IntegrityError:
        return False
    return True


async def clear_flags(ctx: Ctx, where: Any, by: str, note: str = "") -> int:
    values: dict[str, Any] = {"cleared_at": db.now(), "cleared_by": by}
    if note:
        values["note"] = note[:1000]
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.flags).where(db.flags.c.cleared_at.is_(None)).where(where).values(**values)
        )
    return result.rowcount or 0
