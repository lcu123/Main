"""Nothing may be kept on the container itself.

Railway throws the container away at every restart and redeploy, and its disk and its
memory go with it. Records belong in Postgres and videos in the bucket. These tests pin
the three gates a start goes through, and what the health check says:

1. the settings: a real deployment that has lost its database or its bucket does not
   start, and says which variable is missing;
2. the database answers before anything listens;
3. the bucket is the one this database's videos are in;

and that a laptop, a test, CI and the browser suite, which have none of this, still start.
And one thing while it runs: a video is never taken down on an answer from the bucket
that settles nothing.

Every name, address and key in this file is made up.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import http.server
import os
import pathlib
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import sqlalchemy as sa

from video_helpers import Env, demo_login, film, login, make_env

from zest_video import app as video_app
from zest_video import auth, config, db, http_web, jobs, videos
from zest_video.config import StartRefused, check_start, public_https
from zest_video.storage import FOUND, MISSING, UNKNOWN, MemoryStorage, S3Storage, presign

# What Railway itself puts in every container it runs.
RAILWAY = {
    "RAILWAY_ENVIRONMENT_NAME": "production",
    "RAILWAY_ENVIRONMENT_ID": "env-0000",
    "RAILWAY_PROJECT_ID": "project-0000",
    "RAILWAY_SERVICE_ID": "service-0000",
    "RAILWAY_SERVICE_NAME": "zest-video",
    "RAILWAY_DEPLOYMENT_ID": "deployment-0000",
    "RAILWAY_REPLICA_ID": "replica-0000",
    "RAILWAY_PUBLIC_DOMAIN": "zest-video-made-up.up.railway.app",
    "RAILWAY_PRIVATE_DOMAIN": "zest-video.railway.internal",
}
# The names that are set on the deployed service today (checked read-only on Nov 14, 2026),
# with made-up values. If one of these names changes, this is the list to change with it.
DURABLE = {
    "VIDEO_DATABASE_URL": "postgresql://postgres:made-up-db-password@postgres.railway.internal:5432/railway",
    "VIDEO_S3_ENDPOINT": "https://storage.example.com",
    "VIDEO_S3_BUCKET": "zest-videos",
    "VIDEO_S3_REGION": "auto",
    "VIDEO_S3_ACCESS_KEY_ID": "made-up-key-id",
    "VIDEO_S3_SECRET_ACCESS_KEY": "made-up-secret-key",
}
PRODUCTION = {**RAILWAY, **DURABLE, "VIDEO_EMAIL_MODE": "off", "FR_WRITES": "off"}
SECRETS = ("made-up-db-password", "made-up-secret-key")
# The key's ID is not a secret (it is part of every signed link a phone is handed), but a
# message about settings has no business quoting it either.
VALUES = (*SECRETS, "made-up-key-id")
MUST_HAVE = ("VIDEO_DATABASE_URL", "VIDEO_S3_ENDPOINT", "VIDEO_S3_BUCKET", "VIDEO_S3_ACCESS_KEY_ID",
             "VIDEO_S3_SECRET_ACCESS_KEY")


OVERRIDES = ("VIDEO_ALLOW_THROWAWAY_STORAGE", "VIDEO_START_WITH_FILES_MISSING", "VIDEO_START_WITH_NEW_DATABASE")


@pytest.fixture(autouse=True)
def not_this_machines_own_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests may themselves be run on Railway (`railway run pytest`, or a box that
    Railway hosts), or on a machine with an override left set. The shared fixture reads the
    environment, so without this every test here would be taken for a real deployment
    and refused. A test that wants one of these variables sets it itself."""
    for name in list(os.environ):
        if name.startswith("RAILWAY_") or name in OVERRIDES:
            monkeypatch.delenv(name)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch):
    """Start from an environment with none of the video server's settings in it, then set
    exactly what a test names. Returns a function: settings(env) -> Config."""
    for name in list(os.environ):
        if name == "VIDEO_TEST_DATABASE_URL":
            continue  # the test suite's own switch, not a setting of the server
        if name.startswith(("VIDEO_", "RAILWAY_", "RESEND_", "QUO_", "APNS_")) or name in (
            "DATABASE_URL", "FR_WRITES", "FR_WRITE_CUSTOMER_IDS",
        ):
            monkeypatch.delenv(name)

    def load(env: dict[str, str]) -> config.Config:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return config.load()

    return load


def refusal(cfg: config.Config) -> str:
    with pytest.raises(StartRefused) as caught:
        check_start(cfg)
    message = caught.value.message
    assert "NOT starting" in message
    assert not any(value in message for value in VALUES), "a log line must never carry a key or a password"
    return message


# == gate 1: the settings ==============================================================


async def test_the_settings_on_the_deployed_server_today_start(settings) -> None:
    cfg = settings(PRODUCTION)
    assert cfg.platform == "Railway" and cfg.real_deployment
    assert check_start(cfg) == [], "nothing to refuse and nothing to warn about"

    ctx = video_app.build_ctx(cfg)
    try:
        assert isinstance(ctx.storage, S3Storage), "videos go to the bucket"
        assert ctx.engine.dialect.name == "postgresql" and ctx.engine.driver == "asyncpg", "records go to Postgres"
        upload = ctx.storage.put_url("videos/2026-11-14/a.mp4", "video/mp4", 86400)
        assert upload.startswith("https://zest-videos.storage.example.com/videos/2026-11-14/a.mp4?")
    finally:
        await ctx.engine.dispose()


@pytest.fixture
def entry(monkeypatch):
    """`main()` with the web server and the database stood in for: `served` gets the app it
    would have served, `asked` the address of each database it tried. `answers_after` is how
    many tries fail before the database answers (None: it never does)."""
    import uvicorn

    class Entry:
        served: list[Any] = []
        asked: list[Any] = []
        answers_after: int | None = 0

    async def create_all(engine: Any) -> None:
        Entry.asked.append(engine.url)
        if Entry.answers_after is None or len(Entry.asked) <= Entry.answers_after:
            raise ConnectionRefusedError("connection refused: postgres.railway.internal:5432 made-up-db-password")

    Entry.served, Entry.asked, Entry.answers_after = [], [], 0
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: Entry.served.append(app))
    monkeypatch.setattr(db, "create_all", create_all)
    monkeypatch.setattr(video_app, "DATABASE_PAUSE", 0.01)
    return Entry


def test_the_real_entry_point_starts_with_those_settings(settings, entry, capsys) -> None:
    settings(PRODUCTION)
    video_app.main()
    assert len(entry.served) == 1, "it got as far as serving"
    assert [url.drivername for url in entry.asked] == ["postgresql+asyncpg"], "after the database answered"
    line = capsys.readouterr().err
    assert "records=Postgres" in line and "videos=bucket" in line and "where=Railway" in line
    assert "NOT starting" not in line and "fine on a laptop" not in line and "THROWAWAY" not in line
    assert not any(value in line for value in VALUES)


def test_railways_own_name_for_the_database_is_accepted_too(settings) -> None:
    env = {k: v for k, v in PRODUCTION.items() if k != "VIDEO_DATABASE_URL"}
    cfg = settings({**env, "DATABASE_URL": DURABLE["VIDEO_DATABASE_URL"]})
    assert check_start(cfg) == [] and cfg.database_url.startswith("postgresql+asyncpg://")


@pytest.mark.parametrize("name", MUST_HAVE)
@pytest.mark.parametrize("how", ["deleted", "empty", "only spaces", "mistyped"])
def test_losing_any_one_setting_stops_a_real_deployment_and_names_it(settings, name: str, how: str) -> None:
    env = dict(PRODUCTION)
    value = env.pop(name)
    if how == "empty":
        env[name] = ""
    elif how == "only spaces":
        env[name] = "   "
    elif how == "mistyped":
        env[name.replace("VIDEO_", "VIDEOS_")] = value  # the value is there, under a name nothing reads
    cfg = settings(env)

    message = refusal(cfg)
    assert name in message, "the message names the variable that is gone"
    others = [other for other in MUST_HAVE if other != name and other in message]
    assert others == [], f"and only that one, not {others}"
    with pytest.raises(StartRefused):
        video_app.build_ctx(cfg)  # the quiet fall-back to memory is not reachable either


@pytest.mark.parametrize("name", MUST_HAVE)
def test_the_real_entry_point_exits_before_anything_listens(settings, entry, capsys, name: str) -> None:
    settings({k: v for k, v in PRODUCTION.items() if k != name})
    with pytest.raises(SystemExit) as stopped:
        video_app.main()
    assert stopped.value.code == 1, "a failed start, so Railway keeps the version before"
    assert entry.served == [] and entry.asked == [], "it never served, and never even tried a database"
    err = capsys.readouterr().err
    assert "zest-video is NOT starting." in err and name in err
    assert "the version before it keeps serving" in err
    assert not any(value in err for value in VALUES)


@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:////data/zest_video.db",
    "sqlite+aiosqlite:///./zest_video.db",
    "sqlite:///zest_video.db",
])
def test_a_database_address_that_is_really_a_file_is_caught(settings, url: str) -> None:
    message = refusal(settings({**PRODUCTION, "VIDEO_DATABASE_URL": url}))
    assert "VIDEO_DATABASE_URL" in message and ("SQLite file" in message or "missing or empty" in message)
    assert "inside the container" in message


@pytest.mark.parametrize("url", [
    "${{Postgres.DATABASE_URL}}",  # a Railway reference that was never filled in
    "mysql://user:made-up-db-password@db.internal/zest",
    "postgresql://",  # the right kind of address with nothing in it
    "postgres.railway.internal:5432/railway",
])
def test_a_database_address_that_is_not_postgres_is_caught(settings, url: str) -> None:
    message = refusal(settings({**PRODUCTION, "VIDEO_DATABASE_URL": url}))
    assert "VIDEO_DATABASE_URL is not a Postgres address" in message


def test_an_endpoint_with_no_bucket_and_a_bucket_with_no_keys_are_caught(settings, monkeypatch) -> None:
    no_bucket = settings({k: v for k, v in PRODUCTION.items() if k != "VIDEO_S3_BUCKET"})
    assert "VIDEO_S3_BUCKET is missing or empty" in refusal(no_bucket)

    monkeypatch.delenv("VIDEO_S3_ACCESS_KEY_ID")
    monkeypatch.delenv("VIDEO_S3_SECRET_ACCESS_KEY")
    no_keys = settings({"VIDEO_S3_BUCKET": "zest-videos"})
    assert "VIDEO_S3_ACCESS_KEY_ID and VIDEO_S3_SECRET_ACCESS_KEY are missing or empty" in refusal(no_keys)

    not_an_address = settings({**DURABLE, "VIDEO_S3_ENDPOINT": "storage.example.com"})
    assert "VIDEO_S3_ENDPOINT is not a web address" in refusal(not_an_address)


def test_everything_gone_at_once_is_one_message_that_lists_it_all(settings) -> None:
    message = refusal(settings(RAILWAY))
    assert message.count("zest-video is NOT starting.") == 1
    assert "VIDEO_DATABASE_URL is missing or empty" in message
    assert all(name in message for name in MUST_HAVE)
    assert "This is a real deployment (on Railway)" in message
    assert "VIDEO_ALLOW_THROWAWAY_STORAGE=1" in message, "and it says how a demo gets past, by name"


def test_the_region_alone_may_be_left_out(settings) -> None:
    """The region has a working default, and a wrong one is caught by asking the bucket
    (gate 3), so its absence alone does not stop a start."""
    cfg = settings({k: v for k, v in PRODUCTION.items() if k != "VIDEO_S3_REGION"})
    assert check_start(cfg) == [] and cfg.s3_region == "auto"


