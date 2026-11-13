"""Pages anyone with a link can open: the customer watch page, unsubscribe, privacy
and support. No login: the 128-bit token in the link is the key, so every page here
is marked noindex, never cached, and rate limited.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, db, videos
from .core import ApiError, Ctx, audit, get_settings, raise_flag
from .http_api import client_ip
from .render import render

STATIC_DIR = Path(__file__).parent / "static"


class CachedStatic(StaticFiles):
    """Logo, font and script are versioned by `?v=`, so browsers may keep them a long time."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        return response

PAGE_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": (
        "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; font-src 'self'; "
        "script-src 'self'; media-src 'self' https:; connect-src 'self'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}


def page(ctx: Ctx, template: str, status: int = 200, **context: Any) -> HTMLResponse:
    html = render(
        template,
        brand=ctx.cfg.brand_name,
        office_phone=ctx.cfg.office_phone,
        version=__version__,
        **context,
    )
    return HTMLResponse(html, status_code=status, headers=PAGE_HEADERS)


def verify_svix(secret: str, headers: Any, body: bytes, *, tolerance: int = 300) -> bool:
    """Resend signs webhooks the Svix way: HMAC-SHA256 over `id.timestamp.body` with the
    base64 key after the `whsec_` prefix."""
    msg_id, stamp, given = headers.get("svix-id"), headers.get("svix-timestamp"), headers.get("svix-signature")
    if not (secret and msg_id and stamp and given):
        return False
    try:
        if abs(time.time() - int(stamp)) > tolerance:
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError):
        return False
    expected = base64.b64encode(
        hmac.new(key, f"{msg_id}.{stamp}.".encode() + body, hashlib.sha256).digest()
    ).decode()
    return any(
        hmac.compare_digest(expected, part.split(",", 1)[1])
        for part in given.split()
        if part.startswith("v1,")
    )


