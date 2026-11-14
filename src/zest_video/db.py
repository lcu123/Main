"""Tables and engine. Postgres in production, SQLite in tests and local runs.

Plain SQLAlchemy Core on purpose: a dozen small tables, no ORM, and every
query readable where it is used. Times are stored as UTC and always come back
timezone-aware (`UTCDateTime`), because SQLite drops the zone otherwise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool


def now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(sa.TypeDecorator):
    impl = sa.DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect: Any) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


metadata = sa.MetaData()

# Techs mirror FieldRoutes employees of type 1 (technician). Refreshed daily and
# whenever an unknown contact asks for a code, so a new hire can log in at 6:30.
techs = sa.Table(
    "techs",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True),  # FieldRoutes employeeID
    sa.Column("fname", sa.String(80), nullable=False, default=""),
    sa.Column("lname", sa.String(80), nullable=False, default=""),
    sa.Column("email", sa.String(254), nullable=False, default=""),
    sa.Column("phone", sa.String(20), nullable=False, default=""),  # digits only
    sa.Column("active", sa.Boolean, nullable=False, default=True),
    sa.Column("demo", sa.Boolean, nullable=False, default=False),
    sa.Column("synced_at", UTCDateTime, nullable=False),
)

devices = sa.Table(
    "devices",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),
    sa.Column("tech_id", sa.Integer, nullable=False, index=True),
    sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
    sa.Column("name", sa.String(120), nullable=False, default=""),
    sa.Column("model", sa.String(60), nullable=False, default=""),
    sa.Column("os_version", sa.String(30), nullable=False, default=""),
    sa.Column("app_build", sa.Integer, nullable=False, default=0),
    sa.Column("apns_token", sa.String(200), nullable=False, default=""),
    sa.Column("apns_sandbox", sa.Boolean, nullable=False, default=False),
    sa.Column("created_at", UTCDateTime, nullable=False),
    sa.Column("last_seen_at", UTCDateTime, nullable=False),
    sa.Column("revoked_at", UTCDateTime),
    sa.Column("revoked_by", sa.String(254)),
)

# One-time codes for both tech login and office login. Only a hash is stored.
login_codes = sa.Table(
    "login_codes",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("purpose", sa.String(10), nullable=False),  # tech | office
    sa.Column("subject", sa.String(254), nullable=False, index=True),  # tech id or office email
    sa.Column("code_hash", sa.String(64), nullable=False),
    sa.Column("created_at", UTCDateTime, nullable=False),
    sa.Column("expires_at", UTCDateTime, nullable=False),
    sa.Column("tries", sa.Integer, nullable=False, default=0),
    sa.Column("used_at", UTCDateTime),
    sa.Column("issued_by", sa.String(254)),  # office user, when read out by hand
)

# The day's stops: one row per customer per day, every appointment folded in.
# No phone, email, billing or notes are kept here; the customer's email is read
# fresh from FieldRoutes at the moment an email is sent.
stops = sa.Table(
    "stops",
    metadata,
    sa.Column("day", sa.String(10), primary_key=True),  # YYYY-MM-DD, office local
    sa.Column("customer_id", sa.Integer, primary_key=True),
    sa.Column("tech_ids", sa.JSON, nullable=False),  # every tech assigned to any appointment
    sa.Column("name", sa.String(200), nullable=False, default=""),
    sa.Column("last_name", sa.String(80), nullable=False, default=""),
    sa.Column("address", sa.String(200), nullable=False, default=""),
    sa.Column("city_line", sa.String(120), nullable=False, default=""),
    sa.Column("lat", sa.Float),
    sa.Column("lng", sa.Float),
    sa.Column("appointments", sa.JSON, nullable=False),  # [{id, tech, type, status, checkIn, checkOut}]
    sa.Column("services", sa.JSON, nullable=False),
    sa.Column("position", sa.Integer, nullable=False, default=0),
    sa.Column("check_in", UTCDateTime),
    sa.Column("check_out", UTCDateTime),
    sa.Column("done", sa.Boolean, nullable=False, default=False),
    sa.Column("done_seen_at", UTCDateTime),  # when we first saw it finished; drives the no-video flag
    sa.Column("gone", sa.Boolean, nullable=False, default=False),  # cancelled or moved off the day
    sa.Column("synced_at", UTCDateTime, nullable=False),
)

videos = sa.Table(
    "videos",
    metadata,
    sa.Column("id", sa.String(36), primary_key=True),  # made on the phone
    sa.Column("token", sa.String(32), nullable=False, unique=True),  # 128-bit watch link
    sa.Column("tech_id", sa.Integer, nullable=False, index=True),
    sa.Column("tech_first_name", sa.String(80), nullable=False, default=""),
    sa.Column("device_id", sa.String(36), nullable=False),
    sa.Column("demo", sa.Boolean, nullable=False, default=False),
    sa.Column("day", sa.String(10), nullable=False, index=True),
    sa.Column("customer_id", sa.Integer, index=True),  # NULL while not-on-my-list is unmatched
    sa.Column("customer_name", sa.String(200), nullable=False, default=""),
    sa.Column("address", sa.String(200), nullable=False, default=""),
    sa.Column("city_line", sa.String(120), nullable=False, default=""),
    sa.Column("appointment_ids", sa.JSON, nullable=False),
    sa.Column("not_on_list", sa.Boolean, nullable=False, default=False),
    # awaiting_upload | held | sent | no_email | unmatched | pulled | deleted | replaced
    sa.Column("state", sa.String(20), nullable=False, index=True),
    sa.Column("object_key", sa.String(200), nullable=False),
    sa.Column("size_bytes", sa.Integer, nullable=False, default=0),
    sa.Column("etag", sa.String(80), nullable=False, default=""),
    sa.Column("duration_sec", sa.Float, nullable=False, default=0),
    sa.Column("recorded_at", UTCDateTime, nullable=False),  # phone clock
    sa.Column("lat", sa.Float),
    sa.Column("lng", sa.Float),
    sa.Column("accuracy_m", sa.Float),
    sa.Column("signals", sa.JSON, nullable=False),
    sa.Column("override", sa.Boolean, nullable=False, default=False),
    sa.Column("recommend", sa.Boolean, nullable=False, default=False),
    sa.Column("spoken_text", sa.Text, nullable=False, default=""),
    sa.Column("time_check", sa.String(10), nullable=False, default="unknown"),  # server's own check
    sa.Column("created_at", UTCDateTime, nullable=False),  # server clock
    sa.Column("uploaded_at", UTCDateTime),
    sa.Column("email_due_at", UTCDateTime),
    sa.Column("emailed_at", UTCDateTime),
    sa.Column("email_to", sa.String(254), nullable=False, default=""),
    sa.Column("email_id", sa.String(80), nullable=False, default=""),
    sa.Column("email_skip", sa.String(30), nullable=False, default=""),  # why no email went
    sa.Column("first_viewed_at", UTCDateTime),
    sa.Column("view_count", sa.Integer, nullable=False, default=0),
    sa.Column("ended_at", UTCDateTime),  # pulled, deleted or replaced
    sa.Column("ended_by", sa.String(254), nullable=False, default=""),
    sa.Column("ended_reason", sa.String(200), nullable=False, default=""),
    sa.Column("stuck_nudged_at", UTCDateTime),
    sa.Column("object_deleted", sa.Boolean, nullable=False, default=False),
)

scores = sa.Table(
    "scores",
    metadata,
    sa.Column("video_id", sa.String(36), primary_key=True),
    sa.Column("stars", sa.Integer, nullable=False),
    sa.Column("comment", sa.Text, nullable=False, default=""),
    sa.Column("created_at", UTCDateTime, nullable=False),
)

# Things the office must look at. `dedupe` makes raising a flag safe to repeat.
flags = sa.Table(
    "flags",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("dedupe", sa.String(120), nullable=False, unique=True),
    sa.Column("kind", sa.String(30), nullable=False, index=True),
    sa.Column("day", sa.String(10), nullable=False, index=True),
    sa.Column("tech_id", sa.Integer),
    sa.Column("customer_id", sa.Integer),
    sa.Column("video_id", sa.String(36)),
    sa.Column("detail", sa.Text, nullable=False, default=""),
    sa.Column("created_at", UTCDateTime, nullable=False),
    sa.Column("cleared_at", UTCDateTime),
    sa.Column("cleared_by", sa.String(254), nullable=False, default=""),
    sa.Column("note", sa.Text, nullable=False, default=""),
)

# Audit trail: every record, upload, view, pull, move, login and setting change.
events = sa.Table(
    "events",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("at", UTCDateTime, nullable=False, index=True),
    sa.Column("actor_type", sa.String(10), nullable=False),  # tech | office | customer | system
    sa.Column("actor", sa.String(254), nullable=False, default=""),
    sa.Column("action", sa.String(40), nullable=False, index=True),
    sa.Column("video_id", sa.String(36), index=True),
    sa.Column("customer_id", sa.Integer),
    sa.Column("detail", sa.JSON),
)

settings = sa.Table(
    "settings",
    metadata,
    sa.Column("key", sa.String(40), primary_key=True),
    sa.Column("value", sa.JSON),
    sa.Column("updated_at", UTCDateTime, nullable=False),
    sa.Column("updated_by", sa.String(254), nullable=False, default=""),
)

office_users = sa.Table(
    "office_users",
    metadata,
    sa.Column("email", sa.String(254), primary_key=True),
    sa.Column("role", sa.String(10), nullable=False),  # owner | staff
    sa.Column("active", sa.Boolean, nullable=False, default=True),
    sa.Column("added_at", UTCDateTime, nullable=False),
    sa.Column("added_by", sa.String(254), nullable=False, default=""),
)

office_sessions = sa.Table(
    "office_sessions",
    metadata,
    sa.Column("token_hash", sa.String(64), primary_key=True),
    sa.Column("email", sa.String(254), nullable=False, index=True),
    sa.Column("created_at", UTCDateTime, nullable=False),
    sa.Column("expires_at", UTCDateTime, nullable=False),
)

# FieldRoutes writes wait here and retry, so an API outage never loses a note,
# a score or a task. `dedupe` keeps a retry from writing twice.
fr_outbox = sa.Table(
    "fr_outbox",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column("dedupe", sa.String(120), nullable=False, unique=True),
    sa.Column("kind", sa.String(10), nullable=False),  # note | review | task
    sa.Column("customer_id", sa.Integer, nullable=False),
    sa.Column("video_id", sa.String(36)),
    sa.Column("payload", sa.JSON, nullable=False),
    sa.Column("attempts", sa.Integer, nullable=False, default=0),
    sa.Column("next_try_at", UTCDateTime, nullable=False),
    sa.Column("done_at", UTCDateTime),
    sa.Column("failed_at", UTCDateTime),
    sa.Column("last_error", sa.Text, nullable=False, default=""),
    sa.Column("created_at", UTCDateTime, nullable=False),
)

# Customers who asked not to get video emails (the link in every email).
email_optouts = sa.Table(
    "email_optouts",
    metadata,
    sa.Column("customer_id", sa.Integer, primary_key=True),
    sa.Column("at", UTCDateTime, nullable=False),
)


async def insert_once(conn: Any, table: sa.Table, values: dict[str, Any]) -> bool:
    """Insert a row unless one with the same unique key is already there. True when this
    call put it in.

    Catching the database's complaint about a second row would do for one insert, but on
    Postgres that complaint spoils the whole save it happens in. This way the save goes
    on, so a record and what follows from it (a flag, an outbox row) can be written in one
    step, and two servers running side by side at a deploy cannot trip each other up."""
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    result = await conn.execute(insert(table).values(**values).on_conflict_do_nothing())
    return bool(result.rowcount)


def make_engine(url: str) -> AsyncEngine:
    if url.startswith("sqlite"):
        kwargs: dict[str, Any] = {"connect_args": {"timeout": 30}}
        if ":memory:" in url:
            # One shared connection, or every checkout would see its own empty database.
            kwargs["poolclass"] = StaticPool
        return create_async_engine(url, **kwargs)
    return create_async_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5)


async def create_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