async def test_a_demo_can_be_let_through_but_only_by_a_loud_name_and_it_shouts_every_start(settings) -> None:
    cfg = settings({**RAILWAY, "VIDEO_ALLOW_THROWAWAY_STORAGE": "1", "VIDEO_EMAIL_MODE": "off", "FR_WRITES": "off"})
    notes = check_start(cfg)
    assert "THIS SERVER LOSES ITS DATA AT THE NEXT RESTART" in notes[0]
    assert any("VIDEO_DATABASE_URL" in n for n in notes) and any("VIDEO_S3_BUCKET" in n for n in notes)
    ctx = video_app.build_ctx(cfg)
    try:
        assert isinstance(ctx.storage, MemoryStorage)
    finally:
        await ctx.engine.dispose()

    for not_yes in ("0", "no", "off", ""):
        assert "NOT starting" in refusal(settings({"VIDEO_ALLOW_THROWAWAY_STORAGE": not_yes}))


# -- telling a real deployment from a laptop, a test and CI -----------------------------


@pytest.mark.parametrize("url, public", [
    ("https://zest-video-made-up.up.railway.app", True),
    ("https://video.zestlawnpest.com", True),
    ("https://8.8.8.8", True),
    ("http://video.zestlawnpest.com", False),  # not https: nothing a customer could be sent
    ("http://localhost:8080", False),
    ("https://localhost", False),
    ("https://127.0.0.1:19301", False),
    ("https://10.0.0.12", False),
    ("https://video.test", False),  # what the test suite uses
    ("https://video.example.com", False),  # what .env.example shows
    ("https://zest-video.railway.internal", False),
    ("https://server", False),
    ("video.zestlawnpest.com", False),
    ("", False),
])
def test_what_counts_as_a_public_address(url: str, public: bool) -> None:
    assert public_https(url) is public


@pytest.mark.parametrize("mark", config.RAILWAY_MARKS)
def test_any_one_of_railways_own_variables_means_a_real_deployment(settings, mark: str) -> None:
    cfg = settings({mark: "x"})
    assert cfg.platform == "Railway" and cfg.real_deployment
    assert "NOT starting" in refusal(cfg), "and with nothing else set, it does not start"


def test_a_public_https_address_alone_means_a_real_deployment_on_any_host(settings) -> None:
    cfg = settings({"VIDEO_BASE_URL": "https://video.zestlawnpest.com"})
    assert cfg.platform == "" and cfg.real_deployment
    message = refusal(cfg)
    assert "at https://video.zestlawnpest.com" in message and "VIDEO_DATABASE_URL" in message


async def test_a_laptop_still_starts_and_is_told_what_it_is_running_on(settings) -> None:
    cfg = settings({})
    assert not cfg.real_deployment
    notes = check_start(cfg)
    assert len(notes) == 2 and all(n.startswith("WARNING, fine on a laptop") for n in notes)
    ctx = video_app.build_ctx(cfg)
    try:
        assert isinstance(ctx.storage, MemoryStorage) and ctx.engine.dialect.name == "sqlite"
    finally:
        await ctx.engine.dispose()


def test_the_railway_command_line_tools_own_variables_do_not_make_a_laptop_a_deployment(settings) -> None:
    cfg = settings({"RAILWAY_TOKEN": "made-up", "RAILWAY_API_TOKEN": "made-up"})
    assert not cfg.real_deployment


def test_the_ci_smoke_test_and_the_readme_command_still_start(settings) -> None:
    ci = settings({"PORT": "8080", "VIDEO_DATABASE_URL": "sqlite+aiosqlite:////tmp/zest_video_ci.db"})
    assert not ci.real_deployment and all(n.startswith("WARNING") for n in check_start(ci))
    readme = settings({"VIDEO_DATABASE_URL": "sqlite+aiosqlite:///./zest_video.db", "VIDEO_MAIL_BACKEND": "log"})
    assert not readme.real_deployment and check_start(readme)


async def test_the_test_suite_and_the_browser_suite_are_not_taken_for_deployments(tmp_path) -> None:
    env = await make_env(tmp_path)
    try:
        assert not env.ctx.cfg.real_deployment, "https://video.test is a name set aside for tests"
        for base in ("http://127.0.0.1:19300", "https://127.0.0.1:19301", "http://localhost:19300"):
            env.reconfigure(base_url=base)  # what tests/browser/serve.py passes
            assert not env.ctx.cfg.real_deployment
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


# -- the server's own address: an emailed link cannot be called back --------------------


def test_a_lost_base_url_falls_back_to_the_address_railway_gave_the_service(settings) -> None:
    cfg = settings({**PRODUCTION, "VIDEO_EMAIL_MODE": "live", "FR_WRITES": "on"})
    assert cfg.base_url == "https://zest-video-made-up.up.railway.app", "never http://localhost in a customer's email"
    assert check_start(cfg) == []


def test_the_base_url_that_was_set_wins_over_railways(settings) -> None:
    cfg = settings({**PRODUCTION, "VIDEO_BASE_URL": "https://video.zestlawnpest.com/"})
    assert cfg.base_url == "https://video.zestlawnpest.com"


@pytest.mark.parametrize("switch", [{"VIDEO_EMAIL_MODE": "live"}, {"VIDEO_EMAIL_MODE": "test"}, {"FR_WRITES": "on"}])
def test_with_no_public_address_nothing_that_carries_a_link_may_go_out(settings, switch: dict[str, str]) -> None:
    env = {k: v for k, v in PRODUCTION.items() if k != "RAILWAY_PUBLIC_DOMAIN"}
    quiet = settings(env)  # email off, FieldRoutes writes off: no link can leave
    assert quiet.base_url == "http://localhost:8080" and check_start(quiet) == []

    message = refusal(settings(switch))
    assert "VIDEO_BASE_URL" in message and "Every link sent out would be dead" in message
    assert "VIDEO_ALLOW_THROWAWAY_STORAGE" not in message, "that override is about storage, not about dead links"


# -- one switch, one meaning: FR_WRITES here and in the FieldRoutes connector -----------


@pytest.mark.parametrize("word, on", [
    ("on", True), ("ON", True), (" On ", True), ("1", True), ("true", True), ("yes", True),
    ("off", False), ("OFF", False), ("0", False), ("false", False), ("no", False),
    ("disabled", False), ("of", False), ("onn", False), ("", False), ("   ", False),
])
def test_fr_writes_means_here_what_it_means_in_the_connector(settings, word: str, on: bool) -> None:
    """The video server and the FieldRoutes connector read the same variable. If one word
    meant "off" there and "on" here, the office would be told writes are off while notes,
    tasks and reviews were being written to customers' accounts."""
    from fr_mcp import server as connector

    cfg = settings({**PRODUCTION, "FR_WRITES": word})
    assert cfg.fr_writes is on
    assert cfg.fr_writes is connector._writes_enabled(), "one office, one answer"
    known = word.strip().lower() in ("on", "1", "true", "yes", "off", "0", "false", "no")
    said = [n for n in config.switch_notes() if "FR_WRITES" in n]
    assert bool(said) is not known, "a word it does not know is read as off, and it says so"
    assert all(n.startswith("WARNING") and "read as off" in n for n in said)


def test_a_missing_fr_writes_still_means_on_in_both_places(settings) -> None:
    """Not changed here: what a deleted FR_WRITES should mean is the owner's to decide."""
    from fr_mcp import server as connector

    cfg = settings({k: v for k, v in PRODUCTION.items() if k != "FR_WRITES"})
    assert cfg.fr_writes is True and connector._writes_enabled() is True
    assert config.switch_notes() == []


def test_a_mistyped_switch_is_named_at_the_start_and_turns_nothing_on(settings, entry, capsys) -> None:
    settings({**PRODUCTION, "FR_WRITES": "flase", "VIDEO_EMAIL_MODE": "on"})
    video_app.main()
    err = capsys.readouterr().err
    assert "fr-writes=off" in err and "customer-email=off" in err
    assert "WARNING: FR_WRITES is set to a word this server does not know" in err
    assert "WARNING: VIDEO_EMAIL_MODE is set to a word this server does not know" in err
    assert "flase" not in err, "names only; a value is never printed"


# -- an override left behind must not sit there in silence ------------------------------


@pytest.mark.parametrize("name", OVERRIDES)
def test_an_override_that_is_set_says_so_at_every_start_needed_or_not(settings, entry, capsys, name: str) -> None:
    """Set once for a demo, or to get past one refusal, and then forgotten. Nothing is
    missing today, so nothing else would mention it; and on the day a setting is lost the
    gate it switches off would let the server start on storage that is wiped."""
    cfg = settings({**PRODUCTION, name: "1"})
    notes = check_start(cfg)
    assert len(notes) == 1 and notes[0].startswith(f"WARNING: {name} is on") and "REMOVE IT" in notes[0]

    video_app.main()
    err = capsys.readouterr().err
    assert len(entry.served) == 1, "it starts: nothing is wrong today"
    assert f"overrides={name}" in err, "the first line of every start names it"
    assert f"WARNING: {name} is on" in err


def test_with_no_override_set_the_first_line_says_none(settings, entry, capsys) -> None:
    settings(PRODUCTION)
    video_app.main()
    err = capsys.readouterr().err
    assert "overrides=none" in err and " is on" not in err and "REMOVE IT" not in err


def test_every_override_at_once_is_named_one_by_one(settings) -> None:
    cfg = settings({**PRODUCTION, **{name: "1" for name in OVERRIDES}})
    notes = check_start(cfg)
    assert [n.split(" is on")[0] for n in notes] == [f"WARNING: {name}" for name in OVERRIDES]
    assert config.overrides_on(cfg) == list(OVERRIDES)


def test_the_throwaway_override_in_use_still_shouts_as_before(settings) -> None:
    cfg = settings({**RAILWAY, "VIDEO_ALLOW_THROWAWAY_STORAGE": "1", "VIDEO_EMAIL_MODE": "off", "FR_WRITES": "off"})
    notes = check_start(cfg)
    assert "THIS SERVER LOSES ITS DATA AT THE NEXT RESTART" in notes[0]
    assert not any("nothing needs it" in n for n in notes), "it is needed here, and the first line says what it costs"


# -- a database address that can never work ends in plain words, with no password ------


@pytest.mark.parametrize("url", [
    "postgresql+psycopg2://postgres:made-up-db-password@postgres.railway.internal:5432/railway",
    "postgresql+nothing://postgres:made-up-db-password@postgres.railway.internal:5432/railway",
    "postgresql://postgres:made-up-db-password@postgres.railway.internal:port/railway",
    "postgresql://postgres:made-up-db-password@postgres.railway.internal:5432x/railway",
])
def test_a_postgres_address_this_server_cannot_use_is_caught_at_the_first_gate(settings, entry, capsys, url: str) -> None:
    """Another driver's name, or a port that is not a number. Each used to get through the
    settings gate and end as a stack of code."""
    cfg = settings({**PRODUCTION, "VIDEO_DATABASE_URL": url})
    assert "VIDEO_DATABASE_URL is not a Postgres address this server can use" in refusal(cfg)
    with pytest.raises(SystemExit) as stopped:
        video_app.main()
    assert stopped.value.code == 1 and entry.served == [] and entry.asked == []
    err = capsys.readouterr().err
    assert err.count("zest-video is NOT starting.") == 1 and "Traceback" not in err
    assert not any(value in err for value in VALUES)


