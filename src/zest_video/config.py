"""Deployment config, read once from the environment.

Two rules shape the defaults, both from "never surprise the owner":

- Customer email is OFF until someone sets `VIDEO_EMAIL_MODE`. A fresh
  deployment can take videos all day and no customer hears from it.
- FieldRoutes writes obey the same `FR_WRITES` / `FR_WRITE_CUSTOMER_IDS`
  switches the MCP server uses, so one office has one set of write rules.

And one more, from "nothing may be lost": a container's own disk and memory are
wiped at every restart and redeploy. Records belong in Postgres and videos in
the bucket. A real deployment that is missing either setting does not start
(`check_start`); it never falls back, quietly, to a file or to memory.
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    raw = _env(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    return [p.strip() for p in _env(name).split(",") if p.strip()]


def _int_set(name: str) -> frozenset[int] | None:
    """Comma-separated IDs, or None when the variable is unset (meaning: no restriction)."""
    parts = _list(name)
    if not parts:
        return None
    out = set()
    for p in parts:
        try:
            out.add(int(p))
        except ValueError:
            continue
    return frozenset(out)


# With no database set, a laptop gets a file next to where the server was started.
LOCAL_DATABASE_URL = "sqlite+aiosqlite:///./zest_video.db"


def _database_url() -> str:
    """Railway hands out `postgresql://`; SQLAlchemy's async engine needs the driver named."""
    url = _env("VIDEO_DATABASE_URL") or _env("DATABASE_URL")
    if not url:
        return LOCAL_DATABASE_URL
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


# Railway puts these in every container it runs, so one of them means "this is a real
# deployment" whatever else is or is not set. The Railway command-line tool's own
# variables (RAILWAY_TOKEN and the like) are left out on purpose: a laptop has those.
RAILWAY_MARKS = (
    "RAILWAY_ENVIRONMENT_ID", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID", "RAILWAY_DEPLOYMENT_ID",
    "RAILWAY_REPLICA_ID", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT", "RAILWAY_SERVICE_NAME",
    "RAILWAY_PROJECT_NAME", "RAILWAY_PUBLIC_DOMAIN", "RAILWAY_PRIVATE_DOMAIN", "RAILWAY_STATIC_URL",
)

# Names that never lead to a real server: the ones set aside for tests and examples.
_NOT_REAL_ENDINGS = (".test", ".example", ".invalid", ".localhost", ".local", ".internal",
                     ".example.com", ".example.org", ".example.net")
_NOT_REAL_HOSTS = ("localhost", "example.com", "example.org", "example.net")


