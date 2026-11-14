"""Wires the server together and runs it.

    zest-video            # serves on $PORT (default 8080)

One process: the HTTP app plus the background loop in jobs.py. It is a
separate Railway service from the FieldRoutes MCP server but ships in the same
image and uses the same FieldRoutes client and keys.

The container this runs in is thrown away at every restart and redeploy, and
everything on its disk and in its memory goes with it. So nothing that matters
is kept there: records are in Postgres, videos are in the bucket, and a start
goes through three gates before the server takes a request or runs a job:

1. the settings (`config.check_start`): a real deployment with no Postgres or
   no bucket does not start;
2. the database answers (`wait_for_database`: it is waited for, for a few minutes);
3. the bucket and the database belong together (`check_bucket`): the bucket holds
   this database's videos, and the database is not a new one beside a full bucket.

A start that is refused says why once, in plain words, and never with a secret in it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from typing import Any, AsyncIterator

import sqlalchemy as sa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from fr_mcp.client import FieldRoutesClient

from . import __version__, auth, db, http_api, http_office, http_web, jobs
from .config import Config, StartRefused, check_start, hush, load, overrides_on, switch_notes
from .core import Ctx
from .fr import FieldRoutes
from .notify import Pusher, Texter, make_mailer
from .storage import FOUND, MISSING, REFUSALS, UNKNOWN, MemoryStorage, S3Storage

# How long a start keeps asking a database that does not answer before it gives up.
# Long enough to sit out a Postgres restart, short enough that a database that is
# really gone shows up in Railway as a crash and not as a server that hangs for good.
DATABASE_PATIENCE = 300.0
DATABASE_PAUSE = 3.0
# The bucket gets one short look at start-up. It must never hold a restart up for long.
BUCKET_WAIT = 10.0
BUCKET_SAMPLE = 5  # how many of the newest videos are looked for
BUCKET_PROOF = 3  # this many looked for and none found: that is not the right bucket
BUCKET_PROBE_KEY = "videos/start-up-check"  # asked about when the database has no videos yet
VIDEO_PREFIX = "videos/"  # every video's file is under this (videos.register)
HAS_A_FILE = ("held", "sent", "no_email", "unmatched")


def say(line: str) -> None:
    print(f"zest-video: {line}", file=sys.stderr, flush=True)


def build_ctx(cfg: Config, **overrides: Any) -> Ctx:
    """Everything the handlers need. Tests pass fakes through `overrides`."""
    # Whatever is not handed in is chosen below from the settings, and with nothing set
    # that means a file here for records and this process's memory for videos. Fine on a
    # laptop. On a real deployment this raises StartRefused, so neither fallback can ever
    # happen there quietly.
    check_start(cfg, database=overrides.get("engine") is None, bucket=overrides.get("storage") is None)
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


# -- gates 2 and 3: the database answers, and the bucket is the right one ---------------


def _why(exc: BaseException, ctx: Ctx) -> str:
    """A short reason that is safe in a log: the kind of error and its first line, with
    the database password cut out in case a driver ever quotes the address. The secrets
    are cut before the line is shortened, so that no half of one is left at the end."""
    lines = str(exc).strip().splitlines()
    text = hush(f"{type(exc).__name__}: {lines[0] if lines else ''}", ctx.cfg)
    password = ctx.engine.url.password
    return (text.replace(str(password), "***") if password else text)[:200]


async def wait_for_database(ctx: Ctx, *, patience: float | None = None) -> None:
    """Gate 2: the database answers and its tables are there.

    A database that does not answer is asked again for up to `patience` seconds
    (DATABASE_PATIENCE when not given), not given up on at once. Railway restarts a
    crashed service a few times and then leaves it down, so a server that fell over
    because Postgres was restarting beside it would stay down until someone noticed.
    Nothing listens while this waits: on a new deploy the health check cannot pass and
    the version before keeps serving; on a restart the phones are told to try again
    later and keep their videos."""
    patience = DATABASE_PATIENCE if patience is None else patience
    started, pause = time.monotonic(), DATABASE_PAUSE
    while True:
        try:
            await db.create_all(ctx.engine)
            return
        except Exception as exc:  # noqa: BLE001 - every way a database can fail to answer
            if isinstance(exc, TypeError):
                # Not a database that is slow to come up: the driver would not take what
                # the address asks for (`?sslmode=...` is one such), and never will.
                # Waiting five minutes for it would only hide the mistake.
                raise StartRefused(
                    "The database address cannot be used as it is written.",
                    [
                        f"The database driver refused it: {_why(exc, ctx)}",
                        "Check VIDEO_DATABASE_URL. On Railway it is ${{Postgres.DATABASE_URL}}, with nothing "
                        "added after it.",
                    ],
                ) from None
            if time.monotonic() - started + pause > patience:
                waited = f"for {patience / 60:.0f} minutes" if patience >= 60 else "when the server started"
                raise StartRefused(
                    f"The database did not answer {waited}.",
                    [
                        f"The last try ended with: {_why(exc, ctx)}",
                        "Check that the Postgres service is running and that VIDEO_DATABASE_URL still points at it.",
                    ],
                    ["Nothing is lost: phones keep their videos and send them when the server is back."],
                    # `from None`: the reason is in the message, with the password cut out.
                    # Chained, the first failure would be printed again in full.
                ) from None
            say(f"the database is not answering yet ({_why(exc, ctx)}). Asking again in {pause:.0f} seconds.")
            await asyncio.sleep(pause)


async def database_first(ctx: Ctx) -> None:
    """The patient wait for the database, run by `main` before the web server exists.

    It is done there, and not in the server's own start-up, for one reason: a web server
    that is starting up notes a stop signal and acts on it only once it has started, so a
    wait in there cannot be stopped. Out here the process stops the moment it is told to.

    The connections made here belong to this short-lived event loop, so they are closed
    before the server's own loop makes its own."""
    try:
        await wait_for_database(ctx)
    finally:
        await ctx.engine.dispose()