@pytest.mark.parametrize("url", [
    "postgresql+nothing://postgres:made-up-db-password@db.internal:5432/zest",
    "postgresql://postgres:made-up-db-password@db.internal:port/zest",
    "nothing-at-all://postgres:made-up-db-password@db.internal/zest",
    "postgres:made-up-db-password@db.internal/zest",
])
def test_on_a_laptop_an_address_that_cannot_be_used_is_one_plain_line(settings, entry, capsys, url: str) -> None:
    """Off Railway the settings gate only warns, so the address reaches the code that
    builds the database connection. Whatever that code raises, the log gets one line."""
    settings({"VIDEO_DATABASE_URL": url})
    with pytest.raises(SystemExit) as stopped:
        video_app.main()
    assert stopped.value.code == 1 and entry.served == []
    err = capsys.readouterr().err
    assert "zest-video: NOT starting: " in err and "VIDEO_DATABASE_URL" in err
    assert "Traceback" not in err and "made-up-db-password" not in err


def test_an_address_the_driver_will_never_take_is_refused_at_once_not_waited_for(settings, entry, monkeypatch, capsys) -> None:
    """`?sslmode=require` is the usual one. The driver refuses the word itself, so no
    amount of waiting helps; it used to be asked again for five minutes as a database
    that was slow to come up."""
    settings({**PRODUCTION, "VIDEO_DATABASE_URL": DURABLE["VIDEO_DATABASE_URL"] + "?sslmode=require"})

    async def refuse_the_word(engine: Any) -> None:
        entry.asked.append(engine.url)
        raise TypeError("connect() got an unexpected keyword argument 'sslmode'")

    monkeypatch.setattr(db, "create_all", refuse_the_word)
    monkeypatch.setattr(video_app, "DATABASE_PATIENCE", 0.3)  # so that a wait, if there is one, is a short one
    with pytest.raises(SystemExit) as stopped:
        video_app.main()
    assert stopped.value.code == 1 and entry.served == []
    assert len(entry.asked) == 1, "one try, no waiting"
    err = capsys.readouterr().err
    assert "The database address cannot be used as it is written." in err and "sslmode" in err
    assert "VIDEO_DATABASE_URL" in err and "not answering yet" not in err
    assert "made-up-db-password" not in err and "Traceback" not in err


def test_a_reason_is_cut_clean_of_every_secret_before_it_is_shortened(settings) -> None:
    cfg = settings({
        **PRODUCTION, "RESEND_API_KEY": "made-up-mail-key", "VIDEO_SECRET": "made-up-signing-secret",
        "VIDEO_DATABASE_URL": "postgresql://postgres:made%2Dup%2Ddb-password@postgres.railway.internal:5432/railway",
    })
    line = (
        "could not connect to postgresql+asyncpg://postgres:made%2Dup%2Ddb-password@postgres.railway.internal:5432 "
        "(password made-up-db-password) with made-up-secret-key, made-up-mail-key and made-up-signing-secret, "
        "nor to https://someone:another-password@elsewhere.example.com"
    )
    said = config.hush(line, cfg)
    for secret in ("made%2Dup%2Ddb-password", "made-up-db-password", "made-up-secret-key", "made-up-mail-key",
                   "made-up-signing-secret", "another-password", "someone"):
        assert secret not in said, secret
    assert "could not connect to" in said and "elsewhere.example.com" in said, "the words around them stay"


# -- a stop signal during the wait for the database -------------------------------------


def test_the_wait_for_the_database_listens_for_the_stop_signal_itself(settings, entry, monkeypatch) -> None:
    """In a container the server is process number 1, and a process number 1 is only sent
    a stop signal it has asked to hear. During the wait nothing had asked."""
    settings(PRODUCTION)
    during: list[Any] = []
    real = db.create_all

    async def note_who_listens(engine: Any) -> None:
        during.append(signal.getsignal(signal.SIGTERM))
        await real(engine)

    monkeypatch.setattr(db, "create_all", note_who_listens)
    before = signal.getsignal(signal.SIGTERM)
    video_app.main()
    (listening,) = during
    assert callable(listening), "while it waits, the stop signal has someone to hear it"
    with pytest.raises(SystemExit) as stopped:
        listening(signal.SIGTERM, None)
    assert stopped.value.code == 143, "and it ends the process, with the code a stop signal gives"
    assert signal.getsignal(signal.SIGTERM) == before, "the web server is handed back what was there"


def test_a_stop_signal_ends_the_wait_even_where_it_would_otherwise_be_ignored(tmp_path) -> None:
    """The real process, waiting on a database that is not there, started with the stop
    signal ignored. That is how a container's first process stands until it asks to hear
    the signal, and it needs no container to try."""
    port = _free_port()
    log = tmp_path / "server.log"
    with log.open("w") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "zest_video.app"], cwd=tmp_path, stdout=subprocess.DEVNULL, stderr=out,
            preexec_fn=lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN),
            env={
                "PATH": os.environ.get("PATH", ""), "PORT": str(port), "PYTHONUNBUFFERED": "1", "NO_PROXY": "*",
                "FR_AUTH_KEY": "ci", "FR_AUTH_TOKEN": "ci", "FR_BASE_URL": "http://127.0.0.1:9/api",
                **PRODUCTION,
                "VIDEO_DATABASE_URL": "postgresql://postgres:made-up-db-password@127.0.0.1:1/railway",
            },
        )
    try:
        deadline = time.monotonic() + 60
        while "not answering yet" not in log.read_text() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert proc.poll() is None and "not answering yet" in log.read_text(), log.read_text()
        proc.send_signal(signal.SIGTERM)
        try:
            code = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pytest.fail("ten seconds after the stop signal the server was still waiting for its database")
    finally:
        proc.kill()
    err = log.read_text()
    assert code == 143, err
    assert "Traceback" not in err and "made-up-db-password" not in err
    assert not _listening(port)


# -- these tests on a machine that is itself on Railway ---------------------------------


def test_these_tests_do_not_take_the_machines_own_settings_for_the_servers(tmp_path) -> None:
    """Run through `railway run`, or on a box Railway hosts, the environment holds
    Railway's own variables. The tests must not be taken for a real deployment: one test
    from each of this area's two files is run that way, as its own pytest. So is one from
    each of two files that do nothing about it themselves: for those it is the shared
    helper (`video_helpers.make_env`) that leaves the machine's own settings out."""
    repo = pathlib.Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if k != "VIDEO_TEST_DATABASE_URL"}
    env.update(RAILWAY, VIDEO_START_WITH_FILES_MISSING="1", VIDEO_START_WITH_NEW_DATABASE="1",
               VIDEO_ALLOW_THROWAWAY_STORAGE="1")
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:logging",
         f"--basetemp={tmp_path / 'inner'}",
         "tests/test_video_durable.py::test_a_storage_that_cannot_be_asked_is_let_through",
         "tests/test_video_restart.py::test_a_restart_does_not_reopen_the_techs_ten_minutes",
         "tests/test_video_app.py::test_the_phone_never_receives_contact_or_billing_data",
         "tests/test_video_route_day.py::test_a_reshuffled_route_changes_the_order_on_the_phone_and_nothing_else"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300,
    )
    assert run.returncode == 0 and "4 passed" in run.stdout, run.stdout[-3000:] + run.stderr[-2000:]


async def test_the_shared_test_helper_leaves_the_machines_own_settings_out(tmp_path, monkeypatch) -> None:
    for name, value in {**RAILWAY, "VIDEO_START_WITH_FILES_MISSING": "1", "VIDEO_START_WITH_NEW_DATABASE": "1",
                        "VIDEO_ALLOW_THROWAWAY_STORAGE": "1"}.items():
        monkeypatch.setenv(name, value)
    assert config.load().platform == "Railway", "the machine itself does look like Railway here"
    env = await make_env(tmp_path)
    try:
        cfg = env.ctx.cfg
        assert (cfg.platform, cfg.real_deployment) == ("", False)
        assert (cfg.allow_throwaway_storage, cfg.start_with_files_missing, cfg.start_with_new_database) == (False,) * 3
        assert config.overrides_on(cfg) == []
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()

    # The one thing that is let through: the demo override, for the browser rig when it is
    # handed a public https address on purpose (tests/browser/serve.py --base-url).
    (tmp_path / "rig").mkdir()
    monkeypatch.delenv("VIDEO_ALLOW_THROWAWAY_STORAGE")
    if not os.environ.get("VIDEO_TEST_DATABASE_URL"):  # its records are a file here, so without the override it is refused
        with pytest.raises(StartRefused):
            await make_env(tmp_path / "rig", base_url="https://zest-made-up.trycloudflare.com")
    monkeypatch.setenv("VIDEO_ALLOW_THROWAWAY_STORAGE", "1")
    rig = await make_env(tmp_path / "rig", base_url="https://zest-made-up.trycloudflare.com")
    try:
        assert rig.ctx.cfg.allow_throwaway_storage and rig.ctx.cfg.platform == ""
    finally:
        await rig.http.aclose()
        await rig.ctx.engine.dispose()