def public_https(url: str) -> bool:
    """Is this an https address the outside world can reach? `https://video.test`,
    `https://localhost` and `https://127.0.0.1` are not."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if parts.scheme != "https" or not host:
        return False
    if host in _NOT_REAL_HOSTS or host.endswith(_NOT_REAL_ENDINGS):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return "." in host


def _railway_origin() -> str:
    """The service's own public address, as Railway names it. Used only when
    VIDEO_BASE_URL is missing, so losing that one variable cannot turn every link in
    every email into a link to `localhost`."""
    domain = _env("RAILWAY_PUBLIC_DOMAIN").removeprefix("https://").removeprefix("http://").strip("/")
    return f"https://{domain}" if domain else ""


@dataclass(frozen=True)
class Config:
    database_url: str
    base_url: str  # public origin, no trailing slash; every emailed link is built from it
    tz: ZoneInfo

    # FieldRoutes
    office_id: int | None
    fr_writes: bool
    fr_write_customer_ids: frozenset[int] | None
    fr_employee_id: int | None  # employee the API writes as
    fr_note_type_id: int | None
    fr_sales_task_category_id: int | None
    sync_seconds: int  # how stale the day's stops may get before a refresh
    work_start_hour: int
    work_end_hour: int

    # Storage (S3-compatible, private bucket)
    s3_endpoint: str
    s3_bucket: str
    s3_region: str
    s3_access_key: str
    s3_secret_key: str
    s3_path_style: bool
    upload_url_seconds: int
    watch_url_seconds: int
    max_upload_bytes: int

    # Email
    mail_backend: str  # resend | log | off
    resend_api_key: str
    resend_webhook_secret: str
    mail_from: str
    mail_reply_to: str
    email_mode: str  # off | test | live  (customer emails only; login codes always send)
    email_test_to: str
    email_customer_ids: frozenset[int] | None

    # Text messages for login codes (Quo, formerly OpenPhone)
    quo_api_key: str
    quo_from: str

    # Push (APNs)
    apns_key_p8: str
    apns_key_id: str
    apns_team_id: str
    apns_bundle_id: str

    # Access
    owner_emails: tuple[str, ...]
    demo_contact: str
    demo_code: str
    secret: str  # signs CSRF tokens; sessions and device tokens are random, not signed

    # Words on pages and emails
    brand_name: str
    office_phone: str
    app_store_url: str
    privacy_contact: str

    extra: dict[str, str] = field(default_factory=dict)

    # Where this is running, and the three loud overrides (see `check_start` and app.start_up).
    platform: str = ""  # "Railway" when Railway's own variables are in the environment
    allow_throwaway_storage: bool = False  # VIDEO_ALLOW_THROWAWAY_STORAGE=1: a demo that may lose everything
    start_with_files_missing: bool = False  # VIDEO_START_WITH_FILES_MISSING=1: the bucket really was emptied
    start_with_new_database: bool = False  # VIDEO_START_WITH_NEW_DATABASE=1: the database really is new

    @property
    def storage_configured(self) -> bool:
        return bool(self.s3_endpoint and self.s3_bucket and self.s3_access_key and self.s3_secret_key)

    @property
    def real_deployment(self) -> bool:
        """Is this serving real techs and customers, and not a laptop, a test or CI?

        Two honest signs, and either is enough. The platform says so (Railway's own
        variables are there), or the server has been given a public https address to
        put in customers' emails. A test, a laptop and the CI smoke test have neither."""
        return bool(self.platform) or public_https(self.base_url)

    @property
    def push_configured(self) -> bool:
        return bool(self.apns_key_p8 and self.apns_key_id and self.apns_team_id and self.apns_bundle_id)

    @property
    def demo_enabled(self) -> bool:
        return bool(self.demo_contact and len(self.demo_code) == 6 and self.demo_code.isdigit())


# -- nothing may be kept on the container itself ---------------------------------------


FIX_AND_DEPLOY = (
    "Put the settings right (in Railway: the zest-video service, Variables) and deploy again.",
    "On Railway a deploy that does not start never takes over: the version before it keeps serving.",
)


class StartRefused(Exception):
    """This server must not start as it is set up. `message` is the whole story in plain
    words, ready to print: what is wrong, by variable name, and what to do. It never
    holds a value, only names, so it is safe in a log."""

    def __init__(self, why: str, problems: list[str], advice: tuple[str, ...] | list[str] = FIX_AND_DEPLOY):
        self.problems = list(problems)
        self.message = "\n".join(
            ["", "zest-video is NOT starting.", "", why, "", *[f"  - {line}" for line in problems], "", *advice, ""]
        )
        super().__init__(self.message)


_BUCKET_SETTINGS = (
    ("VIDEO_S3_ENDPOINT", "s3_endpoint"),
    ("VIDEO_S3_BUCKET", "s3_bucket"),
    ("VIDEO_S3_ACCESS_KEY_ID", "s3_access_key"),
    ("VIDEO_S3_SECRET_ACCESS_KEY", "s3_secret_key"),
)


def storage_problems(cfg: Config, *, database: bool = True, bucket: bool = True) -> list[str]:
    """Why this server, as set up, would lose records (`database`) or videos (`bucket`)
    at its next restart. One plain line for each cause, naming the variable. Empty when
    nothing would be lost."""
    return (_database_problems(cfg) if database else []) + (_bucket_problems(cfg) if bucket else [])


