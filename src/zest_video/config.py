"""Deployment config, read once from the environment.

Two rules shape the defaults, both from "never surprise the owner":

- Customer email is OFF until someone sets `VIDEO_EMAIL_MODE`. A fresh
  deployment can take videos all day and no customer hears from it.
- FieldRoutes writes obey the same `FR_WRITES` / `FR_WRITE_CUSTOMER_IDS`
  switches the MCP server uses, so one office has one set of write rules.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
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


def _database_url() -> str:
    """Railway hands out `postgresql://`; SQLAlchemy's async engine needs the driver named."""
    url = _env("VIDEO_DATABASE_URL") or _env("DATABASE_URL")
    if not url:
        return "sqlite+aiosqlite:///./zest_video.db"
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


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

    @property
    def storage_configured(self) -> bool:
        return bool(self.s3_endpoint and self.s3_bucket and self.s3_access_key and self.s3_secret_key)

    @property
    def push_configured(self) -> bool:
        return bool(self.apns_key_p8 and self.apns_key_id and self.apns_team_id and self.apns_bundle_id)

    @property
    def demo_enabled(self) -> bool:
        return bool(self.demo_contact and len(self.demo_code) == 6 and self.demo_code.isdigit())


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
        base_url=(_env("VIDEO_BASE_URL") or "http://localhost:8080").rstrip("/"),
        tz=ZoneInfo(_env("VIDEO_TZ") or _env("TZ") or "America/Los_Angeles"),
        office_id=int(office) if office.isdigit() else None,
        fr_writes=_env("FR_WRITES", "on").lower() != "off",
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
    )