# == the real process, started the way Railway starts it ================================


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spawn(tmp_path, port: int, env: dict[str, str]) -> subprocess.Popen:
    """`zest-video` as its own process, with nothing inherited but the path. FieldRoutes
    is pointed at a dead local port so that no test ever calls out."""
    base = {
        "PATH": os.environ.get("PATH", ""),
        "PORT": str(port),
        "PYTHONUNBUFFERED": "1",
        "NO_PROXY": "*",
        "FR_AUTH_KEY": "ci",
        "FR_AUTH_TOKEN": "ci",
        "FR_BASE_URL": "http://127.0.0.1:9/api",
    }
    return subprocess.Popen(
        [sys.executable, "-m", "zest_video.app"], cwd=tmp_path, env={**base, **env},
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_the_process_itself_refuses_to_start_on_railway_without_its_bucket(tmp_path) -> None:
    port = _free_port()
    env = {k: v for k, v in PRODUCTION.items() if k != "VIDEO_S3_BUCKET"}
    proc = _spawn(tmp_path, port, env)
    try:
        code = proc.wait(timeout=60)
    finally:
        proc.kill()
    err = proc.stderr.read()
    assert code == 1, err
    assert "zest-video is NOT starting." in err and "VIDEO_S3_BUCKET is missing or empty" in err
    assert "Traceback" not in err, "one plain message, not a stack of code"
    assert not any(value in err for value in VALUES)
    assert not _listening(port), "it never listened, so the health check can never pass"
    assert not list(tmp_path.iterdir()), "and it left no database file behind"


def test_the_process_starts_on_a_laptop_and_its_health_check_passes(tmp_path) -> None:
    """The same start CI's Docker smoke test makes: a SQLite file, no bucket, no Railway."""
    port = _free_port()
    proc = _spawn(tmp_path, port, {"VIDEO_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/local.db"})
    try:
        answer, deadline = None, time.monotonic() + 60
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as http:
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    answer = http.get("/healthz")
                    break
                except httpx.TransportError:
                    time.sleep(0.2)
            assert answer is not None and (answer.status_code, answer.text) == (200, "ok")
            assert http.get("/privacy").status_code == 200
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
    err = proc.stderr.read()
    assert "videos=MEMORY" in err and "WARNING, fine on a laptop" in err, "it says what it is running on"
    assert "start-up checks passed" in err
    written = {p.name for p in tmp_path.iterdir()}
    assert written and all(name.startswith("local.db") for name in written), (
        f"the only thing it wrote to disk is the database it was told to use, not {written}"
    )


def test_the_server_never_writes_a_file_of_its_own() -> None:
    """Videos go from the phone straight to the bucket and records go to the database.
    If the server ever starts keeping a file on its own disk, that file is lost at the
    next deploy. This reads the server's source for the ways a file gets written."""
    source = pathlib.Path(video_app.__file__).parent
    for path in sorted(source.glob("*.py")):
        text = path.read_text()
        for way in ("open(", "write_text", "write_bytes", "tempfile", "shutil", "mkdir(", "NamedTemporaryFile"):
            assert way not in text, f"{path.name} uses {way}: nothing may be kept on the container's own disk"


class _BucketOnThisMachine(http.server.BaseHTTPRequestHandler):
    """A bucket that is reachable and takes any key. It holds the files named in `holds`
    (none unless told). With `says` set to one of the storage's own error codes, it
    answers everything with that instead."""

    asked: list[str] = []
    says = "NoSuchKey"
    holds: tuple[str, ...] = ()

    def do_GET(self) -> None:  # noqa: N802 - the name http.server looks for
        path, _, question = self.path.partition("?")
        self.asked.append(path)
        if self.says == "NoSuchKey" and "list-type=2" in question:
            listed = "".join(f"<Contents><Key>{key}</Key><Size>5000</Size></Contents>" for key in self.holds[:1])
            status, body = 200, (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>zest-videos</Name>'
                f"<Prefix>videos/</Prefix><KeyCount>{len(self.holds[:1])}</KeyCount><MaxKeys>1</MaxKeys>"
                f"<IsTruncated>false</IsTruncated>{listed}</ListBucketResult>"
            ).encode()
        elif self.says == "NoSuchKey" and path.split("/", 2)[-1] in self.holds:
            status, body = 206, b"v"  # the one byte that was asked for
        else:
            status = 404
            body = f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{self.says}</Code></Error>'.encode()
        self.send_response(status)
        self.send_header("Content-Type", "video/mp4" if status == 206 else "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:
        pass


@contextlib.contextmanager
def bucket_on_this_machine(says: str = "NoSuchKey", holds: tuple[str, ...] = ()):
    _BucketOnThisMachine.asked, _BucketOnThisMachine.says, _BucketOnThisMachine.holds = [], says, holds
    bucket = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _BucketOnThisMachine)
    threading.Thread(target=bucket.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{bucket.server_address[1]}"
    finally:
        bucket.shutdown()
        bucket.server_close()
        _BucketOnThisMachine.says, _BucketOnThisMachine.holds = "NoSuchKey", ()


def test_the_process_refused_at_the_bucket_says_so_once_in_plain_words(tmp_path) -> None:
    """The bucket is checked inside the web server's own start-up. A refusal there used to
    come out as the plain message, then a stack of code with the same message in it a
    second time. Here the storage says there is no bucket by the name in the settings."""
    port = _free_port()
    with bucket_on_this_machine("NoSuchBucket") as endpoint:
        proc = _spawn(tmp_path, port, {
            **PRODUCTION,
            # No Postgres on this machine: a file, let through by name as a demo would be.
            "VIDEO_DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path}/demo.db",
            "VIDEO_ALLOW_THROWAWAY_STORAGE": "1",
            "VIDEO_S3_ENDPOINT": endpoint,
            "VIDEO_S3_PATH_STYLE": "1",
        })
        try:
            code = proc.wait(timeout=60)
        finally:
            proc.kill()
    err = proc.stderr.read()
    assert code not in (0, None), err
    assert err.count("zest-video is NOT starting.") == 1, f"the message, once:\n{err}"
    assert "The storage has no bucket with the name in VIDEO_S3_BUCKET." in err
    assert "Traceback" not in err and 'File "' not in err, "plain words, not a stack of code"
    assert not any(value in err for value in VALUES)
    assert "overrides=VIDEO_ALLOW_THROWAWAY_STORAGE" in err, "and the override it ran under is named in the first line"
    assert _BucketOnThisMachine.asked == [f"/zest-videos/{video_app.BUCKET_PROBE_KEY}"]
    assert not _listening(port), "it never listened"


@pytest.mark.skipif(not os.environ.get("VIDEO_TEST_DATABASE_URL"), reason="needs Postgres (CI's Postgres job has it)")
async def test_the_process_starts_as_it_is_set_up_in_production_on_a_real_postgres(tmp_path) -> None:
    """The nearest thing to the deployed server that a test may touch: the real process,
    Railway's variables, production's variable names, a real Postgres, and a bucket that
    answers over HTTP."""
    env = await make_env(tmp_path)  # a clean database
    await env.http.aclose()
    await env.ctx.engine.dispose()
    port = _free_port()
    with bucket_on_this_machine() as endpoint:
        proc = _spawn(tmp_path, port, {
            **PRODUCTION,
            # Railway hands the address out without the driver's name in it.
            "VIDEO_DATABASE_URL": os.environ["VIDEO_TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"),
            "VIDEO_S3_ENDPOINT": endpoint,
            "VIDEO_S3_PATH_STYLE": "1",
        })
        try:
            answer, deadline = None, time.monotonic() + 60
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as http_:
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        answer = await http_.get("/healthz")
                        break
                    except httpx.TransportError:
                        await asyncio.sleep(0.2)
            assert answer is not None and (answer.status_code, answer.text) == (200, "ok")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
    err = proc.stderr.read()
    assert "where=Railway" in err and "records=Postgres" in err and "videos=bucket" in err
    assert "overrides=none" in err
    assert "start-up checks passed. Records: Postgres, answering. Videos: the bucket answers" in err
    # (This used to pin one read. The second asks the bucket whether it holds any video,
    # which is what catches a new database beside a full bucket.)
    assert _BucketOnThisMachine.asked == [f"/zest-videos/{video_app.BUCKET_PROBE_KEY}", "/zest-videos/"], (
        "two small reads, nothing written"
    )
    assert "would not say" not in err, "and the real list call, made over HTTP, was understood"
    assert "WARNING, fine on a laptop" not in err and "NOT starting" not in err
    assert not any(secret in err for secret in SECRETS)


# What the live database can look like on the day this version is pushed, and what the
# real process does on each. The live server has only ever held test and demo videos, so
# the states differ in what is left of those. (docs/video/GO-LIVE.md has the same table.)
LEFT_OVER = "videos/2026-11-12/11111111-1111-4111-8111-111111111111.mp4"  # a test file nobody cleared out
LIVE_STATES = {
    # state: (what is in the database, what is in the bucket, an override, does it start)
    "demo videos, the bucket cleared": ("demo videos", "nothing", "", True),
    "demo videos, old files in the bucket": ("demo videos", "old files", "", True),
    "test videos, one of their files still there": ("test videos", "one of theirs", "", True),
    "no video at all, an empty bucket": ("nothing", "nothing", "", True),
    "no video, phones signed in, old files in the bucket": ("sign-ins", "old files", "", True),
    "no video, nobody ever signed in, old files in the bucket": ("nothing", "old files", "", False),
    "the same, started by name": ("nothing", "old files", "VIDEO_START_WITH_NEW_DATABASE", True),
    "three test videos, every file cleared out by hand": ("test videos", "nothing", "", False),
    "the same, started by name ": ("test videos", "nothing", "VIDEO_START_WITH_FILES_MISSING", True),
}


@pytest.mark.skipif(not os.environ.get("VIDEO_TEST_DATABASE_URL"), reason="needs Postgres (CI's Postgres job has it)")
@pytest.mark.parametrize("state", LIVE_STATES)
async def test_the_process_on_each_state_the_live_database_can_be_in(tmp_path, state: str) -> None:
    """Every state starts but two, and each of those is refused once, in plain words that
    name the way past it: a database with no video that no phone has ever signed in on,
    beside a bucket that holds video files (a new or an emptied database); and three or
    more test videos whose files are all gone (which looks exactly like the wrong bucket)."""
    in_database, in_bucket, override, starts = LIVE_STATES[state]
    env = await make_env(tmp_path)  # a clean database, with its tables
    holds: tuple[str, ...] = (LEFT_OVER,) if in_bucket == "old files" else ()
    if in_database == "demo videos":
        headers = await demo_login(env)  # the App Review account: its phone signs in, and films a sample stop
        await env.http.get("/v1/today", headers=headers)
        await film(env, headers, -101)
    elif in_database == "test videos":
        ids = await three_videos(env)
        if in_bucket == "one of theirs":
            holds = ((await env.video(ids[0])).object_key,)
    elif in_database == "sign-ins":
        await login(env)
    await env.http.aclose()
    await env.ctx.engine.dispose()
    port = _free_port()
    with bucket_on_this_machine(holds=holds) as endpoint:
        proc = _spawn(tmp_path, port, {
            **PRODUCTION,
            "VIDEO_DATABASE_URL": os.environ["VIDEO_TEST_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://"),
            "VIDEO_S3_ENDPOINT": endpoint,
            "VIDEO_S3_PATH_STYLE": "1",
            **({override: "1"} if override else {}),
        })
        try:
            answer, deadline = None, time.monotonic() + 60
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5) as http_:
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        answer = await http_.get("/healthz")
                        break
                    except httpx.TransportError:
                        await asyncio.sleep(0.2)
            started = answer is not None and (answer.status_code, answer.text) == (200, "ok")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
    err = proc.stderr.read()
    assert "Traceback" not in err and not any(secret in err for secret in SECRETS)
    assert started == starts, f"{state}\n{err}"
    if not starts:
        assert proc.returncode not in (0, None, -signal.SIGTERM), err
        assert err.count("zest-video is NOT starting.") == 1, f"the message, once:\n{err}"
        if in_database == "nothing":
            assert "The bucket holds videos, and the database does not know of one video, and no phone has ever signed in on it" in err
            assert "VIDEO_DATABASE_URL" in err and "VIDEO_START_WITH_NEW_DATABASE=1" in err
        else:
            assert "The bucket holds none of the newest 3 videos the database knows" in err
            assert "VIDEO_S3_BUCKET" in err and "VIDEO_START_WITH_FILES_MISSING=1" in err
        return
    assert "NOT starting" not in err and "start-up checks passed. Records: Postgres, answering." in err
    assert f"overrides={override or 'none'}" in err
    if override == "VIDEO_START_WITH_NEW_DATABASE":
        assert "REMOVE IT once the first phone has signed in" in err
        assert "WARNING: The bucket holds videos, and the database does not know of one video" in err
    elif override:
        assert "WARNING: The bucket holds none of the newest 3 videos the database knows" in err
    elif in_database == "sign-ins":
        assert "WARNING: the bucket holds video files, and this database knows of no video. Phones have signed in" in err
    elif in_database == "test videos":
        assert "Videos: the bucket answers and holds the newest videos (1 of 3 looked for).\n" in err
    elif in_database == "demo videos":
        assert "Videos: the bucket answers (no customer videos to look for yet).\n" in err, "and no warning about the bucket"
    else:
        assert "Videos: the bucket answers (the database has no videos yet).\n" in err, "and no warning about the bucket"


# == gates 2 and 3, and the health check ===============================================


@pytest.fixture
async def env(tmp_path) -> Env:
    e = await make_env(tmp_path)
    yield e
    await e.http.aclose()
    await e.ctx.engine.dispose()


class StartFailed(Exception):
    raised: BaseException | None = None


@contextlib.asynccontextmanager
async def running(app: Any):
    """Start an app the way uvicorn does (the ASGI lifespan) and stop it on the way out.
    Raises StartFailed when the start-up does not complete."""
    to_app: asyncio.Queue = asyncio.Queue()
    from_app: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0"}, "state": {}}, to_app.get, from_app.put))
    await to_app.put({"type": "lifespan.startup"})
    first = await from_app.get()
    if first["type"] != "lifespan.startup.complete":
        (raised,) = await asyncio.gather(task, return_exceptions=True)
        failed = StartFailed(first.get("message", ""))  # the text the web server is handed to print
        failed.raised = raised  # and what, if anything, the app raised into the web server
        raise failed
    await asyncio.sleep(0.01)  # let the job loop take its first step, as it would under uvicorn
    try:
        yield
    finally:
        await to_app.put({"type": "lifespan.shutdown"})
        await from_app.get()
        await asyncio.gather(task, return_exceptions=True)


@pytest.fixture
def job_starts(monkeypatch) -> list[Any]:
    """Stands in for the job loop and records each time one is started."""
    started: list[Any] = []

    async def loop(ctx: Any, **kwargs: Any) -> None:
        started.append(ctx)
        await asyncio.Event().wait()

    monkeypatch.setattr(jobs, "run_forever", loop)
    return started


def test_a_database_that_is_slow_to_come_up_is_waited_for_before_anything_is_served(settings, entry, capsys) -> None:
    """Postgres restarting beside the server must not leave the server down for good."""
    settings(PRODUCTION)
    entry.answers_after = 3
    video_app.main()
    assert len(entry.asked) == 4 and len(entry.served) == 1, "it asked again until the database answered, then served"
    err = capsys.readouterr().err
    assert err.count("the database is not answering yet (ConnectionRefusedError") == 3
    assert "made-up-db-password" not in err, "the password is cut out of the reason"


def test_a_database_that_never_answers_ends_the_start_in_plain_words(settings, entry, monkeypatch, capsys) -> None:
    settings(PRODUCTION)
    entry.answers_after = None
    monkeypatch.setattr(video_app, "DATABASE_PATIENCE", 0.05)
    with pytest.raises(SystemExit) as stopped:
        video_app.main()
    assert stopped.value.code == 1 and entry.served == [] and len(entry.asked) > 1
    err = capsys.readouterr().err
    assert "zest-video is NOT starting." in err and "The database did not answer" in err
    assert "VIDEO_DATABASE_URL" in err and "phones keep their videos" in err
    assert "made-up-db-password" not in err


async def test_a_database_gone_at_the_last_moment_stops_the_start_and_no_job_runs(
    env: Env, monkeypatch, capsys, job_starts
) -> None:
    """The wait is over and the web server is starting. Here the database gets one try."""
    tries = []

    async def gone(engine: Any) -> None:
        tries.append(1)
        raise OSError("connection reset")

    monkeypatch.setattr(db, "create_all", gone)
    with pytest.raises(StartFailed):
        async with running(video_app.build_app(env.ctx)):
            pytest.fail("the server must not come up without its database")
    assert tries == [1], "no waiting here: a web server that is starting cannot be told to stop"
    assert "zest-video is NOT starting." in capsys.readouterr().err
    assert job_starts == []


def with_a_password_in_its_address(settings) -> Any:
    """A server whose database address holds a password, as the real one's does. Nothing
    is connected to: the test stands in for the database's answer."""
    from dataclasses import replace

    from conftest import FakeFR, _client_for
    from video_helpers import seed

    from zest_video.notify import MemoryMailer, MemoryPusher, MemoryTexter

    cfg = replace(
        settings({}), base_url="https://video.test",
        database_url="postgresql+asyncpg://postgres:made-up-db-password@127.0.0.1:1/railway",
    )
    return video_app.build_ctx(
        cfg, fr_client=_client_for(FakeFR(data=seed())), storage=MemoryStorage(), mailer=MemoryMailer(),
        texter=MemoryTexter(), pusher=MemoryPusher(),
    )


async def test_a_refusal_inside_the_web_servers_start_up_is_said_once_with_no_stack_and_no_password(
    settings, monkeypatch, capsys, job_starts
) -> None:
    """The web server prints whatever text a failed start-up hands it. That text used to be
    the whole stack of code, with the first failure chained in, in full: the same message
    a second time, and the password that had been so carefully cut out of the first."""
    ctx = with_a_password_in_its_address(settings)

    async def gone(engine: Any) -> None:
        raise ConnectionRefusedError("connection refused: postgres.railway.internal:5432 made-up-db-password")

    monkeypatch.setattr(db, "create_all", gone)
    with pytest.raises(StartFailed) as failed:
        async with running(video_app.build_app(ctx)):
            pytest.fail("the server must not come up without its database")
    await ctx.engine.dispose()
    handed_over = str(failed.value)
    assert handed_over == "", "the web server is handed nothing more to print"
    refusal_raised = failed.value.raised
    assert isinstance(refusal_raised, StartRefused), "the start still fails, so the process still exits with an error"
    assert refusal_raised.__cause__ is None and refusal_raised.__suppress_context__, (
        "and the first failure is not chained to it, to be printed in full by whatever prints this one"
    )
    err = capsys.readouterr().err
    assert err.count("zest-video is NOT starting.") == 1 and "The database did not answer" in err
    assert "ConnectionRefusedError: connection refused: postgres.railway.internal:5432 ***" in err
    for text in (err, handed_over, refusal_raised.message):
        assert "made-up-db-password" not in text and "Traceback" not in text
    assert job_starts == []


async def test_anything_else_going_wrong_in_the_start_up_also_ends_in_plain_words(
    settings, monkeypatch, capsys, job_starts
) -> None:
    ctx = with_a_password_in_its_address(settings)

    async def answers(engine: Any) -> None:
        return None

    async def breaks(ctx: Any) -> None:
        raise RuntimeError("could not read office_users at postgresql://postgres:made-up-db-password@db/railway")

    monkeypatch.setattr(db, "create_all", answers)
    monkeypatch.setattr(auth, "ensure_owners", breaks)
    with pytest.raises(StartFailed) as failed:
        async with running(video_app.build_app(ctx)):
            pytest.fail("the server must not come up with its start-up checks unfinished")
    await ctx.engine.dispose()
    err = capsys.readouterr().err
    assert err.count("zest-video is NOT starting.") == 1 and "The start-up checks could not be finished." in err
    assert "RuntimeError: could not read office_users" in err, "what went wrong is still said"
    assert str(failed.value) == "" and "made-up-db-password" not in err and "Traceback" not in err
    assert job_starts == []


def test_the_start_gives_the_database_long_enough_to_restart_and_not_forever() -> None:
    assert 120 <= video_app.DATABASE_PATIENCE <= 600
    assert video_app.BUCKET_WAIT <= 15, "and the bucket never holds a restart up for long"


# -- gate 3: the bucket ---------------------------------------------------------------


FRONT_DOOR = "front door 404"
# Three more answers that are not the bucket's own word about a file, each with a 200 on it:
PAGE_200 = "a web page with a 200"  # "we will be right back", from whatever stands in front of the bucket
WEAK_ETAG = "the file, its ETag marked weak"  # the right file behind something that rewrites the header to W/"..."
NO_ETAG = "the file, its ETag dropped"  # the right file behind something that drops the header


class FakeBucket:
    """A stand-in bucket that speaks the real one's wire dialect, so `S3Storage` itself is
    what runs. `mode` is "ok", "down" (cannot be reached), "timeout", "error" (a bad minute),
    FRONT_DOOR (a plain "not found" page from whatever stands in front of the bucket, with
    none of the bucket's own words in it), PAGE_200, WEAK_ETAG, NO_ETAG, or one of the
    storage's own error codes. `kind` is the Content-Type it gives a file ("" for none).

    `lose` holds the numbers of the requests (1 is the first) whose answer is lost to a
    503. `listing` is how a list call is answered: "ok", or an error code."""

    def __init__(self, objects: dict[str, bytes]):
        self.objects = objects
        self.mode = "ok"
        self.slow = 0.0
        self.asked: list[str] = []
        self.lose: set[int] = set()
        self.listing = "ok"
        self.listed: list[dict[str, str]] = []
        self.kind = ""

    async def handler(self, request: httpx.Request) -> httpx.Response:
        self.asked.append(f"{request.method} {request.url.path}")
        if self.slow:
            await asyncio.sleep(self.slow)
        if self.mode == "down":
            raise httpx.ConnectError("no route to the bucket")
        if self.mode == "timeout":
            raise httpx.ReadTimeout("the bucket took too long to answer")
        if self.mode == "error" or len(self.asked) in self.lose:
            return httpx.Response(503, text="<Error><Code>SlowDown</Code></Error>")
        if self.mode == FRONT_DOOR:
            if request.method == "HEAD":
                return httpx.Response(404)
            return httpx.Response(404, text="<html><body><h1>Application not found</h1></body></html>",
                                  headers={"content-type": "text/html"})
        if self.mode == PAGE_200:
            page = b"<html><body>We will be right back</body></html>"
            headers = {"content-type": "text/html; charset=utf-8", "content-length": str(len(page)), "etag": '"its-own-etag"'}
            return httpx.Response(200, headers=headers) if request.method == "HEAD" else httpx.Response(200, content=page, headers=headers)
        if self.mode not in ("ok", WEAK_ETAG, NO_ETAG):
            status = 404 if self.mode == "NoSuchBucket" else 400 if self.mode.startswith("Authorization") else 403
            return self._error(status, self.mode)
        if request.url.path == "/" and request.url.params.get("list-type") == "2":
            return self._list(dict(request.url.params))
        body = self.objects.get(request.url.path.lstrip("/"))
        if body is None:
            return self._error(404, "NoSuchKey")
        etag = hashlib.md5(body, usedforsecurity=False).hexdigest()
        about = {"etag": f'W/"{etag}"' if self.mode == WEAK_ETAG else f'"{etag}"'}
        if self.mode == NO_ETAG:
            about = {}
        if self.kind:
            about["content-type"] = self.kind
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(body)), **about})
        return httpx.Response(206, content=body[:1], headers=about)

    def _list(self, params: dict[str, str]) -> httpx.Response:
        """The bucket's answer to "what do you hold under this prefix?" (ListObjectsV2)."""
        self.listed.append(params)
        if self.listing != "ok":
            return self._error(403, self.listing)
        prefix, most = params.get("prefix", ""), int(params.get("max-keys", "1000"))
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>zest-videos</Name>'
            f"<Prefix>{prefix}</Prefix><KeyCount>{len(keys[:most])}</KeyCount><MaxKeys>{most}</MaxKeys>"
            f"<IsTruncated>{'true' if len(keys) > most else 'false'}</IsTruncated>"
            + "".join(f"<Contents><Key>{key}</Key><Size>5000</Size></Contents>" for key in keys[:most])
            + "</ListBucketResult>"
        )
        return httpx.Response(200, text=xml, headers={"content-type": "application/xml"})

    @staticmethod
    def _error(status: int, code: str) -> httpx.Response:
        xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code><Message>made up</Message></Error>'
        return httpx.Response(status, text=xml, headers={"content-type": "application/xml"})


