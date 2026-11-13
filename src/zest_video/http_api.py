"""The phone's API (docs/video/API.md). JSON in, JSON out, one bearer token per phone."""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

import sqlalchemy as sa
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import auth, db, videos
from .core import ApiError, Ctx, audit, get_settings
from .route import today_payload

MAX_BODY = 64 * 1024
EVENT_TYPES = {
    "record_start", "record_stop", "retake", "draft_discarded", "far_warning", "override",
    "two_keys_warning", "storage_warn", "storage_block", "stop_switched", "permission",
}


def client_ip(request: Request) -> str:
    """Behind Railway's edge the real client is the last X-Forwarded-For entry (the one
    the trusted proxy appended); earlier entries are whatever the caller typed."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def error_response(exc: ApiError) -> JSONResponse:
    extra = dict(exc.extra)
    headers = {}
    retry = extra.pop("retry_after", None)
    if retry:
        headers["Retry-After"] = str(retry)
    return JSONResponse({"error": exc.code, "message": exc.message, **extra}, status_code=exc.status, headers=headers)


async def read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise ApiError(400, "bad_request", "That request is too big.")
    if not raw:
        return {}
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ApiError(400, "bad_request", "That request could not be read.") from exc
    if not isinstance(body, dict):
        raise ApiError(400, "bad_request", "That request could not be read.")
    return body


def _build(request: Request) -> int:
    try:
        return int(request.headers.get("x-zest-build", "0"))
    except ValueError:
        return 0


Handler = Callable[[Request, Ctx], Awaitable[Response]]
AuthedHandler = Callable[[Request, Ctx, auth.Device], Awaitable[Response]]


def routes(ctx: Ctx) -> list[Route]:
    async def gate(request: Request) -> None:
        """An app too old to trust gets told to update, on every call."""
        build = _build(request)
        if build:
            minimum = (await get_settings(ctx))["min_build"]
            if build < minimum:
                raise ApiError(426, "update_required", "Please update Zest Video to keep going.", minBuild=minimum)

    def public(handler: Handler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                await gate(request)
                return await handler(request, ctx)
            except ApiError as exc:
                return error_response(exc)

        return endpoint

    def authed(handler: AuthedHandler) -> Callable[[Request], Awaitable[Response]]:
        async def endpoint(request: Request) -> Response:
            try:
                await gate(request)
                header = request.headers.get("authorization", "")
                token = header[7:].strip() if header.lower().startswith("bearer ") else ""
                device = await auth.authenticate(ctx, token, _build(request))
                return await handler(request, ctx, device)
            except ApiError as exc:
                return error_response(exc)

        return endpoint

    async def request_code(request: Request, ctx: Ctx) -> Response:
        body = await read_json(request)
        await auth.request_tech_code(ctx, str(body.get("contact") or ""), client_ip(request))
        return JSONResponse({"ok": True})

    async def verify(request: Request, ctx: Ctx) -> Response:
        body = await read_json(request)
        result = await auth.verify_tech_code(
            ctx, str(body.get("contact") or ""), str(body.get("code") or ""), body, client_ip(request)
        )
        return JSONResponse(result)

    async def logout(request: Request, ctx: Ctx, device: auth.Device) -> Response:
        await auth.revoke_device(ctx, device.id, f"tech:{device.tech_id}")
        await audit(ctx, "tech", device.tech_id, "logout", detail={"device": device.id})
        return JSONResponse({"ok": True})

    async def today(request: Request, ctx: Ctx, device: auth.Device) -> Response:
        return JSONResponse(await today_payload(ctx, device))

    async def register(request: Request, ctx: Ctx, device: auth.Device) -> Response:
        return JSONResponse(await videos.register(ctx, device, await read_json(request)))

    async def complete(request: Request, ctx: Ctx, device: auth.Device) -> Response:
        return JSONResponse(
            await videos.complete(ctx, device, request.path_params["video_id"].lower(), await read_json(request))
        )

    async def delete(request: Request, ctx: Ctx, device: auth.Device) -> Response:
        return JSONResponse(await videos.delete_by_tech(ctx, device, request.path_params["video_id"].lower()))

    async def push_token(request: Request, ctx: Ctx, device: auth.Device) -> Response:
        body = await read_json(request)
        token = "".join(ch for ch in str(body.get("apnsToken") or "").lower() if ch in "0123456789abcdef")[:200]
        async with ctx.engine.begin() as conn:
            await conn.execute(
                sa.update(db.devices)
                .where(db.devices.c.id == device.id)
                .values(apns_token=token, apns_sandbox=bool(body.get("sandbox")))
            )
        return JSONResponse({"ok": True})

    async def events(request: Request, ctx: Ctx, device: auth.Device) -> Response:
        body = await read_json(request)
        items = body.get("events") if isinstance(body.get("events"), list) else []
        saved = 0
        for item in items[:100]:
            if not isinstance(item, dict) or item.get("type") not in EVENT_TYPES:
                continue
            video_id = str(item.get("videoId") or "").lower()
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            if len(json.dumps(detail)) > 2000:
                detail = {"note": "detail too large, dropped"}
            await audit(
                ctx, "tech", device.tech_id, f"app_{item['type']}",
                video_id=video_id if videos.UUID_RE.match(video_id) else None,
                customer_id=item.get("customerId") if isinstance(item.get("customerId"), int) else None,
                detail=detail,
                at=videos._parse_time(item.get("at")),
            )
            saved += 1
        return JSONResponse({"ok": True, "saved": saved})

    return [
        Route("/v1/auth/request-code", public(request_code), methods=["POST"]),
        Route("/v1/auth/verify", public(verify), methods=["POST"]),
        Route("/v1/auth/logout", authed(logout), methods=["POST"]),
        Route("/v1/today", authed(today), methods=["GET"]),
        Route("/v1/videos", authed(register), methods=["POST"]),
        Route("/v1/videos/{video_id}/complete", authed(complete), methods=["POST"]),
        Route("/v1/videos/{video_id}", authed(delete), methods=["DELETE"]),
        Route("/v1/devices/push", authed(push_token), methods=["POST"]),
        Route("/v1/events", authed(events), methods=["POST"]),
    ]
