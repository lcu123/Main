"""Wires the server together and runs it.

    zest-video            # serves on $PORT (default 8080)

One process: the HTTP app plus the background loop in jobs.py. It is a
separate Railway service from the FieldRoutes MCP server but ships in the same
image and uses the same FieldRoutes client and keys.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import Any, AsyncIterator

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from fr_mcp.client import FieldRoutesClient

from . import __version__, auth, db, http_api, http_office, http_web, jobs
from .config import Config, load
from .core import Ctx
from .fr import FieldRoutes
from .notify import Pusher, Texter, make_mailer
from .storage import MemoryStorage, S3Storage


def build_ctx(cfg: Config, **overrides: Any) -> Ctx:
    """Everything the handlers need. Tests pass fakes through `overrides`."""
    engine = overrides.pop("engine", None) or db.make_engine(cfg.database_url)
    fr_client = overrides.pop("fr_client", None) or FieldRoutesClient()
    storage = overrides.pop("storage", None) or (S3Storage(cfg) if cfg.storage_configured else MemoryStorage())
    return Ctx(
        cfg=cfg,
        engine=engine,
        fr=FieldRoutes(fr_client, cfg),
        storage=storage,
        mailer=overrides.pop("mailer", None) or make_mailer(cfg),
        texter=overrides.pop("texter", None) or Texter(cfg),
        pusher=overrides.pop("pusher", None) or Pusher(cfg),
        **overrides,
    )


class SecurityHeaders:
    """HTTPS-only: tell browsers to stay on HTTPS, and bounce plain HTTP that reaches us
    through the proxy. Health checks from inside the platform stay on HTTP."""

    def __init__(self, app: ASGIApp, *, https: bool):
        self.app = app
        self.https = https

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.https:
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        if headers.get("x-forwarded-proto") == "http" and scope.get("path") != "/healthz":
            host = headers.get("host", "")
            query = scope.get("query_string", b"").decode()
            url = f"https://{host}{scope.get('path', '/')}" + (f"?{query}" if query else "")
            await Response(status_code=308, headers={"Location": url})(scope, receive, send)
            return

        async def send_with_hsts(message: Any) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).append(
                    (b"strict-transport-security", b"max-age=31536000; includeSubDomains")
                )
            await send(message)

        await self.app(scope, receive, send_with_hsts)


def build_app(ctx: Ctx, *, run_jobs: bool = True) -> ASGIApp:
    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await db.create_all(ctx.engine)
        await auth.ensure_owners(ctx)
        task = asyncio.create_task(jobs.run_forever(ctx)) if run_jobs else None
        try:
            yield
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await ctx.engine.dispose()

    async def server_error(request: Request, exc: Exception) -> Response:
        if request.url.path.startswith("/v1/"):
            return JSONResponse({"error": "server_error", "message": "Something went wrong. Try again."}, status_code=500)
        return http_web.page(ctx, "gone.html", status=500, title="Something went wrong",
                             message="Please try again in a minute.")

    async def not_found(request: Request, exc: Exception) -> Response:
        if request.url.path.startswith("/v1/"):
            return JSONResponse({"error": "bad_request", "message": "No such address."}, status_code=404)
        return http_web.page(ctx, "gone.html", status=404, title="Page not found", message="That page is not here.")

    app = Starlette(
        routes=[*http_api.routes(ctx), *http_office.routes(ctx), *http_web.routes(ctx)],
        lifespan=lifespan,
        exception_handlers={500: server_error, 404: not_found},
    )
    app.state.ctx = ctx
    return SecurityHeaders(app, https=ctx.cfg.base_url.startswith("https://"))


def main() -> None:
    import uvicorn

    cfg = load()
    ctx = build_ctx(cfg)
    port = int(os.environ.get("PORT", "8080"))
    print(
        f"zest-video {__version__}: serving on :{port}; base={cfg.base_url}; "
        f"storage={'bucket' if cfg.storage_configured else 'MEMORY (not for real use)'}; "
        f"mail={cfg.mail_backend}; customer-email={cfg.email_mode}; "
        f"text={'on' if cfg.quo_api_key and cfg.quo_from else 'off'}; push={'on' if cfg.push_configured else 'off'}; "
        f"fr-writes={'on' if cfg.fr_writes else 'off'}; demo={'on' if cfg.demo_enabled else 'off'}",
        file=sys.stderr,
    )
    if cfg.secret == "dev-only-secret-change-me" and cfg.base_url.startswith("https://"):
        print("zest-video: WARNING: set VIDEO_SECRET to a long random value", file=sys.stderr)
    # access_log=False: request paths carry watch tokens, which are the customers' keys.
    uvicorn.run(build_app(ctx), host="0.0.0.0", port=port, log_level="info", access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
