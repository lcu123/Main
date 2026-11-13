"""Login with no passwords anywhere.

Techs: type the phone or email on their FieldRoutes record, get a 6-digit
code by text and email, stay signed in on that phone for good.
Office: an emailed code for addresses on the allow-list, then a session cookie.

A code works once, dies after 15 minutes and locks after 5 wrong tries.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from . import db, security
from .core import ApiError, Ctx, audit
from .fr import FieldRoutesError
from .notify import DeliveryError, Email
from .render import render

DEMO_TECH_ID = -1
OFFICE_SESSION_DAYS = 14
TECH_RESYNC_SECONDS = 300


@dataclass
class Device:
    id: str
    tech_id: int
    tech_first_name: str
    demo: bool
    apns_token: str
    apns_sandbox: bool


# -- techs --------------------------------------------------------------------


async def sync_techs(ctx: Ctx) -> int:
    """Mirror FieldRoutes' active technicians. Anyone no longer on that list goes
    inactive here, which locks their app on its next call."""
    listed = await ctx.fr.technicians()
    now = db.now()
    ids = [t["id"] for t in listed]
    async with ctx.engine.begin() as conn:
        known = {r[0] for r in (await conn.execute(sa.select(db.techs.c.id))).all()}
        for t in listed:
            values = dict(
                fname=t["fname"], lname=t["lname"], email=t["email"], phone=t["phone"], active=True, synced_at=now
            )
            if t["id"] in known:
                await conn.execute(sa.update(db.techs).where(db.techs.c.id == t["id"]).values(**values))
            else:
                await conn.execute(sa.insert(db.techs).values(id=t["id"], demo=False, **values))
        gone = sa.update(db.techs).where(db.techs.c.demo.is_(False)).where(db.techs.c.active.is_(True))
        if ids:
            gone = gone.where(db.techs.c.id.notin_(ids))
        await conn.execute(gone.values(active=False, synced_at=now))
    ctx.last_tech_sync = time.monotonic()
    return len(listed)


async def _find_tech(ctx: Ctx, kind: str, value: str) -> Any | None:
    column = db.techs.c.email if kind == "email" else db.techs.c.phone
    query = sa.select(db.techs).where(column == value, db.techs.c.active.is_(True), db.techs.c.demo.is_(False))
    async with ctx.engine.connect() as conn:
        row = (await conn.execute(query)).first()
    if row is None and time.monotonic() - ctx.last_tech_sync > TECH_RESYNC_SECONDS:
        # A tech added in FieldRoutes this morning must be able to log in without the office.
        try:
            await sync_techs(ctx)
        except FieldRoutesError:
            return None
        async with ctx.engine.connect() as conn:
            row = (await conn.execute(query)).first()
    return row


def _is_demo(ctx: Ctx, value: str) -> bool:
    """The App Review account. Its contact may be a made-up mobile number (so the reviewer
    walks the same text-message path a tech does) or an email."""
    if not ctx.cfg.demo_enabled:
        return False
    return value == security.normalize_contact(ctx.cfg.demo_contact)[1]


async def _new_code(ctx: Ctx, purpose: str, subject: str, issued_by: str | None = None) -> str:
    code = security.new_code()
    now = db.now()
    async with ctx.engine.begin() as conn:
        # Only the newest code counts; asking again retires the old one.
        await conn.execute(
            sa.update(db.login_codes)
            .where(db.login_codes.c.purpose == purpose, db.login_codes.c.subject == subject)
            .where(db.login_codes.c.used_at.is_(None))
            .values(used_at=now)
        )
        await conn.execute(
            sa.insert(db.login_codes).values(
                purpose=purpose,
                subject=subject,
                code_hash=security.code_digest(subject, code),
                created_at=now,
                expires_at=now + timedelta(minutes=security.CODE_MINUTES),
                tries=0,
                issued_by=issued_by,
            )
        )
    return code


async def _check_code(ctx: Ctx, purpose: str, subject: str, code: str) -> None:
    """Raise ApiError unless `code` is the live code for this subject. Marks it used."""
    now = db.now()
    # The wrong-try count must be committed before we raise: an exception inside the
    # transaction would roll the count back and the 5-try lock would never trip.
    failure: ApiError | None = None
    async with ctx.engine.begin() as conn:
        row = (
            await conn.execute(
                sa.select(db.login_codes)
                .where(db.login_codes.c.purpose == purpose, db.login_codes.c.subject == subject)
                .where(db.login_codes.c.used_at.is_(None), db.login_codes.c.expires_at > now)
                .order_by(db.login_codes.c.id.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            failure = ApiError(401, "bad_code", "That code is not right or it ran out. Ask for a new code.", triesLeft=0)
        elif row.tries >= security.CODE_MAX_TRIES:
            failure = ApiError(423, "locked", "Too many wrong tries. Ask for a new code.")
        elif not security.same(row.code_hash, security.code_digest(subject, code)):
            # Count in SQL, not from the value we read, so two guesses at once both count.
            await conn.execute(
                sa.update(db.login_codes)
                .where(db.login_codes.c.id == row.id)
                .values(tries=db.login_codes.c.tries + 1)
            )
            left = security.CODE_MAX_TRIES - (row.tries + 1)
            if left <= 0:
                failure = ApiError(423, "locked", "Too many wrong tries. Ask for a new code.")
            else:
                failure = ApiError(401, "bad_code", "That code is not right. Try again.", triesLeft=left)
        else:
            claimed = await conn.execute(
                sa.update(db.login_codes)
                .where(db.login_codes.c.id == row.id, db.login_codes.c.used_at.is_(None))
                .values(used_at=now)
            )
            if not claimed.rowcount:  # two phones raced for the same code: only one wins
                failure = ApiError(401, "bad_code", "That code was already used. Ask for a new code.", triesLeft=0)
    if failure is not None:
        raise failure


async def request_tech_code(ctx: Ctx, contact: str, ip: str) -> None:
    kind, value = security.normalize_contact(contact)
    if not kind:
        raise ApiError(400, "bad_request", "Type your mobile number or your email.")
    for key, limit in ((f"code-ip:{ip}", 10), (f"code-to:{value}", 5)):
        wait = ctx.limiter.retry_after(key, limit, 900)
        if wait:
            raise ApiError(429, "slow_down", "Too many tries. Wait a few minutes.", retry_after=wait)
    if _is_demo(ctx, value):
        return
    tech = await _find_tech(ctx, kind, value)
    if tech is None:
        # Same answer as a match: the caller never learns who is on file.
        await audit(ctx, "system", "login", "code_request_unknown", detail={"kind": kind})
        return
    code = await _new_code(ctx, "tech", str(tech.id))
    delivered = await _deliver_code(ctx, code, email=tech.email, phone=tech.phone, first_name=tech.fname)
    await audit(ctx, "tech", tech.id, "code_requested", detail={"delivered": delivered})
    if not delivered:
        raise ApiError(503, "server_error", "We could not send a code right now. Try again in a minute.")


async def _deliver_code(ctx: Ctx, code: str, *, email: str, phone: str, first_name: str) -> list[str]:
    """Send to both the number and the email on file; either one arriving is enough."""
    delivered: list[str] = []
    brand = ctx.cfg.brand_name
    if phone and ctx.texter.configured:
        try:
            # The code leads the message so iOS offers it above the keyboard by itself.
            await ctx.texter.send(
                phone, f"{code} is your Zest Video code. It works once, for 15 minutes. Do not share it."
            )
            delivered.append("text")
        except DeliveryError:
            pass
    if email and ctx.mailer.configured:
        try:
            await ctx.mailer.send(
                Email(
                    to=email,
                    subject=f"{code} is your {brand} code",
                    html=render("email_code.html", code=code, first_name=first_name, brand=brand, base_url=ctx.cfg.base_url),
                    text=f"Hi {first_name},\n\nYour {brand} code is {code}\n\nIt works once, for 15 minutes.\n",
                    tag="login_code",
                )
            )
            delivered.append("email")
        except DeliveryError:
            pass
    return delivered


async def _ensure_demo_tech(ctx: Ctx) -> Any:
    async with ctx.engine.begin() as conn:
        row = (await conn.execute(sa.select(db.techs).where(db.techs.c.id == DEMO_TECH_ID))).first()
        if row is None:
            await conn.execute(
                sa.insert(db.techs).values(
                    id=DEMO_TECH_ID, fname="Demo", lname="Tech", email="", phone="", active=True, demo=True,
                    synced_at=db.now(),
                )
            )
            row = (await conn.execute(sa.select(db.techs).where(db.techs.c.id == DEMO_TECH_ID))).first()
    return row


async def verify_tech_code(ctx: Ctx, contact: str, code: str, info: dict[str, Any], ip: str) -> dict[str, Any]:
    kind, value = security.normalize_contact(contact)
    code = "".join(ch for ch in str(code) if ch.isdigit())
    wait = ctx.limiter.retry_after(f"verify-ip:{ip}", 30, 900)
    if wait:
        raise ApiError(429, "slow_down", "Too many tries. Wait a few minutes.", retry_after=wait)
    if not kind or len(code) != 6:
        raise ApiError(401, "bad_code", "That code is not right. Try again.", triesLeft=0)

    if _is_demo(ctx, value):
        # The App Review account: a fixed code, so it gets its own strict limit.
        if not ctx.limiter.allow("verify-demo", 20, 900) or not security.same(code, ctx.cfg.demo_code):
            raise ApiError(401, "bad_code", "That code is not right. Try again.", triesLeft=0)
        tech = await _ensure_demo_tech(ctx)
    else:
        tech = await _find_tech(ctx, kind, value)
        if tech is None:
            raise ApiError(401, "bad_code", "That code is not right. Try again.", triesLeft=0)
        await _check_code(ctx, "tech", str(tech.id), code)

    token = security.new_token()
    device_id = str(uuid.uuid4())
    now = db.now()
    async with ctx.engine.begin() as conn:
        await conn.execute(
            sa.insert(db.devices).values(
                id=device_id,
                tech_id=tech.id,
                token_hash=security.digest(token),
                name=str(info.get("deviceName") or "")[:120],
                model=str(info.get("deviceModel") or "")[:60],
                os_version=str(info.get("osVersion") or "")[:30],
                app_build=_as_int(info.get("appBuild")),
                apns_token="",
                apns_sandbox=False,
                created_at=now,
                last_seen_at=now,
            )
        )
    await audit(ctx, "tech", tech.id, "login", detail={"device": device_id, "model": str(info.get("deviceModel") or "")[:60]})
    return {"token": token, "tech": {"id": tech.id, "firstName": tech.fname, "demo": bool(tech.demo)}}


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


async def authenticate(ctx: Ctx, token: str, build: int = 0) -> Device:
    if not token:
        raise ApiError(401, "unauthorized", "Please sign in again.")
    async with ctx.engine.begin() as conn:
        row = (
            await conn.execute(
                sa.select(db.devices, db.techs.c.fname, db.techs.c.active, db.techs.c.demo)
                .join(db.techs, db.techs.c.id == db.devices.c.tech_id)
                .where(db.devices.c.token_hash == security.digest(token))
            )
        ).first()
        if row is None or row.revoked_at is not None or not row.active:
            raise ApiError(401, "unauthorized", "Please sign in again.")
        now = db.now()
        if now - row.last_seen_at > timedelta(minutes=5) or (build and build != row.app_build):
            await conn.execute(
                sa.update(db.devices)
                .where(db.devices.c.id == row.id)
                .values(last_seen_at=now, app_build=build or row.app_build)
            )
    return Device(
        id=row.id,
        tech_id=row.tech_id,
        tech_first_name=row.fname,
        demo=bool(row.demo),
        apns_token=row.apns_token,
        apns_sandbox=bool(row.apns_sandbox),
    )


async def revoke_device(ctx: Ctx, device_id: str, by: str) -> bool:
    async with ctx.engine.begin() as conn:
        result = await conn.execute(
            sa.update(db.devices)
            .where(db.devices.c.id == device_id, db.devices.c.revoked_at.is_(None))
            .values(revoked_at=db.now(), revoked_by=by)
        )
    return bool(result.rowcount)


async def issue_code_by_hand(ctx: Ctx, tech_id: int, by: str) -> str:
    """The office reads a code to a tech (new phone, no email handy). Same rules as any code."""
    async with ctx.engine.connect() as conn:
        tech = (await conn.execute(sa.select(db.techs).where(db.techs.c.id == tech_id))).first()
    if tech is None or not tech.active or tech.demo:
        raise ApiError(400, "bad_request", "That tech is not active in FieldRoutes.")
    code = await _new_code(ctx, "tech", str(tech.id), issued_by=by)
    await audit(ctx, "office", by, "code_issued", detail={"tech": tech_id})
    return code


# -- office -------------------------------------------------------------------


@dataclass
class OfficeUser:
    email: str
    role: str
    session: str

    @property
    def owner(self) -> bool:
        return self.role == "owner"


async def ensure_owners(ctx: Ctx) -> None:
    """Owners named in VIDEO_OWNER_EMAILS always exist and stay owners."""
    now = db.now()
    async with ctx.engine.begin() as conn:
        for email in ctx.cfg.owner_emails:
            row = (await conn.execute(sa.select(db.office_users).where(db.office_users.c.email == email))).first()
            if row is None:
                await conn.execute(
                    sa.insert(db.office_users).values(email=email, role="owner", active=True, added_at=now, added_by="env")
                )
            elif row.role != "owner" or not row.active:
                await conn.execute(
                    sa.update(db.office_users).where(db.office_users.c.email == email).values(role="owner", active=True)
                )


async def request_office_code(ctx: Ctx, email: str, ip: str) -> None:
    email = (email or "").replace("\x00", "").strip().lower()[:254]
    for key, limit in ((f"ocode-ip:{ip}", 10), (f"ocode-to:{email}", 5)):
        wait = ctx.limiter.retry_after(key, limit, 900)
        if wait:
            raise ApiError(429, "slow_down", "Too many tries. Wait a few minutes.", retry_after=wait)
    async with ctx.engine.connect() as conn:
        user = (
            await conn.execute(
                sa.select(db.office_users).where(db.office_users.c.email == email, db.office_users.c.active.is_(True))
            )
        ).first()
    if user is None:
        return  # same answer either way
    code = await _new_code(ctx, "office", email)
    try:
        await ctx.mailer.send(
            Email(
                to=email,
                subject=f"{code} is your {ctx.cfg.brand_name} office code",
                html=render("email_code.html", code=code, first_name="", brand=ctx.cfg.brand_name, base_url=ctx.cfg.base_url),
                text=f"Your {ctx.cfg.brand_name} office code is {code}\n\nIt works once, for 15 minutes.\n",
                tag="office_code",
            )
        )
    except DeliveryError as exc:
        raise ApiError(503, "server_error", f"We could not email a code: {exc}") from exc
    await audit(ctx, "office", email, "code_requested")


async def verify_office_code(ctx: Ctx, email: str, code: str, ip: str) -> str:
    email = (email or "").replace("\x00", "").strip().lower()[:254]
    code = "".join(ch for ch in str(code) if ch.isdigit())
    wait = ctx.limiter.retry_after(f"overify-ip:{ip}", 30, 900)
    if wait:
        raise ApiError(429, "slow_down", "Too many tries. Wait a few minutes.", retry_after=wait)
    await _check_code(ctx, "office", email, code)
    token = security.new_token()
    now = db.now()
    async with ctx.engine.begin() as conn:
        await conn.execute(
            sa.insert(db.office_sessions).values(
                token_hash=security.digest(token),
                email=email,
                created_at=now,
                expires_at=now + timedelta(days=OFFICE_SESSION_DAYS),
            )
        )
    await audit(ctx, "office", email, "login")
    return token


async def office_user(ctx: Ctx, session_token: str) -> OfficeUser | None:
    if not session_token:
        return None
    async with ctx.engine.connect() as conn:
        row = (
            await conn.execute(
                sa.select(db.office_users.c.email, db.office_users.c.role)
                .join(db.office_sessions, db.office_sessions.c.email == db.office_users.c.email)
                .where(db.office_sessions.c.token_hash == security.digest(session_token))
                .where(db.office_sessions.c.expires_at > db.now(), db.office_users.c.active.is_(True))
            )
        ).first()
    return OfficeUser(email=row.email, role=row.role, session=session_token) if row else None


async def office_logout(ctx: Ctx, session_token: str) -> None:
    async with ctx.engine.begin() as conn:
        await conn.execute(
            sa.delete(db.office_sessions).where(db.office_sessions.c.token_hash == security.digest(session_token))
        )


async def set_office_user(ctx: Ctx, email: str, role: str, active: bool, by: str) -> None:
    email = (email or "").replace("\x00", "").strip().lower()[:254]
    if "@" not in email or role not in ("owner", "staff"):
        raise ApiError(400, "bad_request", "Give a real email and a role.")
    if email in ctx.cfg.owner_emails and (role != "owner" or not active):
        raise ApiError(400, "bad_request", "That owner is set in the server settings and cannot be changed here.")
    if email == by and (role != "owner" or not active):
        raise ApiError(400, "bad_request", "You cannot remove yourself.")
    async with ctx.engine.begin() as conn:
        updated = await conn.execute(
            sa.update(db.office_users).where(db.office_users.c.email == email).values(role=role, active=active)
        )
        if updated.rowcount == 0:
            await conn.execute(
                sa.insert(db.office_users).values(email=email, role=role, active=active, added_at=db.now(), added_by=by)
            )
        if not active:
            await conn.execute(sa.delete(db.office_sessions).where(db.office_sessions.c.email == email))
    await audit(ctx, "office", by, "office_user_changed", detail={"email": email, "role": role, "active": active})