async def deployed(tmp_path, **cfg: Any) -> tuple[Env, FakeBucket]:
    """A server set up as the real one is (on Railway, with a bucket), over the fakes."""
    env = await make_env(
        tmp_path, s3_endpoint="https://storage.example.com", s3_bucket="zest-videos",
        s3_access_key="made-up-key-id", s3_secret_key="made-up-secret-key", **cfg,
    )
    bucket = FakeBucket(env.storage.objects)  # `film` puts files in env.storage; the fake bucket serves them
    env.ctx.storage = S3Storage(env.ctx.cfg, transport=httpx.MockTransport(bucket.handler))
    env.reconfigure(platform="Railway")  # the test database stands in for Postgres
    assert env.ctx.cfg.real_deployment
    return env, bucket


@pytest.fixture
async def live(tmp_path):
    env, bucket = await deployed(tmp_path)
    yield env, bucket
    await env.http.aclose()
    await env.ctx.engine.dispose()


async def three_videos(env: Env) -> list[str]:
    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    env.mailer.sent.clear()
    return [await film(env, headers, customer) for customer in (501, 502, 503)]


async def test_the_right_bucket_passes_and_the_log_says_what_was_looked_for(live, capsys, job_starts) -> None:
    env, bucket = live
    await three_videos(env)
    bucket.asked.clear()
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert sorted(bucket.asked) == sorted(f"GET /{key}" for key in env.storage.objects), "one small read per video"
    assert "Videos: the bucket answers and holds the newest videos (3 of 3 looked for)" in capsys.readouterr().err


