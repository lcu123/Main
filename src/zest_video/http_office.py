"""The office page: today's stops by tech with video status, flags on top, and the
few actions staff need (pull, move, note, issue a code, cut off a phone). Owners also
get settings, office users, per-tech stats and the audit trail.

Server-rendered HTML with plain forms. Sign-in is an emailed code for addresses on
the allow-list; every POST carries a CSRF token tied to the session.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, quote, urlsplit

import sqlalchemy as sa
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from . import __version__, auth, db, security, videos
from .core import DEFAULT_SETTINGS, FLAG_WORDS, ApiError, Ctx, audit, clear_flags, get_settings, set_setting
from .fr import FieldRoutesError
from .http_api import client_ip
from .jobs import techs_with_the_app
from .render import render
from .route import LIVE_STATES, sync_day

COOKIE = "zv_office"
OFFICE_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Cache-Control": "no-store",
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
}

EMAIL_SKIP_WORDS = {
    "email_off": "customer emails are off",
    "not_in_test_list": "customer is not on the test list",
    "opted_out": "customer stopped video emails",
    "no_address": "no email on file",
    "fr_reminders_off": "email reminders are off in FieldRoutes",
    "send_failed": "the email could not be sent",
    "demo": "demo",
}

SETTING_HELP = {
    "max_seconds": "Longest video, in seconds.",
    "hold_minutes": "Minutes the tech has to delete or record again before the customer is emailed.",
    "far_meters": "Warn the tech when he is farther than this from the address (meters).",
    "min_build": "Oldest app build allowed. Raise it to force an update.",
    "require_video": "Flag the office when a stop finishes with no video.",
    "recommend_button": "Show the tech the 'I recommended something' switch.",
    "leave_reminders": "Let the app remind the tech when he leaves a stop with no video. This asks the tech for "
    "'Always' location. Leave it off until Apple has approved the app.",
    "missing_alert_minutes": "Minutes after a stop finishes before 'no video' is flagged.",
    "stuck_hours": "Hours a video may sit on a phone before the office is flagged.",
    "retention_months": "Months videos are kept before they are deleted.",
    "skip_service_ids": "FieldRoutes service type IDs that do not need a video (space or comma between).",
    "respect_fr_email_reminders": "Do not email customers whose email reminders are off in FieldRoutes.",
    "email_subject": "Email subject. {tech} becomes the tech's first name.",
    "email_intro": "First lines of the email. {tech} and {address} are filled in.",
    "prompts": "What the tech reads while recording, one per line.",
}


def _valid_day(raw: str | None, default: str) -> str:
    try:
        return datetime.strptime(raw or "", "%Y-%m-%d").date().isoformat()
    except ValueError:
        return default


def _shift(day: str, days: int) -> str:
    return (datetime.strptime(day, "%Y-%m-%d").date() + timedelta(days=days)).isoformat()


async def tech_names(ctx: Ctx) -> dict[int, str]:
    async with ctx.engine.connect() as conn:
        rows = (await conn.execute(sa.select(db.techs.c.id, db.techs.c.fname, db.techs.c.lname))).all()
    return {r.id: f"{r.fname} {r.lname}".strip() or f"Tech {r.id}" for r in rows}


def video_status(ctx: Ctx, video: Any) -> tuple[str, str]:
    """(label, tone) for a video. Tones: good, wait, bad, off."""
    if video.state == "awaiting_upload":
        return "Waiting on phone", "wait"
    if video.state == "held":
        when = ctx.local(video.email_due_at).strftime("%-I:%M %p") if video.email_due_at else "soon"
        return f"Sending at {when}", "wait"
    if video.state == "sent":
        return ("Watched", "good") if video.first_viewed_at else ("Sent", "good")
    if video.state == "no_email":
        return f"No email: {EMAIL_SKIP_WORDS.get(video.email_skip, video.email_skip or 'not sent')}", "off"
    if video.state == "unmatched":
        return "Needs a customer", "bad"
    if video.state == "pulled":
        return "Pulled", "off"
    return video.state.capitalize(), "off"


async def board(ctx: Ctx, day: str) -> dict[str, Any]:
    names = await tech_names(ctx)
    async with ctx.engine.connect() as conn:
        stops = (
            await conn.execute(
                sa.select(db.stops).where(db.stops.c.day == day, db.stops.c.gone.is_(False)).order_by(db.stops.c.position)
            )
        ).all()
        vids = (
            await conn.execute(
                sa.select(db.videos)
                .where(db.videos.c.day == day, db.videos.c.demo.is_(False))
                .where(db.videos.c.state.in_((*LIVE_STATES, "pulled")))
                .order_by(db.videos.c.created_at)
            )
        ).all()
        score_rows = (
            await conn.execute(sa.select(db.scores).where(db.scores.c.video_id.in_([v.id for v in vids] or [""])))
        ).all()
        open_flags = (
            await conn.execute(
                sa.select(db.flags).where(db.flags.c.cleared_at.is_(None)).order_by(db.flags.c.created_at.desc()).limit(200)
            )
        ).all()
    scores = {s.video_id: s for s in score_rows}

    # Prefer the live video for a customer; fall back to a pulled one so the row says so.
    by_customer: dict[int, Any] = {}
    for v in vids:
        if v.customer_id is None:
            continue
        current = by_customer.get(v.customer_id)
        if current is None or (current.state == "pulled" and v.state != "pulled"):
            by_customer[v.customer_id] = v

    equipped = await techs_with_the_app(ctx)
    groups: dict[int, list[dict[str, Any]]] = {}
    seen_videos: set[str] = set()
    counts = {"stops": 0, "done": 0, "with_video": 0, "missing": 0}
    for stop in stops:
        tech_id = (stop.tech_ids or [0])[0]
        video = by_customer.get(stop.customer_id)
        has_app = bool(equipped.intersection(stop.tech_ids or []))
        if video is not None:
            seen_videos.add(video.id)
            label, tone = video_status(ctx, video)
        elif not has_app:
            label, tone = "Tech has no app yet", "off"
        elif stop.done:
            label, tone = "No video", "bad"
        else:
            label, tone = "Not done yet", "off"
        missing = has_app and stop.done and (video is None or video.state == "pulled")
        counts["stops"] += 1
        counts["done"] += 1 if stop.done else 0
        counts["with_video"] += 1 if video is not None and video.state != "pulled" else 0
        counts["missing"] += 1 if missing else 0
        groups.setdefault(tech_id, []).append(
            {"stop": stop, "video": video, "label": label, "tone": tone, "score": scores.get(video.id) if video else None}
        )

    others = [
        {"video": v, "label": video_status(ctx, v)[0], "tone": video_status(ctx, v)[1], "score": scores.get(v.id)}
        for v in vids
        if v.id not in seen_videos
    ]
    open_stops = [s for s in stops if s.customer_id not in by_customer or by_customer[s.customer_id].state == "pulled"]
    return {
        "day": day,
        "groups": sorted(((names.get(t, f"Tech {t}" if t else "No tech"), rows) for t, rows in groups.items()), key=lambda g: g[0]),
        "others": others,
        "open_stops": open_stops,
        "flags": [
            {"flag": f, "words": FLAG_WORDS.get(f.kind, f.kind), "tech": names.get(f.tech_id or 0, "")} for f in open_flags
        ],
        "counts": counts,
    }


async def stats(ctx: Ctx, days: int) -> list[dict[str, Any]]:
    """Per tech over the last `days`: videos sent, stops missed, watch rate, average score."""
    settings = await get_settings(ctx)
    skip = set(settings["skip_service_ids"] or [])
    end = ctx.today()
    start = _shift(end, -(days - 1))
    names = await tech_names(ctx)
    async with ctx.engine.connect() as conn:
        stops = (
            await conn.execute(
                sa.select(db.stops).where(db.stops.c.day.between(start, end), db.stops.c.gone.is_(False), db.stops.c.done.is_(True))
            )
        ).all()
        vids = (
            await conn.execute(
                sa.select(db.videos)
                .where(db.videos.c.day.between(start, end), db.videos.c.demo.is_(False))
                .where(db.videos.c.state.in_((*LIVE_STATES, "pulled")))
            )
        ).all()
        score_rows = (
            await conn.execute(sa.select(db.scores).where(db.scores.c.video_id.in_([v.id for v in vids] or [""])))
        ).all()
    filmed = {(v.day, v.customer_id) for v in vids if v.customer_id is not None}
    stars = {s.video_id: s.stars for s in score_rows}
    equipped = await techs_with_the_app(ctx)

    rows: dict[int, dict[str, Any]] = {}

    def row(tech_id: int) -> dict[str, Any]:
        return rows.setdefault(
            tech_id,
            {"name": names.get(tech_id, f"Tech {tech_id}"), "stops": 0, "missed": 0, "videos": 0, "sent": 0,
             "watched": 0, "stars": []},
        )

    for stop in stops:
        types = {a.get("type") for a in stop.appointments}
        if types and types <= skip:
            continue
        if not equipped.intersection(stop.tech_ids or []):
            continue  # a tech without the app cannot miss a video
        r = row((stop.tech_ids or [0])[0])
        r["stops"] += 1
        if (stop.day, stop.customer_id) not in filmed:
            r["missed"] += 1
    for v in vids:
        r = row(v.tech_id)
        r["videos"] += 1
        if v.state == "sent":
            r["sent"] += 1
            r["watched"] += 1 if v.first_viewed_at else 0
        if v.id in stars:
            r["stars"].append(stars[v.id])
    out = []
    for r in rows.values():
        r["watch_rate"] = round(100 * r["watched"] / r["sent"]) if r["sent"] else None
        r["avg_stars"] = round(sum(r["stars"]) / len(r["stars"]), 1) if r["stars"] else None
        r["scores"] = len(r["stars"])
        out.append(r)
    return sorted(out, key=lambda r: r["name"])


Handler = Callable[[Request, auth.OfficeUser], Awaitable[Response]]


def routes(ctx: Ctx) -> list[Route]:
    secure_cookie = ctx.cfg.base_url.startswith("https://")

    def html(template: str, user: auth.OfficeUser | None = None, status: int = 200, **context: Any) -> HTMLResponse:
        return HTMLResponse(
            render(
                template,
                brand=ctx.cfg.brand_name,
                version=__version__,
                user=user,
                csrf=security.csrf_token(ctx.cfg.secret, user.session) if user else "",
                email_mode=ctx.cfg.email_mode if ctx.mailer.delivers else "off",
                mail_delivers=ctx.mailer.delivers,
                email_test_to=ctx.cfg.email_test_to,
                tz=ctx.cfg.tz,
                **context,
            ),
            status_code=status,
            headers=OFFICE_HEADERS,
        )

    def back(request: Request, fallback: str = "/office", *, msg: str = "", err: str = "") -> RedirectResponse:
        # Only ever send people back inside the office page (the target can come from a
        # form field or the Referer header), keeping just the day they were looking at.
        parts = urlsplit(fallback)
        target = parts.path if parts.path.startswith("/office") else "/office"
        day = parse_qs(parts.query).get("day", [""])[0]
        if day:
            target += f"?day={_valid_day(day, ctx.today())}"
        sep = "&" if "?" in target else "?"
        if msg:
            target += f"{sep}msg={quote(msg)}"
        elif err:
            target += f"{sep}err={quote(err)}"
        return RedirectResponse(target, status_code=303)

    def page(handler: Handler, *, owner: bool = False) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            user = await auth.office_user(ctx, request.cookies.get(COOKIE, ""))
            if user is None:
                return RedirectResponse("/office/login", status_code=303)
            if owner and not user.owner:
                return html("office_message.html", user, status=403, title="Owners only",
                            message="Ask the owner to do this, or to make you an owner.")
            if request.method == "POST":
                form = await request.form()
                if not security.same(str(form.get("csrf") or ""), security.csrf_token(ctx.cfg.secret, user.session)):
                    return html("office_message.html", user, status=400, title="That page was stale",
                                message="Go back, refresh, and try again.")
            try:
                return await handler(request, user)
            except ApiError as exc:
                if request.method == "POST":
                    return back(request, request.headers.get("referer") or "/office", err=exc.message)
                return html("office_message.html", user, status=exc.status, title="That did not work", message=exc.message)

        return endpoint

    # -- sign in ---------------------------------------------------------------

    async def login(request: Request) -> Response:
        if request.method == "GET":
            return html("office_login.html", step="email")
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        try:
            await auth.request_office_code(ctx, email, client_ip(request))
        except ApiError as exc:
            return html("office_login.html", step="email", error=exc.message, status=exc.status)
        return html("office_login.html", step="code", email=email)

    async def verify(request: Request) -> Response:
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        try:
            token = await auth.verify_office_code(ctx, email, str(form.get("code") or ""), client_ip(request))
        except ApiError as exc:
            return html("office_login.html", step="code", email=email, error=exc.message, status=exc.status)
        response = RedirectResponse("/office", status_code=303)
        response.set_cookie(
            COOKIE, token, max_age=auth.OFFICE_SESSION_DAYS * 86400, httponly=True, secure=secure_cookie,
            samesite="lax", path="/office",
        )
        return response

    async def logout(request: Request, user: auth.OfficeUser) -> Response:
        await auth.office_logout(ctx, user.session)
        response = RedirectResponse("/office/login", status_code=303)
        response.delete_cookie(COOKIE, path="/office")
        return response

    # -- the day ---------------------------------------------------------------

    async def day_page(request: Request, user: auth.OfficeUser) -> Response:
        today = ctx.today()
        day = _valid_day(request.query_params.get("day"), today)
        stale = ""
        if day == today or request.query_params.get("refresh") == "1":
            try:
                await sync_day(ctx, day, force=request.query_params.get("refresh") == "1")
            except FieldRoutesError as exc:
                stale = f"FieldRoutes did not answer, so this is the last saved list. ({exc})"
        data = await board(ctx, day)
        return html(
            "office_day.html", user, **data, today=today, prev_day=_shift(day, -1), next_day=_shift(day, 1),
            stale=stale, msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""),
            now=db.now(),
        )

    async def open_video(request: Request, user: auth.OfficeUser) -> Response:
        video = await videos.get_video(ctx, request.path_params["video_id"])
        if video is None or video.object_deleted or video.state == "awaiting_upload":
            raise ApiError(404, "bad_request", "That video's file is not here.")
        await audit(ctx, "office", user.email, "video_opened", video_id=video.id, customer_id=video.customer_id)
        return RedirectResponse(ctx.storage.get_url(video.object_key, ctx.cfg.watch_url_seconds), status_code=302)

    async def pull_video(request: Request, user: auth.OfficeUser) -> Response:
        form = await request.form()
        await videos.pull(ctx, request.path_params["video_id"], user.email, str(form.get("reason") or "").strip())
        return back(request, str(form.get("back") or "/office"), msg="Video pulled. Its link is dead.")

    async def move_video(request: Request, user: auth.OfficeUser) -> Response:
        form = await request.form()
        raw = str(form.get("customer_id") or form.get("customer_id_other") or "").strip()
        if not raw.isdigit():
            raise ApiError(400, "bad_request", "Pick a customer, or type the FieldRoutes customer number.")
        await videos.move(ctx, request.path_params["video_id"], int(raw), user.email)
        return back(request, str(form.get("back") or "/office"), msg="Video moved. The right customer gets the email in a minute.")

    async def clear_flag(request: Request, user: auth.OfficeUser) -> Response:
        form = await request.form()
        flag_id = int(request.path_params["flag_id"])
        note = str(form.get("note") or "").strip()
        cleared = await clear_flags(ctx, db.flags.c.id == flag_id, user.email, note)
        if cleared:
            await audit(ctx, "office", user.email, "flag_cleared", detail={"flag": flag_id, "note": note[:300]})
        return back(request, str(form.get("back") or "/office"), msg="Flag cleared.")

    # -- techs and phones ------------------------------------------------------

    async def techs_page(request: Request, user: auth.OfficeUser) -> Response:
        async with ctx.engine.connect() as conn:
            techs = (
                await conn.execute(
                    sa.select(db.techs).where(db.techs.c.demo.is_(False)).order_by(db.techs.c.active.desc(), db.techs.c.fname)
                )
            ).all()
            devices = (await conn.execute(sa.select(db.devices).order_by(db.devices.c.last_seen_at.desc()))).all()
        by_tech: dict[int, list[Any]] = {}
        for d in devices:
            by_tech.setdefault(d.tech_id, []).append(d)
        return html(
            "office_techs.html", user, techs=techs, devices=by_tech, msg=request.query_params.get("msg", ""),
            err=request.query_params.get("err", ""), code=request.query_params.get("code", ""),
            code_for=request.query_params.get("for", ""),
            mask=security.mask_email,
        )

    async def refresh_techs(request: Request, user: auth.OfficeUser) -> Response:
        try:
            count = await auth.sync_techs(ctx)
        except FieldRoutesError as exc:
            raise ApiError(503, "server_error", f"FieldRoutes did not answer: {exc}") from exc
        return back(request, "/office/techs", msg=f"Found {count} active techs in FieldRoutes.")

    async def issue_code(request: Request, user: auth.OfficeUser) -> Response:
        tech_id = int(request.path_params["tech_id"])
        code = await auth.issue_code_by_hand(ctx, tech_id, user.email)
        names = await tech_names(ctx)
        return RedirectResponse(f"/office/techs?code={code}&for={quote(names.get(tech_id, ''))}", status_code=303)

    async def cut_device(request: Request, user: auth.OfficeUser) -> Response:
        device_id = request.path_params["device_id"]
        if await auth.revoke_device(ctx, device_id, user.email):
            await audit(ctx, "office", user.email, "device_cut_off", detail={"device": device_id})
        return back(request, "/office/techs", msg="That phone is cut off.")

    # -- owner -----------------------------------------------------------------

    async def settings_page(request: Request, user: auth.OfficeUser) -> Response:
        if request.method == "POST":
            form = await request.form()
            errors = []
            for key, default in DEFAULT_SETTINGS.items():
                raw: Any = form.get(key)
                if isinstance(default, bool):
                    raw = raw is not None
                elif raw is None:
                    continue
                try:
                    await set_setting(ctx, key, raw, user.email)
                except ValueError as exc:
                    errors.append(str(exc))
            if errors:
                return back(request, "/office/settings", err=" ".join(errors))
            return back(request, "/office/settings", msg="Saved.")
        return html(
            "office_settings.html", user, settings=await get_settings(ctx), defaults=DEFAULT_SETTINGS, help=SETTING_HELP,
            msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""), cfg=ctx.cfg,
        )

    async def users_page(request: Request, user: auth.OfficeUser) -> Response:
        if request.method == "POST":
            form = await request.form()
            await auth.set_office_user(
                ctx, str(form.get("email") or ""), str(form.get("role") or "staff"), form.get("remove") is None, user.email
            )
            return back(request, "/office/users", msg="Saved.")
        async with ctx.engine.connect() as conn:
            users = (await conn.execute(sa.select(db.office_users).order_by(db.office_users.c.email))).all()
        return html("office_users.html", user, users=users, fixed=set(ctx.cfg.owner_emails),
                    msg=request.query_params.get("msg", ""), err=request.query_params.get("err", ""))

    async def stats_page(request: Request, user: auth.OfficeUser) -> Response:
        try:
            days = max(1, min(120, int(request.query_params.get("days", "30"))))
        except ValueError:
            days = 30
        return html("office_stats.html", user, rows=await stats(ctx, days), days=days)

    async def audit_page(request: Request, user: auth.OfficeUser) -> Response:
        query = sa.select(db.events).order_by(db.events.c.id.desc()).limit(500)
        video_id = request.query_params.get("video", "").strip().lower()
        action = request.query_params.get("action", "").strip()
        if video_id:
            query = query.where(db.events.c.video_id == video_id)
        if action:
            query = query.where(db.events.c.action == action)
        async with ctx.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        names = await tech_names(ctx)
        return html("office_audit.html", user, rows=rows, names=names, video_id=video_id, action=action)

    return [
        Route("/office/login", login, methods=["GET", "POST"]),
        Route("/office/verify", verify, methods=["POST"]),
        Route("/office/logout", page(logout), methods=["POST"]),
        Route("/office", page(day_page)),
        Route("/office/video/{video_id}", page(open_video)),
        Route("/office/video/{video_id}/pull", page(pull_video), methods=["POST"]),
        Route("/office/video/{video_id}/move", page(move_video), methods=["POST"]),
        Route("/office/flag/{flag_id:int}/clear", page(clear_flag), methods=["POST"]),
        Route("/office/techs", page(techs_page)),
        Route("/office/techs/refresh", page(refresh_techs), methods=["POST"]),
        Route("/office/techs/{tech_id:int}/code", page(issue_code), methods=["POST"]),
        Route("/office/devices/{device_id}/cut", page(cut_device), methods=["POST"]),
        Route("/office/settings", page(settings_page, owner=True), methods=["GET", "POST"]),
        Route("/office/users", page(users_page, owner=True), methods=["GET", "POST"]),
        Route("/office/stats", page(stats_page, owner=True)),
        Route("/office/audit", page(audit_page, owner=True)),
    ]