def _database_problems(cfg: Config) -> list[str]:
    out: list[str] = []
    url = cfg.database_url
    if url == LOCAL_DATABASE_URL:
        out.append(
            "VIDEO_DATABASE_URL is missing or empty. Every record (videos, sign-ins, settings) would go "
            "into a file inside the container. On Railway it is ${{Postgres.DATABASE_URL}}."
        )
    elif url.startswith("sqlite"):
        out.append(
            "VIDEO_DATABASE_URL names a SQLite file. That file is inside the container. It must be the "
            "Postgres address."
        )
    else:
        try:
            # Read the way the database engine will read it, so that what passes here is
            # what works there: a password with odd characters in it is fine, a port that
            # is not a number is not. `postgresql://` was turned into
            # `postgresql+asyncpg://` when it was loaded; any other driver's name is one
            # this server cannot use.
            from sqlalchemy.engine import make_url

            parsed = make_url(url)
            is_postgres = parsed.drivername == "postgresql+asyncpg" and bool(parsed.host)
        except Exception:  # noqa: BLE001 - whatever the parser makes of it, it is not an address
            is_postgres = False
        if not is_postgres:
            out.append(
                "VIDEO_DATABASE_URL is not a Postgres address this server can use. It must start with "
                "postgresql:// and name the server, like postgresql://user:password@host:5432/name"
            )
    return out


def _bucket_problems(cfg: Config) -> list[str]:
    out: list[str] = []
    missing = [name for name, attr in _BUCKET_SETTINGS if not getattr(cfg, attr)]
    if len(missing) == len(_BUCKET_SETTINGS):
        out.append(
            "No video bucket is set: " + ", ".join(missing) + " are all missing or empty. Videos would be "
            "kept in the server's memory."
        )
    elif missing:
        out.append(
            " and ".join(missing) + (" is" if len(missing) == 1 else " are") + " missing or empty. "
            "Without all four bucket settings, videos would be kept in the server's memory."
        )
    elif not cfg.s3_endpoint.startswith(("https://", "http://")) or not urlsplit(cfg.s3_endpoint).hostname:
        out.append("VIDEO_S3_ENDPOINT is not a web address. It must start with https://")
    return out


def link_problems(cfg: Config) -> list[str]:
    """Would links this server sends out be dead? Every emailed link and every link saved
    to FieldRoutes is built from the server's public address. An email cannot be called
    back, so this is checked whenever either could go out."""
    if public_https(cfg.base_url) or not (cfg.email_mode != "off" or cfg.fr_writes):
        return []
    return [
        "VIDEO_BASE_URL is missing or is not a public https address, while customer email or FieldRoutes "
        "writes are switched on. Every link sent out would be dead."
    ]


def check_start(cfg: Config, *, database: bool = True, bucket: bool = True) -> list[str]:
    """The gate every start goes through, before anything listens.

    On a real deployment (`Config.real_deployment`) a server that would keep its records
    in a file or its videos in memory must not start: it would run, look healthy, and
    lose everything at the next restart. Raising here is the safe failure. On Railway a
    deploy that does not start never takes over; the version before it keeps serving.

    Anywhere else (a laptop, a test, CI) the same findings come back as warnings to
    print. So does a real deployment with the demo override on. Returns those lines.

    `database` and `bucket` say which halves to look at. Both, for a real start. A caller
    that was handed its own database or its own storage (a test) leaves that half out."""
    lost = storage_problems(cfg, database=database, bucket=bucket)
    if not cfg.real_deployment:
        return [f"WARNING, fine on a laptop and never for real use: {line}" for line in lost]

    refuse = link_problems(cfg)
    notes: list[str] = []
    if lost and cfg.allow_throwaway_storage:
        notes.append(
            "WARNING: VIDEO_ALLOW_THROWAWAY_STORAGE is on. THIS SERVER LOSES ITS DATA AT THE NEXT RESTART. "
            "For a demo only. Remove the variable for real use."
        )
        notes += [f"WARNING: {line}" for line in lost]
    else:
        refuse = lost + refuse
    if not refuse:
        return notes + override_notes(cfg, needed=bool(lost))

    where = f"on {cfg.platform}" if cfg.platform else f"at {cfg.base_url}"
    advice = list(FIX_AND_DEPLOY)
    if lost and not cfg.allow_throwaway_storage:
        advice.append("Only for a demo that may lose every video and record: VIDEO_ALLOW_THROWAWAY_STORAGE=1.")
    raise StartRefused(
        f"This is a real deployment ({where}), and as it is set up now it would lose things:", refuse, advice
    )