async def test_the_wrong_bucket_stops_the_start_before_one_video_is_touched(live, capsys, job_starts) -> None:
    """The settings now point at some other, empty bucket. Started anyway, the server would
    pull each video as its email came due or its customer opened it."""
    env, bucket = live
    ids = await three_videos(env)
    for video_id in ids:
        await env.window_closed(video_id)  # every one of them is due to be emailed right now
    bucket.objects = {}

    with pytest.raises(StartFailed):
        async with running(video_app.build_app(env.ctx)):
            pytest.fail("the server must not come up on the wrong bucket")
    err = capsys.readouterr().err
    assert "zest-video is NOT starting." in err
    assert "holds none of the newest 3 videos" in err and "VIDEO_S3_BUCKET" in err and "VIDEO_S3_ENDPOINT" in err
    assert "VIDEO_START_WITH_FILES_MISSING=1" in err, "and it says how to start anyway if the files really are gone"
    assert job_starts == [], "no job ran"
    assert {(await env.video(v)).state for v in ids} == {"held"}, "so no video was pulled"
    assert await env.flags() == [] and env.mailer.sent == []


async def test_if_the_files_really_are_gone_the_owner_can_say_start_anyway(live, capsys, job_starts) -> None:
    env, bucket = live
    await three_videos(env)
    bucket.objects = {}
    env.reconfigure(start_with_files_missing=True)
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert "WARNING: The bucket holds none of the newest 3 videos" in capsys.readouterr().err


async def test_one_or_two_missing_files_are_not_proof_of_a_wrong_bucket(live, capsys, job_starts) -> None:
    env, bucket = live
    ids = await three_videos(env)
    # Before launch there may be one test video whose file somebody cleared out by hand.
    await env.run(sa.delete(db.videos).where(db.videos.c.id.in_(ids[1:])))
    bucket.objects = {}
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert "WARNING: the bucket answers, and 1 of the newest 1 videos are not in it" in capsys.readouterr().err


async def test_a_few_files_gone_from_the_right_bucket_do_not_stop_it(live, capsys, job_starts) -> None:
    env, bucket = live
    ids = await three_videos(env)
    del env.storage.objects[(await env.video(ids[0])).object_key]
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1, "each missing file is then dealt with one by one, as before"
    assert "holds the newest videos (2 of 3 looked for)" in capsys.readouterr().err


@pytest.mark.parametrize("fault", ["down", "error", "slow", "AccessDenied", FRONT_DOOR])
async def test_a_bucket_having_a_bad_minute_never_stops_a_restart(live, capsys, job_starts, monkeypatch, fault: str) -> None:
    env, bucket = live
    ids = await three_videos(env)
    if fault == "slow":
        monkeypatch.setattr(video_app, "BUCKET_WAIT", 0.05)
        bucket.slow = 5.0
    else:
        bucket.mode = fault
    began = time.monotonic()
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert time.monotonic() - began < 3
    err = capsys.readouterr().err
    assert "WARNING: the bucket gave no clear answer" in err and "Starting anyway" in err
    assert {(await env.video(v)).state for v in ids} == {"held"}


@pytest.mark.parametrize("code, names", [
    ("NoSuchBucket", "VIDEO_S3_BUCKET"),
    ("InvalidAccessKeyId", "VIDEO_S3_ACCESS_KEY_ID"),
    ("SignatureDoesNotMatch", "VIDEO_S3_SECRET_ACCESS_KEY"),
    ("AuthorizationQueryParametersError", "VIDEO_S3_REGION"),
    ("PermanentRedirect", "VIDEO_S3_ENDPOINT"),
])
@pytest.mark.parametrize("videos_known", [True, False])
async def test_a_bucket_that_says_the_settings_are_wrong_stops_the_start(
    live, capsys, job_starts, code: str, names: str, videos_known: bool
) -> None:
    env, bucket = live
    if videos_known:
        await three_videos(env)
    bucket.mode = code
    with pytest.raises(StartFailed):
        async with running(video_app.build_app(env.ctx)):
            pytest.fail("the server must not come up with keys the storage refuses")
    err = capsys.readouterr().err
    assert "The video storage says the settings are wrong:" in err and names in err
    assert not any(secret in err for secret in SECRETS)
    assert job_starts == []


async def test_a_new_database_with_no_videos_yet_asks_whether_the_bucket_answers_and_is_empty(
    live, capsys, job_starts
) -> None:
    """A first start, before any video. (This test used to pin one read only; the second,
    "do you hold any video?", is what catches a new database beside a full bucket.)"""
    env, bucket = live
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert bucket.asked == [f"GET /{video_app.BUCKET_PROBE_KEY}", "GET /"], "two small reads, nothing written"
    err = capsys.readouterr().err
    assert "Videos: the bucket answers (the database has no videos yet)" in err and "WARNING" not in err


async def test_a_demo_video_whose_file_was_cleared_out_is_not_evidence(live, capsys, job_starts) -> None:
    """Demo videos lose their file after a day while their row stays as it was."""
    env, bucket = live
    ids = await three_videos(env)
    await env.run(sa.update(db.videos).values(demo=True))
    bucket.objects = {}
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    err = capsys.readouterr().err
    # (This line used to read "the database has no videos yet", of a database with three.)
    assert ids and "Videos: the bucket answers (no customer videos to look for yet)" in err
    assert "the database has no videos yet" not in err and "WARNING" not in err


async def test_on_a_laptop_the_same_findings_are_warnings(env: Env, capsys, job_starts) -> None:
    await three_videos(env)
    env.storage.objects.clear()
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert "WARNING: The bucket holds none of the newest 3 videos" in capsys.readouterr().err


async def test_what_the_bucket_client_makes_of_each_answer(live) -> None:
    env, bucket = live
    env.storage.objects["videos/2026-11-14/there.mp4"] = b"v" * 5000
    ask = env.ctx.storage.ask
    assert await ask("videos/2026-11-14/there.mp4") == FOUND
    assert await ask("videos/2026-11-14/not-there.mp4") == MISSING
    for mode, answer in (("down", UNKNOWN), ("error", UNKNOWN), ("AccessDenied", UNKNOWN),
                         ("NoSuchBucket", "NoSuchBucket"), ("SignatureDoesNotMatch", "SignatureDoesNotMatch"),
                         (FRONT_DOOR, UNKNOWN)):
        bucket.mode = mode
        assert await ask("videos/2026-11-14/there.mp4") == answer, mode
        assert await ask("videos/2026-11-14/not-there.mp4") == answer, f"{mode}, for a file that is not there"
    assert all(a.startswith("GET ") for a in bucket.asked), "a read of one byte; nothing is ever written to ask"
    memory = MemoryStorage()
    memory.objects["a"] = b"1"
    assert (await memory.ask("a"), await memory.ask("b")) == (FOUND, MISSING)


async def test_a_storage_that_cannot_be_asked_is_let_through(env: Env, capsys, job_starts) -> None:
    class Bare:
        """An older stand-in with no `ask`."""

    env.ctx.storage = Bare()  # type: ignore[assignment]
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert "Videos: not checked" in capsys.readouterr().err


# == while it runs: a video is never pulled on an unclear answer ==========================
#
# Before an email goes and before the player is handed a link, the server looks whether the
# bucket still holds the file it confirmed. "Pulled" takes a video away from its customer
# and kills the link, and nothing undoes that by itself. So it happens only when the bucket
# itself says, in its own words, that the file is gone (or the file was changed).


async def test_a_front_door_404_while_running_must_not_take_a_video_down(live) -> None:
    """The check asks with a HEAD, and a HEAD's "not found" carries no word from the
    bucket itself: whatever stands in front of the bucket says the same on a bad minute.
    The server now asks again in a way that does carry the bucket's word (`storage.ask`)
    and believes "not there" only then. (This was a known gap: three safe videos were
    pulled. `storage.head` and the older test that pins it are as they were.)"""
    env, bucket = live
    ids = await three_videos(env)
    for video_id in ids:
        await env.window_closed(video_id)
    bucket.mode = FRONT_DOOR
    await videos.send_due(env.ctx)
    assert {(await env.video(v)).state for v in ids} == {"held"}, "every file is safe in the bucket"
    assert await env.flags("file_changed") == []

    # Nobody was emailed a link to a file the storage would not vouch for: the emails wait,
    # and go by themselves when the storage answers properly again.
    assert env.mailer.sent == []
    bucket.mode = "ok"
    for video_id in ids:
        await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    assert sorted(m.to for m in env.mailer.sent) == ["bob@example.com", "jane@example.com"]
    assert sorted([(await env.video(v)).state for v in ids]) == ["no_email", "sent", "sent"], "Acme has no address on file"
    assert await env.flags("file_changed") == [] and await env.flags("file_doubt") == []


UNCLEAR = [FRONT_DOOR, "error", "timeout", "down"]  # a bare 404 page, a 503, no answer in time, no connection


@pytest.mark.parametrize("mode", UNCLEAR)
async def test_an_unclear_answer_before_an_email_never_pulls_the_video(live, mode: str) -> None:
    env, bucket = live
    ids = await three_videos(env)
    for video_id in ids:
        await env.window_closed(video_id)
    bucket.mode = mode
    await videos.send_due(env.ctx)
    after = [await env.video(v) for v in ids]
    assert "pulled" not in {v.state for v in after} and await env.flags("file_changed") == []
    assert all(v.object_key in env.storage.objects and not v.object_deleted for v in after), "and no file is touched"
    if mode == FRONT_DOOR:
        assert env.mailer.sent == [], "'not found' from something that is not the bucket: the email waits"
    else:
        # The bucket could not be asked at all. That says nothing about the file, and the
        # player checks again when the customer presses play, so the email goes on time.
        assert sorted(m.to for m in env.mailer.sent) == ["bob@example.com", "jane@example.com"]


@pytest.mark.parametrize("mode", UNCLEAR)
async def test_an_unclear_answer_when_a_customer_presses_play_never_pulls_the_video(live, mode: str) -> None:
    env, bucket = live
    ids = await three_videos(env)
    for video_id in ids:
        await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    jane = await env.video(ids[0])
    assert jane.state == "sent"

    bucket.mode = mode
    for _ in range(3):
        r = await env.http.get(f"/w/{jane.token}/video")
        assert r.status_code == 302, "the player is handed the link; if the file really is gone, it is the player that fails"
    assert (await env.http.get(f"/w/{jane.token}")).status_code == 200
    assert (await env.video(ids[0])).state == "sent" and await env.flags("file_changed") == []
    bucket.mode = "ok"
    assert (await env.http.get(f"/w/{jane.token}/video")).status_code == 302