def routes(ctx: Ctx) -> list[Any]:
    def gone(request: Request, status: int = 410) -> HTMLResponse:
        return page(ctx, "gone.html", status=status)

    async def find(request: Request, *, limit: int = 120) -> Any | None:
        """The video behind this link, or None. Guessing tokens gets slow fast."""
        ip = client_ip(request)
        if not ctx.limiter.allow(f"watch:{ip}", limit, 60):
            raise ApiError(429, "slow_down", "Too many tries. Wait a minute.")
        video = await videos.by_token(ctx, request.path_params["token"])
        if video is None and not ctx.limiter.allow(f"watch-miss:{ip}", 20, 600):
            raise ApiError(429, "slow_down", "Too many tries. Wait a few minutes.")
        return video

    def guarded(handler: Any) -> Any:
        async def endpoint(request: Request) -> Response:
            try:
                return await handler(request)
            except ApiError as exc:
                if exc.status == 429:
                    return page(ctx, "gone.html", status=429, message="Too many tries. Please wait a minute and try again.")
                return page(ctx, "gone.html", status=exc.status, message=exc.message)

        return endpoint

    async def watch(request: Request) -> Response:
        video = await find(request)
        if not videos.is_watchable(video):
            return gone(request, 404 if video is None else 410)
        score = await videos.get_score(ctx, video.id)
        return page(
            ctx,
            "watch.html",
            video=video,
            score=score,
            thanks=request.query_params.get("thanks") == "1",
            recorded=ctx.local(video.recorded_at),
        )

    async def stream(request: Request) -> Response:
        """The player's source. Redirects to a fresh signed bucket URL on every request,
        so the bucket stays private and a page left open for an hour still plays."""
        video = await find(request, limit=600)
        if not videos.is_watchable(video):
            return PlainTextResponse("This video is no longer available.", status_code=410, headers=PAGE_HEADERS)
        url = ctx.storage.get_url(video.object_key, ctx.cfg.watch_url_seconds)
        return RedirectResponse(url, status_code=302, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def played(request: Request) -> Response:
        video = await find(request)
        if videos.is_watchable(video):
            await videos.mark_played(ctx, video)
        return Response(status_code=204)

    async def score(request: Request) -> Response:
        video = await find(request)
        if not videos.is_watchable(video):
            return gone(request)
        form = await request.form()
        try:
            stars = int(str(form.get("stars") or "0"))
        except ValueError:
            stars = 0
        if not 1 <= stars <= 5:
            return RedirectResponse(f"/w/{video.token}#score", status_code=303)
        await videos.save_score(ctx, video, stars, str(form.get("comment") or ""))
        return RedirectResponse(f"/w/{video.token}?thanks=1#score", status_code=303)

    async def not_my_home(request: Request) -> Response:
        video = await find(request)
        if videos.is_watchable(video):
            await videos.not_my_home(ctx, video)
        return page(ctx, "gone.html", status=200, title="Thank you",
                    message="Thank you for telling us. We took this video down and our office will look into it.")

    async def unsubscribe(request: Request) -> Response:
        video = await find(request)
        if video is None:
            return gone(request, 404)
        if request.method == "POST":
            await videos.opt_out(ctx, video)
            return page(ctx, "gone.html", title="Done", message="You will not get video emails from us anymore.")
        return page(ctx, "unsubscribe.html", video=video)

    async def resend_hook(request: Request) -> Response:
        body = await request.body()
        if not verify_svix(ctx.cfg.resend_webhook_secret, request.headers, body):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            event = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "bad_request"}, status_code=400)
        kind = event.get("type")
        email_id = str((event.get("data") or {}).get("email_id") or "")
        if kind in ("email.bounced", "email.complained") and email_id:
            async with ctx.engine.connect() as conn:
                video = (await conn.execute(sa.select(db.videos).where(db.videos.c.email_id == email_id))).first()
            if video is not None:
                what = "bounced" if kind == "email.bounced" else "was marked as spam"
                await raise_flag(
                    ctx, "email_bounced", f"bounce:{video.id}", day=video.day, tech_id=video.tech_id,
                    customer_id=video.customer_id, video_id=video.id,
                    detail=f"The video email for {video.customer_name} {what}. Check the email on the account.",
                )
                await audit(ctx, "system", "email", "email_bounced", video_id=video.id, customer_id=video.customer_id,
                            detail={"type": kind})
        return JSONResponse({"ok": True})

    async def home(request: Request) -> Response:
        return page(ctx, "info.html", kind="home")

    async def privacy(request: Request) -> Response:
        months = (await get_settings(ctx))["retention_months"]
        return page(ctx, "info.html", kind="privacy", privacy_contact=ctx.cfg.privacy_contact, retention_months=months)

    async def support(request: Request) -> Response:
        return page(ctx, "info.html", kind="support", privacy_contact=ctx.cfg.privacy_contact)

    async def app_store(request: Request) -> Response:
        """Where the app's "please update" button lands: the App Store page once it exists."""
        return RedirectResponse(ctx.cfg.app_store_url or "/support", status_code=302)

    async def healthz(request: Request) -> Response:
        return PlainTextResponse("ok")

    async def robots(request: Request) -> Response:
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    return [
        Route("/healthz", healthz),
        Route("/robots.txt", robots),
        Route("/", home),
        Route("/privacy", privacy),
        Route("/support", support),
        Route("/app", app_store),
        Route("/w/{token}", guarded(watch)),
        Route("/w/{token}/video", guarded(stream)),
        Route("/w/{token}/played", guarded(played), methods=["POST"]),
        Route("/w/{token}/score", guarded(score), methods=["POST"]),
        Route("/w/{token}/not-my-home", guarded(not_my_home), methods=["POST"]),
        Route("/u/{token}", guarded(unsubscribe), methods=["GET", "POST"]),
        Route("/hooks/resend", resend_hook, methods=["POST"]),
        Mount("/static", CachedStatic(directory=str(STATIC_DIR)), name="static"),
    ]