# The loud overrides, by the name each is set with. Each lets a real deployment start
# where a gate would have stopped it.
OVERRIDES = (
    ("VIDEO_ALLOW_THROWAWAY_STORAGE", "allow_throwaway_storage"),
    ("VIDEO_START_WITH_FILES_MISSING", "start_with_files_missing"),
    ("VIDEO_START_WITH_NEW_DATABASE", "start_with_new_database"),
)


def overrides_on(cfg: Config) -> list[str]:
    return [name for name, attr in OVERRIDES if getattr(cfg, attr)]


def override_notes(cfg: Config, *, needed: bool) -> list[str]:
    """An override that is set says so at every start, whether or not this start needed
    it. One that was set once and forgotten would otherwise sit there in silence and
    switch its gate off on the one day the gate matters. `needed`: the throwaway-storage
    override is in use right now (`check_start` has already said so)."""
    out: list[str] = []
    if cfg.allow_throwaway_storage and not needed:
        out.append(
            "WARNING: VIDEO_ALLOW_THROWAWAY_STORAGE is on and nothing needs it: the database and the bucket "
            "are both set. REMOVE IT. While it is on, a lost setting does not stop a start, and this server "
            "would run on storage that is wiped at the next restart."
        )
    if cfg.start_with_files_missing:
        out.append(
            "WARNING: VIDEO_START_WITH_FILES_MISSING is on. REMOVE IT once this start is over. While it is on, "
            "a server pointed at the wrong bucket starts anyway, and takes videos down one by one."
        )
    if cfg.start_with_new_database:
        out.append(
            "WARNING: VIDEO_START_WITH_NEW_DATABASE is on. REMOVE IT once the first phone has signed in. While it is on, "
            "a server pointed at the wrong, empty database starts anyway, with every customer's link dead."
        )
    return out


# The words the FieldRoutes connector takes for "on" (fr_mcp.server._writes_enabled). The
# video server reads FR_WRITES by the same rule, so one value can never mean "on" here
# and "off" there.
ON_WORDS = ("on", "1", "true", "yes")
OFF_WORDS = ("off", "0", "false", "no")


def switch_notes() -> list[str]:
    """A switch set to a word this server does not know is read as off, and says so. A
    typo must never be what turns customer email or FieldRoutes writes on, and it must
    not leave them off in silence either."""
    out: list[str] = []
    writes = os.environ.get("FR_WRITES")
    if writes is not None and writes.strip().lower() not in ON_WORDS + OFF_WORDS:
        out.append(
            "WARNING: FR_WRITES is set to a word this server does not know, so it is read as off: nothing "
            "is written to FieldRoutes. Set it to on or off."
        )
    mode = os.environ.get("VIDEO_EMAIL_MODE")
    if mode is not None and mode.strip().lower() not in ("off", "test", "live"):
        out.append(
            "WARNING: VIDEO_EMAIL_MODE is set to a word this server does not know, so it is read as off: no "
            "customer is emailed. Set it to off, test or live."
        )
    return out


def hush(text: str, cfg: Config) -> str:
    """Cut every secret this server was given out of a line that is bound for the log."""
    secrets = [cfg.database_url, cfg.s3_secret_key, cfg.resend_api_key, cfg.resend_webhook_secret,
               cfg.quo_api_key, cfg.apns_key_p8, cfg.secret]
    # The database password, as it is written in the address and as the driver hands it on.
    written = cfg.database_url.partition("://")[2].rpartition("@")[0].partition(":")[2]
    secrets += [written, unquote(written)]
    for secret in sorted({x for x in secrets if len(x) >= 4}, key=len, reverse=True):
        text = text.replace(secret, "***")
    # And whatever else sits between "//" and "@" in any address quoted in the text.
    return re.sub(r"//[^/@\s]+@", "//***@", text)