async def test_a_file_the_bucket_itself_says_is_gone_is_still_pulled_and_the_office_told(live) -> None:
    """The one clear answer: the bucket took our keys and said NoSuchKey."""
    env, bucket = live
    ids = await three_videos(env)
    await env.window_closed(ids[0])
    await videos.send_due(env.ctx)
    jane, bob = await env.video(ids[0]), await env.video(ids[1])
    assert jane.state == "sent" and bob.state == "held"

    # Bob's file goes before his email is due, Jane's after hers was sent.
    del env.storage.objects[bob.object_key], env.storage.objects[jane.object_key]
    bucket.asked.clear()
    await env.window_closed(ids[1])
    await videos.send_due(env.ctx)
    assert bucket.asked[:2] == [f"HEAD /{bob.object_key}", f"GET /{bob.object_key}"], "asked twice: the second time for the bucket's own word"
    bob = await env.video(ids[1])
    assert bob.state == "pulled" and "missing from storage" in bob.ended_reason
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "no email for a video with no file"

    r = await env.http.get(f"/w/{jane.token}/video")
    assert r.status_code == 410 and "location" not in r.headers
    assert (await env.video(ids[0])).state == "pulled"
    flags = await env.flags("file_changed")
    assert {f.video_id for f in flags} == {ids[0], ids[1]} and all("is missing from storage" in f.detail for f in flags)
    assert (await env.actions()).count("video_pulled") == 2, "each written down with its pull"


async def test_the_office_is_told_when_the_storage_stays_unclear_about_one_file(live) -> None:
    """"Not found, but not in the bucket's own words" for a minute is a bad minute. For half
    an hour it may be a file that really is gone, behind a storage that words it some other
    way. The email keeps waiting, nothing is pulled, and the office is asked to look."""
    env, bucket = live
    ids = await three_videos(env)
    video_id = ids[0]
    bucket.mode = FRONT_DOOR
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    assert await env.flags("file_doubt") == [], "a bad minute is not worth a flag"

    # Forty minutes on, still the same answer.
    long_ago = db.now() - timedelta(minutes=50)
    await env.run(sa.update(db.videos).where(db.videos.c.id == video_id).values(
        created_at=long_ago, recorded_at=long_ago, uploaded_at=long_ago + timedelta(minutes=1)))
    for _ in range(3):
        await env.window_closed(video_id)
        await videos.send_due(env.ctx)
    (flag,) = await env.flags("file_doubt")
    assert flag.video_id == video_id and "Nothing was pulled and the customer has not been emailed" in flag.detail
    assert (await env.video(video_id)).state == "held" and env.mailer.sent == []

    # The storage answers again: the email goes, and the flag clears itself.
    bucket.mode = "ok"
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"] and (await env.video(video_id)).state == "sent"
    assert await env.flags("file_doubt") == []
    (cleared,) = await env.flags("file_doubt", open_only=False)
    assert cleared.cleared_by == "system" and cleared.note == "the storage answered"


# A 200 is not the bucket's word either, by itself. "The file was changed" used to be read
# off any answer whose size or ETag differed, so a page saying "we will be right back", or
# the right file behind something that drops or rewrites the ETag header, took every video
# down that came due or was played meanwhile. A video is now pulled as changed only on an
# answer that can only be the bucket's own: a video file with another ETag.

NOT_THE_BUCKETS_WORD = [PAGE_200, WEAK_ETAG, NO_ETAG]


@pytest.mark.parametrize("mode", NOT_THE_BUCKETS_WORD)
async def test_a_200_that_is_not_the_buckets_word_about_the_file_never_pulls_a_video_before_its_email(live, mode: str) -> None:
    env, bucket = live
    ids = await three_videos(env)
    for video_id in ids:
        await env.window_closed(video_id)
    bucket.mode = mode
    await videos.send_due(env.ctx)
    after = [await env.video(v) for v in ids]
    assert "pulled" not in {v.state for v in after} and await env.flags("file_changed") == []
    assert all(v.object_key in env.storage.objects and not v.object_deleted for v in after), "and no file is touched"
    if mode == WEAK_ETAG:
        # The same ETag, only marked weak: it is the confirmed file, and the emails go.
        assert sorted(m.to for m in env.mailer.sent) == ["bob@example.com", "jane@example.com"]
    else:
        # Nothing vouches for the file, so nobody is sent a link to it yet. The emails wait.
        assert env.mailer.sent == [] and {v.state for v in after} == {"held"}
        bucket.mode = "ok"
        for video_id in ids:
            await env.window_closed(video_id)
        await videos.send_due(env.ctx)
        assert sorted(m.to for m in env.mailer.sent) == ["bob@example.com", "jane@example.com"], "and go when it answers"


@pytest.mark.parametrize("mode", NOT_THE_BUCKETS_WORD)
async def test_a_200_that_is_not_the_buckets_word_about_the_file_never_pulls_a_video_at_play_time(live, mode: str) -> None:
    env, bucket = live
    ids = await three_videos(env)
    for video_id in ids:
        await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    jane = await env.video(ids[0])
    assert jane.state == "sent"

    bucket.mode = mode
    for _ in range(3):
        r = await env.http.get(f"/w/{jane.token}/video")
        assert r.status_code == 302, "the player is handed the link; a customer's video is not taken down on this"
    assert (await env.video(ids[0])).state == "sent" and await env.flags("file_changed") == []


@pytest.mark.parametrize("kind", ["", "video/mp4", "application/octet-stream"])
async def test_a_file_that_really_was_changed_is_still_pulled_when_the_bucket_says_so(live, kind: str) -> None:
    """The check is still there for what it was built for: other bytes put over a video
    after the server confirmed it. The bucket answers with a file that has another ETag,
    whatever type it gives the file."""
    env, bucket = live
    bucket.kind = kind
    ids = await three_videos(env)
    jane, bob = await env.video(ids[0]), await env.video(ids[1])
    await env.window_closed(ids[0])
    await videos.send_due(env.ctx)
    assert (await env.video(ids[0])).state == "sent"

    env.storage.objects[jane.object_key] = b"x" * 5000  # the same size, other bytes
    env.storage.objects[bob.object_key] = b"y" * 4000
    await env.window_closed(ids[1])
    await videos.send_due(env.ctx)
    r = await env.http.get(f"/w/{jane.token}/video")
    assert {(await env.video(v)).state for v in ids[:2]} == {"pulled"} and r.status_code == 410
    flags = await env.flags("file_changed")
    assert {f.video_id for f in flags} == set(ids[:2]) and all("was changed after it was uploaded" in f.detail for f in flags)
    assert [m.to for m in env.mailer.sent] == ["jane@example.com"], "no email for a changed file"


async def test_the_office_is_told_when_the_storage_keeps_answering_with_something_that_is_not_the_file(live) -> None:
    env, bucket = live
    ids = await three_videos(env)
    video_id = ids[0]
    long_ago = db.now() - timedelta(minutes=50)
    await env.run(sa.update(db.videos).where(db.videos.c.id == video_id).values(
        created_at=long_ago, recorded_at=long_ago, uploaded_at=long_ago + timedelta(minutes=1)))
    bucket.mode = PAGE_200
    await env.window_closed(video_id)
    await videos.send_due(env.ctx)
    (flag,) = await env.flags("file_doubt")
    assert flag.video_id == video_id and "the storage has not given a clear answer about its file" in flag.detail
    assert "Nothing was pulled and the customer has not been emailed" in flag.detail
    assert (await env.video(video_id)).state == "held" and env.mailer.sent == []


async def test_the_start_up_check_does_not_take_a_web_page_for_a_file(live, capsys, job_starts) -> None:
    """A 200 page in front of the bucket is no answer about a file at start-up either. The
    log used to read "holds the newest videos (1 of 0 looked for)"."""
    env, bucket = live
    bucket.mode = PAGE_200
    assert await env.ctx.storage.ask("videos/2026-11-14/there.mp4") == UNKNOWN
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    err = capsys.readouterr().err
    assert "of 0 looked for" not in err and "holds the newest videos" not in err
    assert "WARNING: the bucket gave no clear answer" in err

    bucket.mode = "ok"
    await three_videos(env)
    bucket.mode = PAGE_200
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 2
    err = capsys.readouterr().err
    assert "holds the newest videos" not in err and "WARNING: the bucket gave no clear answer" in err


async def five_videos(env: Env) -> list[str]:
    pat = await login(env)
    await env.http.get("/v1/today", headers=pat)
    sam = await login(env, "sam@example.com")
    ids = [await film(env, pat, customer) for customer in (501, 502, 503)]
    ids += [await film(env, sam, 504), await film(env, pat, None)]  # Sam's stop, and one not on the list
    env.mailer.sent.clear()
    return ids


async def test_the_wrong_bucket_stops_the_start_even_when_one_answer_is_lost(live, capsys, job_starts) -> None:
    """Five known videos, the settings point at another (empty) bucket, and one of the five
    small reads is lost to a bad moment. Four clear "not here" answers and not one "here"
    is proof enough: started, the emailer's first pass would pull those four."""
    env, bucket = live
    ids = await five_videos(env)
    for video_id in ids:
        if (await env.video(video_id)).state == "held":
            await env.window_closed(video_id)
    before = {v: (await env.video(v)).state for v in ids}
    bucket.objects = {}
    bucket.asked.clear()
    bucket.lose = {2}

    with pytest.raises(StartFailed):
        async with running(video_app.build_app(env.ctx)):
            pytest.fail("the server must not come up on the wrong bucket")
    err = capsys.readouterr().err
    assert "zest-video is NOT starting." in err and "holds none of the newest 5 videos" in err
    assert "4 are not there, and 1 got no clear answer" in err
    assert job_starts == [], "no job ran"
    assert {v: (await env.video(v)).state for v in ids} == before, "so no video was pulled"
    assert await env.flags("file_changed") == [] and env.mailer.sent == []


async def test_two_not_here_answers_among_lost_ones_are_still_not_proof(live, capsys, job_starts) -> None:
    env, bucket = live
    await five_videos(env)
    bucket.objects = {}
    bucket.asked.clear()
    bucket.lose = {1, 3, 5}
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert "WARNING: the bucket answers, and 2 of the newest 5 videos are not in it" in capsys.readouterr().err


# -- gate 3, the other way round: the database is the one this bucket's videos are in --


async def a_new_database(env: Env) -> None:
    """VIDEO_DATABASE_URL now leads to a brand-new, empty database: the Postgres service
    was deleted and added again, its volume was wiped, or the address points elsewhere."""
    async with env.ctx.engine.begin() as conn:
        await conn.run_sync(db.metadata.drop_all)


async def test_a_new_database_beside_a_bucket_full_of_videos_does_not_start(live, capsys, job_starts) -> None:
    """The address still looks right, so the settings gate passes. Started, every link
    already sent to a customer is dead, every phone is signed out, the owner's settings
    are back to what they were on day one, and new records pile up in the wrong place."""
    env, bucket = live
    await three_videos(env)
    assert len(bucket.objects) == 3
    await a_new_database(env)
    bucket.asked.clear()

    with pytest.raises(StartFailed):
        async with running(video_app.build_app(env.ctx)):
            pytest.fail("the server must not come up on a database that knows none of the bucket's videos")
    err = capsys.readouterr().err
    assert "zest-video is NOT starting." in err
    assert "The bucket holds videos, and the database does not know of one video" in err
    assert "VIDEO_DATABASE_URL" in err, "it names the setting to look at"
    assert "VIDEO_START_WITH_NEW_DATABASE=1" in err, "and says how a database that really is new gets past"
    assert not any(secret in err for secret in SECRETS)
    assert job_starts == [], "no job ran"
    assert len(bucket.objects) == 3, "and not one file was touched"
    assert bucket.asked == [f"GET /{video_app.BUCKET_PROBE_KEY}", "GET /"], "two small reads, nothing written"
    assert bucket.listed == [{**bucket.listed[0], "list-type": "2", "prefix": "videos/", "max-keys": "1"}]