async def check_bucket(ctx: Ctx) -> str:
    """Gate 3: do the bucket in the settings and the database belong together?

    A server pointed at the wrong bucket does real harm, one video at a time: when a
    customer opens a link, or an email comes due, the file is looked for, not found, and
    the video is pulled and flagged as missing (videos.file_unchanged). So the newest
    videos the database knows are looked for before anything else runs.

    The other way round is as bad. A database that knows of no video at all, beside a
    bucket that holds videos, is a new or a different database: every link already sent
    is dead and every phone is signed out, and the server would look healthy. A video's
    row is never deleted, so a file in the bucket always has one in the right database.

    One thing tells a new database from one that is in use with old files beside it
    (test files from before the pilot, or videos from after a backup that was put back):
    whether a phone has ever signed in on it. Nobody can sign in on a database before a
    server has started on it. So a database with no video and no sign-in is new or was
    emptied, and the server does not start on it. One with sign-ins is the database the
    techs are already working on; refusing then would only take a running service down
    at its next restart, so it starts and says what it found.

    Returns one line for the start-up log. Raises StartRefused only on a real
    deployment, and only on an answer that cannot be a passing fault: the storage says
    the key, the bucket name or the region is wrong; or it says, in its own words, that
    it holds none of the newest videos; or it holds videos and the database is new.
    A bucket that cannot be reached, or is slow, or errors, settles nothing and never
    stops a start: while it is away phones keep their videos."""
    cfg = ctx.cfg
    ask = getattr(ctx.storage, "ask", None)
    look = getattr(ctx.storage, "look", None)
    if ask is None:
        return "not checked (this storage cannot be asked)"
    async with ctx.engine.connect() as conn:
        rows = (
            await conn.execute(
                # Demo videos are left out: their files are deleted after a day while the
                # row stays as it was, so a missing one proves nothing.
                sa.select(db.videos.c.object_key)
                .where(
                    db.videos.c.state.in_(HAS_A_FILE),
                    db.videos.c.demo.is_(False),
                    db.videos.c.etag != "",
                    db.videos.c.object_deleted.is_(False),
                    db.videos.c.uploaded_at.is_not(None),
                )
                .order_by(db.videos.c.uploaded_at.desc())
                .limit(BUCKET_SAMPLE)
            )
        ).all()
        knows_a_video = (await conn.execute(sa.select(db.videos.c.id).limit(1))).first() is not None
        in_use = (await conn.execute(sa.select(db.devices.c.id).limit(1))).first() is not None
    keys = [r.object_key for r in rows]

    async def questions() -> tuple[list[str], str]:
        answers = list(await asyncio.gather(*(ask(k) for k in keys or [BUCKET_PROBE_KEY])))
        # Only a database with not one video row is asked about, and only once the bucket
        # has shown that it answers and takes our keys.
        holds = UNKNOWN
        if not knows_a_video and look is not None and answers == [MISSING]:
            holds = await look(VIDEO_PREFIX)
        return answers, holds

    try:
        answers, holds = await asyncio.wait_for(questions(), BUCKET_WAIT)
    except Exception:  # noqa: BLE001 - too slow, or it raised: nothing can be said
        answers, holds = [UNKNOWN], UNKNOWN
    refused = next((a for a in answers if a in REFUSALS), None)
    found, missing = answers.count(FOUND), answers.count(MISSING)

    if refused:
        if cfg.real_deployment:
            raise StartRefused("The video storage says the settings are wrong:", [REFUSALS[refused]])
        return f"WARNING: {REFUSALS[refused]}"
    if holds == FOUND and in_use:
        return (
            "WARNING: the bucket holds video files, and this database knows of no video. Phones have signed in "
            "on this database, so it is taken to be the one in use, and those files to be old ones (test files, "
            "or videos from after a backup that was put back). If this is not the database you expect, check "
            "VIDEO_DATABASE_URL"
        )
    if holds == FOUND:
        problem = (
            "The bucket holds videos, and the database does not know of one video, and no phone has ever signed "
            "in on it. That is a new database, or one that was emptied, or VIDEO_DATABASE_URL points at a "
            "different one."
        )
        if cfg.real_deployment and not cfg.start_with_new_database:
            raise StartRefused(
                "The video bucket and the database do not belong together:",
                [problem],
                [
                    "Nothing was changed. On a new database every link already sent to a customer is dead and",
                    "every phone is signed out, so the server is not started. Point VIDEO_DATABASE_URL back at",
                    "the database these videos belong to (or restore its backup) and deploy again.",
                    "If the database really is new, and the files in the bucket are old test files:",
                    "VIDEO_START_WITH_NEW_DATABASE=1. Remove it again once the first phone has signed in.",
                ],
            )
        return f"WARNING: {problem}"
    # None found, and enough clear "not here" answers to be sure. A lost answer among
    # them does not undo the proof: one more "not here" would change nothing.
    if found == 0 and missing >= BUCKET_PROOF:
        if isinstance(ctx.storage, MemoryStorage) and cfg.real_deployment:
            # Only reachable with VIDEO_ALLOW_THROWAWAY_STORAGE on. Nothing is the wrong
            # bucket here: memory was emptied by the restart, as that override said.
            return (
                f"WARNING: videos are kept in this server's memory, so the files of the {len(keys)} newest "
                "videos the database knows went with the last restart"
            )
        unclear = len(keys) - missing
        detail = f" ({missing} are not there, and {unclear} got no clear answer)" if unclear else ""
        problem = (
            f"The bucket holds none of the newest {len(keys)} videos the database knows{detail}. Either the "
            "bucket settings (VIDEO_S3_ENDPOINT, VIDEO_S3_BUCKET) point at the wrong bucket, or the bucket was emptied."
        )
        if cfg.real_deployment and not cfg.start_with_files_missing:
            raise StartRefused(
                "The video bucket and the database do not belong together:",
                [problem],
                [
                    "Nothing was changed. A server running on the wrong bucket would take down every video a",
                    "customer opens, so it is not started. Fix the bucket settings and deploy again.",
                    "If the files really are gone and the server should start anyway, and mark each missing",
                    "video for the office as it is found: VIDEO_START_WITH_FILES_MISSING=1.",
                ],
            )
        return f"WARNING: {problem}"
    if found and keys:
        return f"the bucket answers and holds the newest videos ({found} of {len(keys)} looked for)"
    if missing and keys:
        return f"WARNING: the bucket answers, and {missing} of the newest {len(keys)} videos are not in it"
    if missing:
        # "No videos yet" is only said of a database with none. One that holds demo videos,
        # or videos still on their way up, has videos; there is just no file to look for.
        line = (
            "the bucket answers (no customer videos to look for yet)" if knows_a_video
            else "the bucket answers (the database has no videos yet)"
        )
        if not knows_a_video and holds == UNKNOWN:
            line += (
                ". WARNING: the bucket would not say whether it holds videos already, so it could not be "
                "checked that this is not a new database beside a full bucket"
            )
        return line
    return (
        "WARNING: the bucket gave no clear answer (it could not be reached, or it had an error). "
        "Starting anyway: phones keep their videos until it answers"
    )


