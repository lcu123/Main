"""Login with no passwords anywhere.

Techs: type the phone or email on their FieldRoutes record, get a 6-digit
code by text and email, stay signed in on that phone for good.
Office: a code for people on the allow-list, then a session cookie. The code is
texted to the mobile number an owner set for that person (`office_phones`), or
emailed to their address. Both ways end in the same kind of code and the same session.

A code works once, dies after 15 minutes and locks after 5 wrong tries.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from . import db, security
from .config import hush
from .core import ApiError, Ctx, add_event, audit
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
            # `insert_once`: two servers run side by side for a moment at a deploy, and both
            # may meet a new hire in the same second. The second one updates him instead.
            if t["id"] in known or not await db.insert_once(conn, db.techs, dict(id=t["id"], demo=False, **values)):
                await conn.execute(sa.update(db.techs).where(db.techs.c.id == t["id"]).values(**values))
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


# `issued_by` on an office code that was asked for with a mobile number (`_check_code`).
TEXTED = "text"


async def _check_code(ctx: Ctx, purpose: str, subject: str, code: str, *, texted: bool | None = None) -> None:
    """Raise ApiError unless `code` is the live code for this subject. Marks it used.

    `texted` is for an office code, which has two code steps: True looks only at a code
    that was texted, False only at one that was emailed. Left out, any live code counts
    (a tech's, and a decoy). So a code is taken only at the code step of the way it was
    asked for, and neither step's answer says anything about the other way."""
    now = db.now()
    live = (
        sa.select(db.login_codes)
        .where(db.login_codes.c.purpose == purpose, db.login_codes.c.subject == subject)
        .where(db.login_codes.c.used_at.is_(None), db.login_codes.c.expires_at > now)
    )
    if texted is True:
        live = live.where(db.login_codes.c.issued_by == TEXTED)
    elif texted is False:
        live = live.where(sa.or_(db.login_codes.c.issued_by.is_(None), db.login_codes.c.issued_by != TEXTED))
    # The wrong-try count must be committed before we raise: an exception inside the
    # transaction would roll the count back and the 5-try lock would never trip.
    failure: ApiError | None = None
    async with ctx.engine.begin() as conn:
        row = (await conn.execute(live.order_by(db.login_codes.c.id.desc()).limit(1))).first()
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
    """Owners named in VIDEO_OWNER_EMAILS always exist and stay owners, and each has the
    mobile number VIDEO_OWNER_PHONES gives him."""
    now = db.now()
    async with ctx.engine.begin() as conn:
        for email in ctx.cfg.owner_emails:
            row = (await conn.execute(sa.select(db.office_users).where(db.office_users.c.email == email))).first()
            if row is None:
                await db.insert_once(
                    conn, db.office_users, dict(email=email, role="owner", active=True, added_at=now, added_by="env")
                )
            elif row.role != "owner" or not row.active:
                await conn.execute(
                    sa.update(db.office_users).where(db.office_users.c.email == email).values(role="owner", active=True)
                )
    await ensure_owner_phones(ctx)


async def ensure_owner_phones(ctx: Ctx) -> None:
    """Put each number from VIDEO_OWNER_PHONES on its owner. An owner cannot type his own
    number on the Users page before he can sign in, so this is where his comes from.

    Run at every start, and by two servers side by side at a deploy, so every step is
    safe to repeat: a number that is already his is left alone, an audit entry is written
    only by the server whose own change it was, and the row goes in with `insert_once`.

    The variable wins. A number that sat on somebody else is taken from them, and a number
    the owner had before is replaced. Taking an entry out of the variable removes nothing:
    the number stays his until an owner takes it away on the Users page."""
    for email, phone in ctx.cfg.owner_phones:
        async with ctx.engine.begin() as conn:
            table = db.office_phones
            holder = (await conn.execute(sa.select(table).where(table.c.phone == phone))).first()
            if holder is not None and holder.email == email:
                continue
            if holder is not None:
                taken = await conn.execute(
                    sa.delete(table).where(table.c.phone == phone, table.c.email == holder.email)
                )
                if taken.rowcount:
                    await _retire_office_codes(conn, holder.email)
                    await add_event(
                        conn, "system", "env", "office_phone_removed",
                        detail={"email": holder.email, "ends": phone[-4:], "moved_to": email, "by": "VIDEO_OWNER_PHONES"},
                    )
            mine = (await conn.execute(sa.select(table).where(table.c.email == email))).first()
            if mine is not None and mine.phone == phone:
                continue  # the other server put it there a moment ago
            if mine is not None:
                await conn.execute(sa.delete(table).where(table.c.email == email, table.c.phone == mine.phone))
            added = await db.insert_once(conn, table, dict(phone=phone, email=email, added_at=db.now(), added_by="env"))
            if added:
                await _retire_office_codes(conn, email)
                detail = {"email": email, "ends": phone[-4:], "by": "VIDEO_OWNER_PHONES"}
                if mine is not None:
                    detail["was"] = mine.phone[-4:]
                await add_event(conn, "system", "env", "office_phone_set", detail=detail)


async def _retire_office_codes(conn: Any, email: str) -> None:
    """Inside a save that is open: no code this user was texted before now works any more.
    A number that is taken away or changed must stop at once, and a code texted to it a
    minute ago is part of that. So is one texted in the very moment a number was taken
    away, which is why a number that is put on someone retires them too. A code that was
    emailed is left alone: it went to the user's inbox, and only the email way's code
    step takes it."""
    await conn.execute(
        sa.update(db.login_codes)
        .where(db.login_codes.c.purpose == "office", db.login_codes.c.subject == email)
        .where(db.login_codes.c.issued_by == TEXTED, db.login_codes.c.used_at.is_(None))
        .values(used_at=db.now())
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
    await _check_code(ctx, "office", email, code, texted=False)
    return await _open_office_session(ctx, email)


async def _open_office_session(ctx: Ctx, email: str, detail: dict[str, Any] | None = None) -> str:
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
    await audit(ctx, "office", email, "login", detail=detail)
    return token


# -- office, the text way: a code texted to the number an owner set for that user ---------
#
# The code is the same kind of code as on the email way: purpose "office", subject the
# user's email. So "only the newest code counts", the 15 minutes and the lock after 5 wrong
# tries hold for it unchanged, and a code asked for one way retires one asked for the
# other. A texted code is marked (`TEXTED`) and is taken only at the text way's code step;
# an emailed one only at the email way's. Otherwise each step would tell on the other:
# a wrong code typed at the email step would be answered "try again" in place of "it ran
# out" the moment a text had gone to a number, and so say whose number it is.
#
# Nothing is given away about who is on the list. A number nobody has, the number of a
# user who was removed, and a listed number get the same page and the same answers, at
# the first step and at the code step, and each answer takes as long (`TEXT_ANSWER_SECONDS`).
# Only the listed, active one gets a text. For the others a code that nobody is sent is
# saved (a "decoy"), so that guessing at it is counted and locked exactly as guessing at
# a real one is.
#
# One exception, on purpose: when Quo refuses a text or does not answer, the page says a
# code may not come, and only a listed number ever gets as far as Quo. The page never
# says a text went when the server knows it may not have. The email way and a tech's
# sign-in have always told the truth in the same way. (The same goes for a Quo that takes
# longer to answer than `TEXT_ANSWER_SECONDS`.)
#
# Every message here that points to the email way does so only on a server where an
# emailed code reaches an inbox (`email_way`). The live server ran for its first weeks
# with no mail service, which is why the text way exists: a page that sent a person from
# a text that did not come to an email that cannot come would leave them where they began.

# Every answer about a number that could be on the list takes at least this long. A
# listed number's answer comes after a round trip to Quo and any other number's does
# not, and the time that takes must not tell the two apart.
TEXT_ANSWER_SECONDS = 2.0

NOT_A_CONTACT = "Type your mobile number or your email."
NOT_A_TEXT_NUMBER = "That is not a mobile number. Type all 10 digits and nothing else, like 916-555-0142."
NOT_A_NUMBER = "That is not a mobile number. Type all 10 digits."
NUMBER_TAKEN = "That number is already on another office user. Take it away there first."
NUMBER_TAKEN_FIXED = "That number is an owner's, set in the server's settings. Use another number."
NUMBER_FIXED = "That number is set in the server's settings and cannot be changed here."
NUMBER_IS_OFFICE_LINE = (
    "That is the office's own line, the one the codes are texted from. Use the person's own mobile number."
)
TEXT_FAILED = (
    "Something went wrong sending the text, so a code may not come. If one comes, type it below. If none comes "
    "in a minute, ask for a new one."
)


def email_way(ctx: Ctx) -> str:
    """What typing an email on the sign-in page leads to on this server. "inbox": a mail
    service is connected and the code reaches an inbox. "log": the code is only printed in
    the server's log (VIDEO_MAIL_BACKEND=log, for the first days). "": no email at all."""
    if ctx.mailer.delivers:
        return "inbox"
    return "log" if ctx.mailer.configured else ""


def _no_email(ctx: Ctx) -> str:
    """Said where a message would otherwise point to the email way."""
    if email_way(ctx) == "log":
        return "This server cannot send email yet: a code asked for by email is only written to the server's log."
    return "This server cannot send email yet."


def _no_texting(ctx: Ctx) -> str:
    if email_way(ctx) == "inbox":
        return "This server cannot send texts yet. Type your email here, and the code comes by email."
    if email_way(ctx) == "log":
        return ("This server cannot send texts yet, and it cannot send email yet either. A code asked for by "
                "email is only written to the server's log.")
    return "This server cannot send texts yet, and it cannot send email yet either."


def _text_failed(ctx: Ctx) -> str:
    if email_way(ctx) == "inbox":
        return f"{TEXT_FAILED} Or start again and type your email: the code then comes by email."
    return f"{TEXT_FAILED} {_no_email(ctx)}"


def _office_line(ctx: Ctx) -> str:
    """For someone who types the very line the codes are texted from."""
    words = "That is the office's own line. The codes are texted from it, so one cannot be texted to it. "
    return words + ("Type your own mobile number, or your email." if email_way(ctx) == "inbox"
                    else "Type your own mobile number.")


def _wait(seconds: int) -> str:
    minutes = max(1, -(-int(seconds) // 60))
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def is_office_line(ctx: Ctx, phone: str) -> bool:
    """Is this the line the codes are texted from (QUO_FROM_NUMBER)? Nobody has seen Quo
    text a line from itself, and a code texted to the office's own line would land in the
    inbox everyone on that line reads, not on one person's phone. So it is never tried."""
    return bool(phone) and phone == security.normalize_phone(ctx.cfg.quo_from)


def office_code_text(ctx: Ctx, code: str) -> str:
    """The text an office user gets. The code leads, as in a tech's text, so the phone
    offers it above the keyboard. "office" is what tells the two texts apart for someone
    who gets both. The same words as the subject of the emailed office code."""
    return f"{code} is your {ctx.cfg.brand_name} office code. It works once, for 15 minutes. Do not share it."


async def _office_user_by_phone(ctx: Ctx, phone: str) -> Any | None:
    """The active office user this number signs in, or None."""
    async with ctx.engine.connect() as conn:
        return (
            await conn.execute(
                sa.select(db.office_users.c.email)
                .join(db.office_phones, db.office_phones.c.email == db.office_users.c.email)
                .where(db.office_phones.c.phone == phone, db.office_users.c.active.is_(True))
            )
        ).first()


def _decoy(ctx: Ctx, phone: str) -> str:
    """What a code nobody is sent is filed under, so that a number a stranger typed is
    not kept. Keyed with the server's secret: a bare hash of ten digits can be turned
    back into the number by trying them all, which takes minutes."""
    return hmac.new(ctx.cfg.secret.encode(), f"decoy:{phone}".encode(), hashlib.sha256).hexdigest()


def _say(ctx: Ctx, line: str) -> None:
    print(f"zest-video: {hush(line, ctx.cfg)}", file=sys.stderr, flush=True)


async def request_office_text_code(ctx: Ctx, contact: str, ip: str) -> str:
    """Text a code to an office user's mobile number. Returns the ten digits, for the page
    that asks for the code. The page is the same whoever the number belongs to.

    An ApiError that carries `phone` is one the code step must be shown with: a text may
    have gone, or an earlier one is still good, so the box for the code belongs on the
    page that says what went wrong."""
    typed = (contact or "").replace("\x00", "")
    phone = security.normalize_phone(typed)
    if not phone:
        # Which of the two it says depends only on what was typed: something with digits
        # in it was meant as a number (one digit short, no area code, "ext 5" after it).
        meant_as_a_number = sum(ch.isdigit() for ch in typed) >= 3
        raise ApiError(400, "bad_request", NOT_A_TEXT_NUMBER if meant_as_a_number else NOT_A_CONTACT)
    # The next two are about this server and not about the number's owner. They are said
    # before anyone is looked up, so they are the same answer whoever has that number.
    if not ctx.texter.configured:
        raise ApiError(503, "server_error", _no_texting(ctx))
    if is_office_line(ctx, phone):
        raise ApiError(400, "bad_request", _office_line(ctx))
    # Limits of their own, in the shape of the email way's. Not shared with it: the email
    # way is the way back in, and nothing done here may use up its limits.
    wait = ctx.limiter.retry_after(f"otext-ip:{ip}", 10, 900)
    if wait:
        words = f"Too many tries. Wait {_wait(wait)}"
        words += ", or type your email and the code comes by email." if email_way(ctx) == "inbox" else "."
        raise ApiError(429, "slow_down", words, retry_after=wait)
    wait = ctx.limiter.retry_after(f"otext-to:{phone}", 5, 900)
    if wait:
        # Anybody can ask for a number five times, and so use up what its owner may ask.
        # The newest code he was texted is still good, so he is given the box to type it
        # in. The same answer for every number, listed or not.
        raise ApiError(
            429, "slow_down",
            f"No new code was sent: this number has been asked for too often. Type the newest code it got, or "
            f"wait {_wait(wait)}.",
            retry_after=wait, phone=phone,
        )
    started = time.monotonic()
    try:
        return await _text_office_code(ctx, phone)
    finally:
        await asyncio.sleep(max(0.0, TEXT_ANSWER_SECONDS - (time.monotonic() - started)))


async def _text_office_code(ctx: Ctx, phone: str) -> str:
    user = await _office_user_by_phone(ctx, phone)
    if user is None:
        await _new_code(ctx, "decoy", _decoy(ctx, phone))
        await audit(ctx, "system", "login", "code_request_unknown", detail={"kind": "office_phone"})
        return phone
    code = await _new_code(ctx, "office", user.email, issued_by=TEXTED)
    try:
        await ctx.texter.send(phone, office_code_text(ctx, code))
    except DeliveryError as exc:
        # The texter's own words are not passed on, to the page or to the log: they may
        # repeat the request, and the code and the whole number are in it. After a lost
        # answer the text may have gone all the same, so nothing says that it did not.
        #
        # One line in the log, every time. The owner this happens to cannot sign in to
        # read the audit trail, and the texter itself says nothing when Quo refuses a text
        # both with and without its Done field (400, 403, 404, 422: the likely answers for
        # a number that once replied STOP), or when QUO_MARK_DONE is off. Only what Quo
        # answered and the last four digits.
        ends = phone[-4:]
        if exc.status is None:
            what = (f"An office sign-in code text for the number ending {ends} got no answer from Quo, so nobody "
                    "knows whether it went.")
        else:
            what = f"Quo answered {exc.status} to an office sign-in code text for the number ending {ends}."
        _say(ctx, f"{what} The sign-in page said a code may not come.")
        await audit(ctx, "office", user.email, "code_requested", detail={"by": "text", "ends": ends, "delivered": []})
        raise ApiError(503, "server_error", _text_failed(ctx), phone=phone) from None
    await audit(ctx, "office", user.email, "code_requested", detail={"by": "text", "ends": phone[-4:], "delivered": ["text"]})
    return phone


async def verify_office_text_code(ctx: Ctx, contact: str, code: str, ip: str) -> str:
    """The code step of the text way. A number that signs nobody in fails here exactly as
    a wrong code does."""
    phone = security.normalize_phone((contact or "").replace("\x00", ""))
    code = "".join(ch for ch in str(code) if ch.isdigit())
    wait = ctx.limiter.retry_after(f"otverify-ip:{ip}", 30, 900)
    if wait:
        raise ApiError(429, "slow_down", f"Too many tries. Wait {_wait(wait)}.", retry_after=wait)
    no = ApiError(401, "bad_code", "That code is not right or it ran out. Ask for a new code.", triesLeft=0)
    if not phone:
        raise no
    user = await _office_user_by_phone(ctx, phone)
    if user is None:
        await _check_code(ctx, "decoy", _decoy(ctx, phone), code)  # raises, and counts the try, like any wrong code
        raise no  # one guess in a million matches a code nobody was sent. It opens nothing.
    await _check_code(ctx, "office", user.email, code, texted=True)
    return await _open_office_session(ctx, user.email, {"by": "text", "ends": phone[-4:]})


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


def fixed_office_phones(ctx: Ctx) -> dict[str, str]:
    """Owners whose number comes from VIDEO_OWNER_PHONES. It cannot be changed on a page."""
    return dict(ctx.cfg.owner_phones)


async def set_office_phone(ctx: Ctx, email: str, number: str, by: str) -> str:
    """An owner gives an office user the mobile number he signs in with, or changes it.
    Returns its last four digits, for the page to say. The audit entry carries no more
    than those four either."""
    email = (email or "").replace("\x00", "").strip().lower()[:254]
    phone = security.normalize_phone((number or "").replace("\x00", ""))
    if not phone:
        raise ApiError(400, "bad_request", NOT_A_NUMBER)
    if is_office_line(ctx, phone):
        raise ApiError(400, "bad_request", NUMBER_IS_OFFICE_LINE)
    if email in fixed_office_phones(ctx):
        raise ApiError(400, "bad_request", NUMBER_FIXED)
    table = db.office_phones
    failure: ApiError | None = None
    async with ctx.engine.begin() as conn:
        user = (
            await conn.execute(
                sa.select(db.office_users.c.email).where(db.office_users.c.email == email, db.office_users.c.active.is_(True))
            )
        ).first()
        holder = (await conn.execute(sa.select(table).where(table.c.phone == phone))).first()
        mine = (await conn.execute(sa.select(table).where(table.c.email == email))).first()
        if user is None:
            failure = ApiError(400, "bad_request", "That person is not on the office list. Add them first.")
        elif holder is not None and holder.email != email:
            # "Take it away there first" is no help when it sits on an owner by the variable:
            # there is nothing on his row to take it away with.
            fixed = fixed_office_phones(ctx).get(holder.email) == phone
            failure = ApiError(400, "bad_request", NUMBER_TAKEN_FIXED if fixed else NUMBER_TAKEN)
        elif holder is None and (mine is None or mine.phone != phone):
            # (Each read above sees the database as it is at that moment. So `mine` can
            # already hold this very number, put there between the two reads by the same
            # form sent twice. Then there is nothing left to do.)
            if mine is not None:
                # By its old number too: sent twice (a double click), the second one must
                # not take off what the first has just put on.
                await conn.execute(sa.delete(table).where(table.c.email == email, table.c.phone == mine.phone))
            if await db.insert_once(conn, table, dict(phone=phone, email=email, added_at=db.now(), added_by=by)):
                await _retire_office_codes(conn, email)
                detail = {"email": email, "ends": phone[-4:]}
                if mine is not None:
                    detail["was"] = mine.phone[-4:]
                await add_event(conn, "office", by, "office_phone_set", detail=detail)
            else:
                # Somebody was first by a moment. The same form sent twice: it is his
                # already, and that is all there is to say. Anything else is refused, and
                # raised inside the save on purpose: his old number, taken off just above,
                # is put back with the rest of it.
                now_on = (await conn.execute(sa.select(table).where(table.c.phone == phone))).first()
                if now_on is None:
                    raise ApiError(400, "bad_request", "Somebody changed that person's number at the same moment. "
                                   "Look at the list and try again.")
                if now_on.email != email:
                    raise ApiError(400, "bad_request", NUMBER_TAKEN)
    if failure is not None:
        raise failure
    return phone[-4:]


async def remove_office_phone(ctx: Ctx, email: str, by: str) -> bool:
    """An owner takes a user's number away. It signs nobody in from this moment, and a
    code texted to it a minute ago is dead too. True when there was one to take."""
    email = (email or "").replace("\x00", "").strip().lower()[:254]
    if email in fixed_office_phones(ctx):
        raise ApiError(400, "bad_request", NUMBER_FIXED)
    table = db.office_phones
    async with ctx.engine.begin() as conn:
        mine = (await conn.execute(sa.select(table).where(table.c.email == email))).first()
        if mine is None:
            return False
        gone = await conn.execute(sa.delete(table).where(table.c.email == email, table.c.phone == mine.phone))
        if gone.rowcount:
            await _retire_office_codes(conn, email)
            await add_event(conn, "office", by, "office_phone_removed", detail={"email": email, "ends": mine.phone[-4:]})
    return bool(gone.rowcount)