def load() -> Config:
    email_mode = _env("VIDEO_EMAIL_MODE", "off").lower()
    if email_mode not in ("off", "test", "live"):
        email_mode = "off"
    mail_backend = _env("VIDEO_MAIL_BACKEND").lower() or ("resend" if _env("RESEND_API_KEY") else "off")
    if mail_backend not in ("resend", "log", "off"):
        mail_backend = "off"
    office = _env("FR_OFFICE_ID")
    return Config(
        database_url=_database_url(),
        base_url=(_env("VIDEO_BASE_URL") or _railway_origin() or "http://localhost:8080").rstrip("/"),
        tz=ZoneInfo(_env("VIDEO_TZ") or _env("TZ") or "America/Los_Angeles"),
        office_id=int(office) if office.isdigit() else None,
        # The connector's own rule, word for word. A missing FR_WRITES still means on, there and here.
        fr_writes=_env("FR_WRITES", "on").lower() in ON_WORDS,
        fr_write_customer_ids=_int_set("FR_WRITE_CUSTOMER_IDS"),
        fr_employee_id=_int("FR_DEFAULT_EMPLOYEE_ID", 0) or None,
        fr_note_type_id=int(_env("FR_DEFAULT_NOTE_TYPE_ID")) if _env("FR_DEFAULT_NOTE_TYPE_ID").isdigit() else None,
        fr_sales_task_category_id=(
            _int("VIDEO_SALES_TASK_CATEGORY_ID", 0) or _int("FR_DEFAULT_TASK_CATEGORY_ID", 0) or None
        ),
        sync_seconds=_int("VIDEO_SYNC_SECONDS", 180),
        work_start_hour=_int("VIDEO_WORK_START_HOUR", 6),
        work_end_hour=_int("VIDEO_WORK_END_HOUR", 20),
        s3_endpoint=(_env("VIDEO_S3_ENDPOINT")).rstrip("/"),
        s3_bucket=_env("VIDEO_S3_BUCKET"),
        s3_region=_env("VIDEO_S3_REGION") or "auto",
        s3_access_key=_env("VIDEO_S3_ACCESS_KEY_ID"),
        s3_secret_key=_env("VIDEO_S3_SECRET_ACCESS_KEY"),
        s3_path_style=_bool("VIDEO_S3_PATH_STYLE"),
        upload_url_seconds=_int("VIDEO_UPLOAD_URL_SECONDS", 86400),
        watch_url_seconds=_int("VIDEO_WATCH_URL_SECONDS", 900),
        max_upload_bytes=_int("VIDEO_MAX_UPLOAD_BYTES", 60_000_000),
        mail_backend=mail_backend,
        resend_api_key=_env("RESEND_API_KEY"),
        resend_webhook_secret=_env("RESEND_WEBHOOK_SECRET"),
        mail_from=_env("VIDEO_MAIL_FROM") or "Zest Lawn & Pest <video@zestlawnpest.com>",
        mail_reply_to=_env("VIDEO_MAIL_REPLY_TO"),
        email_mode=email_mode,
        email_test_to=_env("VIDEO_EMAIL_TEST_TO"),
        email_customer_ids=_int_set("VIDEO_EMAIL_CUSTOMER_IDS"),
        quo_api_key=_env("QUO_API_KEY"),
        quo_from=_env("QUO_FROM_NUMBER"),
        apns_key_p8=_env("APNS_KEY_P8").replace("\\n", "\n"),
        apns_key_id=_env("APNS_KEY_ID"),
        apns_team_id=_env("APNS_TEAM_ID"),
        apns_bundle_id=_env("APNS_BUNDLE_ID") or "com.zestlawnpest.video",
        owner_emails=tuple(e.lower() for e in _list("VIDEO_OWNER_EMAILS")),
        demo_contact=_env("VIDEO_DEMO_CONTACT").lower(),
        demo_code=_env("VIDEO_DEMO_CODE"),
        secret=_env("VIDEO_SECRET") or "dev-only-secret-change-me",
        brand_name=_env("VIDEO_BRAND_NAME") or "Zest Lawn & Pest",
        office_phone=_env("VIDEO_OFFICE_PHONE"),
        app_store_url=_env("VIDEO_APP_STORE_URL"),
        privacy_contact=_env("VIDEO_PRIVACY_CONTACT"),
        platform="Railway" if any(_env(name) for name in RAILWAY_MARKS) else "",
        allow_throwaway_storage=_bool("VIDEO_ALLOW_THROWAWAY_STORAGE"),
        start_with_files_missing=_bool("VIDEO_START_WITH_FILES_MISSING"),
        start_with_new_database=_bool("VIDEO_START_WITH_NEW_DATABASE"),
    )