async def start_up(ctx: Ctx) -> None:
    """Gates 2 and 3. Runs before the server listens and before the first job, so a
    server that fails here has taken no request, sent no email and pulled no video.

    The database gets one try here: `main` has already waited for it (`database_first`).

    Whatever goes wrong in here ends as a StartRefused whose message has been printed:
    plain words, once, with no secret in them."""
    try:
        await wait_for_database(ctx, patience=0)
        await auth.ensure_owners(ctx)
        bucket = await check_bucket(ctx)
    except StartRefused as exc:
        print(exc.message, file=sys.stderr, flush=True)
        raise
    except Exception as exc:  # noqa: BLE001 - anything else must not reach the log as a stack of code
        refusal = StartRefused(
            "The start-up checks could not be finished.",
            [f"They ended with: {_why(exc, ctx)}"],
            ["Nothing was changed, and no job ran. Phones keep their videos until the server is back."],
        )
        print(refusal.message, file=sys.stderr, flush=True)
        raise refusal from None
    records = "Postgres" if ctx.engine.dialect.name == "postgresql" else f"{ctx.engine.dialect.name} file"
    say(f"start-up checks passed. Records: {records}, answering. Videos: {bucket}.")


class PlainRefusal:
    """Keeps a refused start to the one plain message that `start_up` has already printed.

    Left alone, the framework hands the web server the whole stack of code as text, and
    the web server prints it: the same message a second time, in the middle of a
    traceback. This swaps that text for nothing. The refusal is still raised, so the
    start still fails and the process still exits with an error."""

    def __init__(self, app: ASGIApp, refused: list[StartRefused]):
        self.app = app
        self.refused = refused

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self.app(scope, receive, send)
            return

        async def send_plainly(message: Any) -> None:
            if message["type"] == "lifespan.startup.failed" and self.refused:
                message = {"type": "lifespan.startup.failed", "message": ""}
            await send(message)

        await self.app(scope, receive, send_plainly)


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
    refused: list[StartRefused] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        # The server does not listen, and no job runs, until this returns. If it raises,
        # the process exits and Railway keeps the version before.
        try:
            await start_up(ctx)
        except StartRefused as exc:
            refused.append(exc)  # already printed; `PlainRefusal` keeps it to that
            raise
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
    return SecurityHeaders(PlainRefusal(app, refused), https=ctx.cfg.base_url.startswith("https://"))