async def test_a_database_in_use_with_old_files_in_the_bucket_starts_and_says_so(live, capsys, job_starts) -> None:
    """The one live-shaped state this check used to refuse wrongly. The database has no
    video row, and the bucket still holds files: test files from before a clean start, or
    videos filmed after a backup that was since put back. But phones have signed in on
    this database, and nobody can sign in on a database before a server has started on
    it. So it is the database in use, not a new one. Refusing would take a working
    service down at its next restart and protect nothing."""
    env, bucket = live
    await login(env)  # Pat's phone signs in
    bucket.objects["videos/2026-10-01/a-test-file-nobody-cleared-out.mp4"] = b"v" * 5000
    bucket.asked.clear()
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    err = capsys.readouterr().err
    assert "NOT starting" not in err
    assert "Videos: WARNING: the bucket holds video files, and this database knows of no video. Phones have signed in" in err
    assert "VIDEO_DATABASE_URL" in err, "and it still names the setting to look at if this is not the database expected"
    assert bucket.asked == [f"GET /{video_app.BUCKET_PROBE_KEY}", "GET /"] and len(bucket.objects) == 1, "nothing written"


async def test_what_marks_a_database_as_new_is_that_no_phone_ever_signed_in_on_it(live, capsys, job_starts) -> None:
    """Techs copied in from FieldRoutes and the owner's own office sign-in are not that
    mark: a server makes both by itself, on any database, in its first minutes."""
    env, bucket = live
    await auth.sync_techs(env.ctx)
    await auth.ensure_owners(env.ctx)
    bucket.objects["videos/2026-10-01/from-the-database-before.mp4"] = b"v" * 5000
    with pytest.raises(StartFailed):
        async with running(video_app.build_app(env.ctx)):
            pytest.fail("the server must not come up on a new database beside a bucket that holds videos")
    err = capsys.readouterr().err
    assert err.count("zest-video is NOT starting.") == 1
    assert "the database does not know of one video, and no phone has ever signed in on it" in err
    assert "VIDEO_START_WITH_NEW_DATABASE=1. Remove it again once the first phone has signed in." in err
    assert job_starts == []


async def test_a_database_that_really_is_new_can_be_started_by_name(live, capsys, job_starts) -> None:
    env, bucket = live
    await three_videos(env)
    await a_new_database(env)
    env.reconfigure(start_with_new_database=True)
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert "WARNING: The bucket holds videos, and the database does not know of one video" in capsys.readouterr().err


async def test_once_the_new_database_has_a_video_the_bucket_is_not_listed_again(live, capsys, job_starts) -> None:
    """So the override is never needed once the first video is in. (It is not needed
    from the first sign-in on, either; that is the test above.)"""
    env, bucket = live
    bucket.objects["videos/2026-10-01/from-the-database-before.mp4"] = b"v" * 5000
    env.reconfigure(start_with_new_database=True)
    async with running(video_app.build_app(env.ctx)):
        assert len(bucket.listed) == 1, "an empty database: the bucket is asked what it holds"
    assert "WARNING: The bucket holds videos, and the database does not know of one video" in capsys.readouterr().err

    headers = await login(env)
    await env.http.get("/v1/today", headers=headers)
    await film(env, headers, 501)
    env.reconfigure(start_with_new_database=False)  # the override is taken off again
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 2
    assert len(bucket.listed) == 1, "the database knows a video now, so the question is not asked again"
    assert "holds the newest videos (1 of 1 looked for)" in capsys.readouterr().err


@pytest.mark.parametrize("answer", ["SignatureDoesNotMatch", "AccessDenied", "NotImplemented"])
async def test_a_list_call_the_storage_will_not_answer_never_stops_a_start(
    live, capsys, job_starts, answer: str
) -> None:
    """Listing is asked nowhere else in the server. If the storage refuses it, that may
    be this one call's fault, so it settles nothing, whatever the refusal says."""
    env, bucket = live
    await three_videos(env)
    await a_new_database(env)
    bucket.listing = answer
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    err = capsys.readouterr().err
    assert "the database has no videos yet" in err
    assert "WARNING: the bucket would not say whether it holds videos" in err


async def test_a_new_database_on_a_laptop_is_a_warning(env: Env, capsys, job_starts) -> None:
    await three_videos(env)
    await a_new_database(env)
    async with running(video_app.build_app(env.ctx)):
        assert len(job_starts) == 1
    assert "WARNING: The bucket holds videos, and the database does not know of one video" in capsys.readouterr().err


async def test_how_the_bucket_is_asked_what_it_holds(live) -> None:
    env, bucket = live
    look = env.ctx.storage.look
    assert await look("videos/") == MISSING, "it answered, and holds nothing there"
    env.storage.objects["other/2026-11-14/a.txt"] = b"x"
    assert await look("videos/") == MISSING, "something else in the bucket is not a video"
    env.storage.objects["videos/2026-11-14/there.mp4"] = b"v" * 5000
    assert await look("videos/") == FOUND
    assert bucket.listed[-1] == {**bucket.listed[-1], "list-type": "2", "prefix": "videos/", "max-keys": "1"}
    assert all(a == "GET /" for a in bucket.asked), "one small read each time; nothing is ever written to ask"

    for mode in ("down", "error", FRONT_DOOR, "NoSuchBucket", "SignatureDoesNotMatch"):
        bucket.mode = mode
        assert await look("videos/") == UNKNOWN, f"{mode}: only a proper list settles anything"
    bucket.mode, bucket.listing = "ok", "AccessDenied"
    assert await look("videos/") == UNKNOWN

    memory = MemoryStorage()
    assert await memory.look("videos/") == MISSING
    memory.objects["videos/2026-11-14/a.mp4"] = b"1"
    assert await memory.look("videos/") == FOUND and await memory.look("other/") == MISSING


def test_a_signed_list_link_is_signed_over_its_own_question() -> None:
    """The bucket checks the signature over the whole question, so the three list settings
    must be inside what is signed, in the bucket's order, or every list call is refused."""
    made = dict(
        method="GET", endpoint="https://storage.example.com", bucket="zest-videos", key="", region="auto",
        access_key="made-up-key-id", secret_key="made-up-secret-key", seconds=60,
        now=datetime(2026, 11, 14, 12, 0, 0, tzinfo=timezone.utc),
    )
    question = {"list-type": "2", "prefix": "videos/", "max-keys": "1"}
    url = httpx.URL(presign(**made, query=question))
    assert (url.host, url.path) == ("zest-videos.storage.example.com", "/")
    names = [pair.split("=")[0] for pair in url.query.decode().split("&")]
    assert names[:-1] == sorted(names[:-1]) and names[-1] == "X-Amz-Signature", "sorted, the signature last"
    assert "prefix=videos%2F" in url.query.decode(), "the slash is written out, as the signature expects"
    signature = url.params["X-Amz-Signature"]
    assert len(signature) == 64 and "made-up-secret-key" not in str(url)
    other = httpx.URL(presign(**made, query={**question, "prefix": "other/"}))
    assert other.params["X-Amz-Signature"] != signature, "a different question, a different signature"
    plain = httpx.URL(presign(**made))
    assert plain.params["X-Amz-Signature"] != signature and "list-type" not in str(plain)
    path_style = httpx.URL(presign(**made, query=question, path_style=True))
    assert (path_style.host, path_style.path) == ("storage.example.com", "/zest-videos/")


async def test_a_demo_that_keeps_videos_in_memory_can_restart(tmp_path, capsys, job_starts) -> None:
    """A demo on Railway with Postgres and no bucket (the throwaway override on). After a
    restart the memory is empty and the database still knows its videos. That is what the
    override said would happen, not a wrong bucket, and it must not stop the demo."""
    env = await make_env(tmp_path)
    try:
        env.reconfigure(platform="Railway", allow_throwaway_storage=True, email_mode="off", fr_writes=False)
        assert check_start(env.ctx.cfg, database=False), "let through, with warnings"
        await three_videos(env)
        env.ctx.storage = MemoryStorage()  # the restart
        async with running(video_app.build_app(env.ctx)):
            assert len(job_starts) == 1
        err = capsys.readouterr().err
        assert "NOT starting" not in err
        assert "WARNING: videos are kept in this server's memory" in err and "3" in err
    finally:
        await env.http.aclose()
        await env.ctx.engine.dispose()


# -- /healthz -------------------------------------------------------------------------


async def test_healthz_says_ok_only_while_the_database_answers(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(http_web, "HEALTH_KEEP_SECONDS", 0)
    r = await env.http.get("/healthz")
    assert (r.status_code, r.text) == (200, "ok")

    good = env.ctx.engine

    class Gone:
        def connect(self) -> Any:
            raise OSError("connection refused")

    env.ctx.engine = Gone()  # type: ignore[assignment]
    r = await env.http.get("/healthz")
    assert r.status_code == 503 and r.text == "not ready: the database is not answering"
    assert r.headers["retry-after"] == "5" and "made-up" not in r.text

    env.ctx.engine = good
    r = await env.http.get("/healthz")
    assert (r.status_code, r.text) == (200, "ok"), "and it says so again by itself once the database is back"


async def test_healthz_does_not_hang_on_a_database_that_hangs(env: Env, monkeypatch) -> None:
    monkeypatch.setattr(http_web, "HEALTH_KEEP_SECONDS", 0)
    monkeypatch.setattr(http_web, "HEALTH_WAIT_SECONDS", 0.05)

    class Hung:
        @contextlib.asynccontextmanager
        async def connect(self) -> Any:
            await asyncio.sleep(60)
            yield None

    good, env.ctx.engine = env.ctx.engine, Hung()  # type: ignore[assignment]
    began = time.monotonic()
    r = await env.http.get("/healthz")
    env.ctx.engine = good
    assert r.status_code == 503 and time.monotonic() - began < 2


async def test_healthz_never_depends_on_the_bucket_fieldroutes_or_the_mail_service(env: Env, monkeypatch) -> None:
    """Each of those has a fallback that loses nothing. A bad minute at one of them must
    not make a working server look dead."""
    monkeypatch.setattr(http_web, "HEALTH_KEEP_SECONDS", 0)

    class Down:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"the health check touched the bucket ({name})")

    env.ctx.storage = Down()  # type: ignore[assignment]
    env.mailer.fail_with = "provider said 500"
    before = len(env.fake.requests)
    for _ in range(3):
        r = await env.http.get("/healthz")
        assert (r.status_code, r.text) == (200, "ok")
    assert len(env.fake.requests) == before, "and it spent nothing from the FieldRoutes quota"


async def test_a_flood_of_health_checks_costs_one_look_at_the_database(env: Env) -> None:
    looks = []
    real = env.ctx.engine.connect

    class Counting:
        def connect(self) -> Any:
            looks.append(1)
            return real()

    good, env.ctx.engine = env.ctx.engine, Counting()  # type: ignore[assignment]
    answers = await asyncio.gather(*(env.http.get("/healthz") for _ in range(40)))
    env.ctx.engine = good
    assert {a.status_code for a in answers} == {200} and len(looks) == 1