def _stop_now(signum: int, frame: Any) -> None:
    raise SystemExit(128 + signum)


def main() -> None:
    import uvicorn

    cfg = load()
    # Gate 1, before anything is built or listens: a real deployment that would keep
    # records in a file or videos in memory stops here, with one plain message.
    try:
        notes = check_start(cfg) + switch_notes()
        ctx = build_ctx(cfg)
    except StartRefused as exc:
        print(exc.message, file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    except sa.exc.ArgumentError:
        # The address could not be read at all. Its text is not printed: it holds a password.
        say("NOT starting: VIDEO_DATABASE_URL could not be read as a database address.")
        raise SystemExit(1) from None
    except Exception as exc:  # noqa: BLE001 - one plain line, never a stack of code with an address in it
        lines = str(exc).strip().splitlines()
        why = hush(f"{type(exc).__name__}: {lines[0] if lines else ''}", cfg)[:200]
        say(f"NOT starting: the server could not be set up ({why}). If it names a database driver or an "
            "address, check VIDEO_DATABASE_URL: it must start with postgresql://")
        raise SystemExit(1) from None
    port = int(os.environ.get("PORT", "8080"))
    print(
        f"zest-video {__version__}: starting on :{port}; base={cfg.base_url}; "
        f"where={cfg.platform or ('a public address' if cfg.real_deployment else 'local')}; "
        f"records={'Postgres' if cfg.database_url.startswith('postgresql') else 'A LOCAL FILE (not for real use)'}; "
        f"videos={'bucket' if cfg.storage_configured else 'MEMORY (gone when this stops; not for real use)'}; "
        f"mail={cfg.mail_backend}; customer-email={cfg.email_mode}; "
        f"text={'on' if cfg.quo_api_key and cfg.quo_from else 'off'}; push={'on' if cfg.push_configured else 'off'}; "
        f"fr-writes={'on' if cfg.fr_writes else 'off'}; demo={'on' if cfg.demo_enabled else 'off'}; "
        f"overrides={', '.join(overrides_on(cfg)) or 'none'}",
        file=sys.stderr,
        flush=True,
    )
    for note in notes:
        say(note)
    if cfg.secret == "dev-only-secret-change-me" and cfg.base_url.startswith("https://"):
        say("WARNING: set VIDEO_SECRET to a long random value")
    # Gate 2, still before the web server exists, so a stop signal stops the wait at once.
    # In a container this is process number 1, and a process number 1 is only sent a stop
    # signal it has asked to hear. So ask, for as long as the wait lasts. The web server
    # listens for the signal itself once it runs, so it is handed back what was there.
    try:
        before: Any = signal.signal(signal.SIGTERM, _stop_now) or signal.SIG_DFL
    except ValueError:  # not the main thread (a test harness): nothing to hand back
        before = None
    try:
        asyncio.run(database_first(ctx))
    except StartRefused as exc:
        print(exc.message, file=sys.stderr, flush=True)
        raise SystemExit(1) from None
    finally:
        if before is not None:
            signal.signal(signal.SIGTERM, before)
    # access_log=False: request paths carry watch tokens, which are the customers' keys.
    uvicorn.run(build_app(ctx), host="0.0.0.0", port=port, log_level="info", access_log=False, proxy_headers=False)


if __name__ == "__main__":
    main()
