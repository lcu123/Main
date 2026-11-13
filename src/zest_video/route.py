"""The day's stops: pulled from FieldRoutes, cached, and served to the phone.

A stop is one customer on one day. Every appointment that customer has that
day is folded into it (on a real day 8 of 33 customers had 2 or 3 appointments
in one visit). The rule is one video per tech, per property, per day: a tech
with three appointments there sends one video, and when two techs each have
appointments at the same property each sends his own. `tech_view` is one
tech's share of a stop.

The cache is what keeps the app working when FieldRoutes is down and what
keeps this server inside the shared API quota: a refresh is one
`appointment/search`, plus a `customer/get` only for customers not seen yet
today.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import sqlalchemy as sa

from . import db
from .auth import Device
from .core import Ctx, get_settings
from .fr import FieldRoutesError, clean, id_list, local_time, to_float, to_int

LIVE_STATES = ("awaiting_upload", "held", "sent", "no_email", "unmatched")


@dataclass(frozen=True)
class StopView:
    """One tech's share of a stop: the appointments he leads there, and what follows
    from them. A tech who leads none (a helper riding along, listed only as an
    additional tech) shares the whole stop."""

    tech_id: int
    lead: bool
    appointment_ids: list[int] = field(default_factory=list)
    types: set[Any] = field(default_factory=set)
    services: list[str] = field(default_factory=list)
    check_in: datetime | None = None
    check_out: datetime | None = None
    done: bool = False
    done_seen_at: datetime | None = None


def _when(value: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def lead_techs(stop: Any) -> list[int]:
    """Techs with an appointment of their own at this stop, in appointment order."""
    out: list[int] = []
    for a in stop.appointments or []:
        tech = (to_int(a.get("tech")) or 0) if isinstance(a, dict) else 0
        if tech > 0 and tech not in out:
            out.append(tech)
    return out


def helper_techs(stop: Any) -> list[int]:
    leads = lead_techs(stop)
    return [t for t in (stop.tech_ids or []) if t not in leads]


def tech_view(stop: Any, tech_id: int | None) -> StopView:
    appts = [a for a in (stop.appointments or []) if isinstance(a, dict)]
    own = [a for a in appts if tech_id and (to_int(a.get("tech")) or 0) == tech_id]
    lead = bool(own)
    if not own:
        own = appts
    services: list[str] = []
    for a in own:
        name = a.get("service")
        if name and name not in services:
            services.append(name)
    if not services and (not lead or len(lead_techs(stop)) <= 1):
        services = list(stop.services or [])  # rows saved before services were kept per appointment
    ins = [t for t in (_when(a.get("checkIn")) for a in own) if t]
    open_check_in = any(a.get("checkIn") and not a.get("checkOut") for a in own)
    outs = [t for t in (_when(a.get("checkOut")) for a in own if a.get("checkIn")) if t]
    done = bool(own) and all((to_int(a.get("status")) or 0) == 1 for a in own)
    seen = [_when(a.get("doneSeenAt")) for a in own]
    done_seen = None
    if done:
        done_seen = max(seen) if all(seen) else stop.done_seen_at  # type: ignore[type-var]
    return StopView(
        tech_id=tech_id or 0,
        lead=lead,
        appointment_ids=[a["id"] for a in own if a.get("id")],
        types={a.get("type") for a in own},
        services=services,
        check_in=min(ins) if ins else None,
        check_out=max(outs) if outs and not open_check_in else None,
        done=done,
        done_seen_at=done_seen,
    )


def missing_key(day: str, customer_id: int, tech_id: int) -> str:
    """Dedupe key of the 'finished with no video' flag: one per tech per stop."""
    return f"no_video:{day}:{customer_id}:{tech_id}"


def public_state(state: str, first_viewed_at: datetime | None) -> str:
    return "watched" if state == "sent" and first_viewed_at else state


def iso(value: datetime | None) -> str | None:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def _customer_name(row: dict[str, Any]) -> tuple[str, str]:
    """(display name, last name). A business with no person on it has no last name."""
    first, last = clean(row.get("fname")), clean(row.get("lname"))
    company = clean(row.get("companyName"))
    person = clean(f"{first} {last}")
    return (person or company or "Customer"), (last if person else "")


def _city_line(row: dict[str, Any]) -> str:
    city, state, zip_code = clean(row.get("city")), clean(row.get("state")), clean(row.get("zip"))
    left = ", ".join(p for p in (city, state) if p)
    return clean(f"{left} {zip_code}")


async def sync_day(ctx: Ctx, day: str, *, force: bool = False) -> bool:
    """Refresh the cached stops for `day`. Returns False when it was fresh enough to skip.
    Raises FieldRoutesError when FieldRoutes cannot be reached; callers that serve the
    phone catch it and fall back to the cache."""
    async with ctx.sync_lock:
        last = ctx.last_sync.get(day)
        if not force and last is not None and time.monotonic() - last < ctx.cfg.sync_seconds:
            return False
        await _sync_day(ctx, day)
        ctx.last_sync[day] = time.monotonic()
        return True


async def _sync_day(ctx: Ctx, day: str) -> None:
    tz = ctx.cfg.tz
    appts = await ctx.fr.day_appointments(day)

    # The tech is the appointment's assignedTech; only when that is unset do we need
    # the route's tech, so the extra route read happens only on days that need it.
    routes: dict[int, dict[str, Any]] = {}
    if any((to_int(a.get("assignedTech")) or 0) <= 0 for a in appts):
        routes = await ctx.fr.day_routes(day)

    groups: dict[int, list[dict[str, Any]]] = {}
    for a in appts:
        customer_id = to_int(a.get("customerID"))
        if customer_id is not None:
            groups.setdefault(customer_id, []).append(a)

    async with ctx.engine.connect() as conn:
        existing = {
            r.customer_id: r for r in (await conn.execute(sa.select(db.stops).where(db.stops.c.day == day))).all()
        }

    new_ids = [cid for cid in groups if cid not in existing or not existing[cid].name]
    customers = await ctx.fr.customers(new_ids) if new_ids else {}
    service_names = await ctx.fr.service_names()
    unknown_types = {to_int(a.get("type")) for a in appts} - set(service_names) - {None}
    if unknown_types:  # a service type added since we cached the names
        ctx.fr.forget_service_names()
        service_names = await ctx.fr.service_names()

    def order_key(item: tuple[int, list[dict[str, Any]]]) -> tuple[str, int, int]:
        rows = item[1]
        return (
            min(clean(a.get("start")) or "99" for a in rows),
            min(to_int(a.get("sequence")) or 0 for a in rows),
            min(to_int(a.get("appointmentID")) or 0 for a in rows),
        )

    now = db.now()
    async with ctx.engine.begin() as conn:
        for position, (customer_id, rows) in enumerate(sorted(groups.items(), key=order_key), start=1):
            before = existing.get(customer_id)
            earlier = {a.get("id"): a for a in (before.appointments or []) if isinstance(a, dict)} if before else {}
            tech_ids: list[int] = []
            folded: list[dict[str, Any]] = []
            services: list[str] = []
            check_ins: list[datetime] = []
            check_outs: list[datetime] = []
            open_check_in = False
            for a in sorted(rows, key=lambda r: to_int(r.get("appointmentID")) or 0):
                tech = to_int(a.get("assignedTech")) or 0
                if tech <= 0:
                    route = routes.get(to_int(a.get("routeID")) or 0, {})
                    tech = to_int(route.get("assignedTech")) or 0
                for t in [tech, *id_list(a.get("additionalTechs"))]:
                    if t > 0 and t not in tech_ids:
                        tech_ids.append(t)
                type_id = to_int(a.get("type"))
                name = service_names.get(type_id or 0, "")
                if name and name not in services:
                    services.append(name)
                check_in = local_time(a.get("checkIn"), tz) or local_time(a.get("timeIn"), tz)
                check_out = local_time(a.get("checkOut"), tz) or local_time(a.get("timeOut"), tz)
                if check_in:
                    check_ins.append(check_in)
                    if check_out:
                        check_outs.append(check_out)
                    else:
                        open_check_in = True
                appointment_id = to_int(a.get("appointmentID"))
                status = to_int(a.get("status")) or 0
                # When we first saw this appointment finished: it starts the clock on the
                # "finished with no video" flag for the tech who leads it.
                done_seen_at = None
                if status == 1:
                    was = earlier.get(appointment_id)
                    done_seen_at = (was or {}).get("doneSeenAt") or (
                        before.done_seen_at.isoformat() if was and before is not None and before.done_seen_at else None
                    ) or now.isoformat()
                folded.append(
                    {
                        "id": appointment_id,
                        "tech": tech,
                        "type": type_id,
                        "service": name,
                        "status": status,
                        "checkIn": check_in.isoformat() if check_in else None,
                        "checkOut": check_out.isoformat() if check_out else None,
                        "doneSeenAt": done_seen_at,
                    }
                )
            done = all(f["status"] == 1 for f in folded)
            values: dict[str, Any] = dict(
                tech_ids=tech_ids,
                appointments=folded,
                services=services,
                position=position,
                check_in=min(check_ins) if check_ins else None,
                check_out=max(check_outs) if check_outs and not open_check_in else None,
                done=done,
                gone=False,
                synced_at=now,
            )
            customer = customers.get(customer_id)
            if customer is not None:
                name, last_name = _customer_name(customer)
                values.update(
                    name=name[:200],
                    last_name=last_name[:80],
                    address=clean(customer.get("address"))[:200],
                    city_line=_city_line(customer)[:120],
                    lat=to_float(customer.get("lat")),
                    lng=to_float(customer.get("lng")),
                )
            if before is None:
                values.setdefault("name", "Customer")
                values["done_seen_at"] = now if done else None
                await conn.execute(sa.insert(db.stops).values(day=day, customer_id=customer_id, **values))
            else:
                if done and before.done_seen_at is None:
                    values["done_seen_at"] = now
                elif not done:
                    values["done_seen_at"] = None
                await conn.execute(
                    sa.update(db.stops)
                    .where(db.stops.c.day == day, db.stops.c.customer_id == customer_id)
                    .values(**values)
                )
        dropped = [cid for cid in existing if cid not in groups and not existing[cid].gone]
        if dropped:  # cancelled or moved to another day since the last look
            await conn.execute(
                sa.update(db.stops)
                .where(db.stops.c.day == day, db.stops.c.customer_id.in_(dropped))
                .values(gone=True, synced_at=now)
            )


async def stops_for_day(ctx: Ctx, day: str) -> list[Any]:
    async with ctx.engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    sa.select(db.stops)
                    .where(db.stops.c.day == day, db.stops.c.gone.is_(False))
                    .order_by(db.stops.c.position)
                )
            ).all()
        )


async def live_videos_for_day(ctx: Ctx, day: str) -> list[Any]:
    async with ctx.engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    sa.select(db.videos).where(db.videos.c.day == day, db.videos.c.state.in_(LIVE_STATES))
                )
            ).all()
        )


def demo_stops(day: str) -> list[dict[str, Any]]:
    """Made-up stops for the App Review account. No map pins, so nothing claims a location."""
    samples = [
        (-101, "Sample Customer One", "One", "100 Sample Street", ["Lawn Care"]),
        (-102, "Sample Customer Two", "Two", "200 Example Avenue", ["Pest Control"]),
        (-103, "Sample Customer Three", "Three", "300 Demo Court", ["Irrigation Check"]),
    ]
    return [
        {
            "customerId": cid,
            "name": name,
            "lastName": last,
            "address": address,
            "cityLine": "Sacramento, CA 95814",
            "lat": None,
            "lng": None,
            "appointmentIds": [],
            "services": services,
            "order": i,
            "checkIn": None,
            "checkOut": None,
            "done": False,
            "video": None,
        }
        for i, (cid, name, last, address, services) in enumerate(samples, start=1)
    ]


async def today_payload(ctx: Ctx, device: Device) -> dict[str, Any]:
    day = ctx.today()
    settings = await get_settings(ctx)
    if not device.demo:
        try:
            await sync_day(ctx, day)
        except FieldRoutesError:
            pass  # FieldRoutes is down: serve the saved route, the phone keeps working

    videos = await live_videos_for_day(ctx, day)
    mine = {v.customer_id: v for v in videos if v.customer_id is not None and v.tech_id == device.tech_id}
    theirs = {v.customer_id: v for v in videos if v.customer_id is not None and v.tech_id != device.tech_id}

    def video_ref(customer_id: int, lead: bool = True) -> dict[str, Any] | None:
        # One video per tech per property: a tech with his own appointments here sees
        # only his own video. A helper also sees the lead's, so the stop reads as covered.
        v = mine.get(customer_id)
        if v is None and not lead:
            v = theirs.get(customer_id)
        if v is None:
            return None
        return {"id": v.id, "state": public_state(v.state, v.first_viewed_at), "byYou": v.tech_id == device.tech_id}

    if device.demo:
        stops = demo_stops(day)
        for s in stops:
            s["video"] = video_ref(s["customerId"])
    else:
        stops = []
        for row in await stops_for_day(ctx, day):
            if device.tech_id not in (row.tech_ids or []):
                continue
            view = tech_view(row, device.tech_id)
            stops.append(
                {
                    "customerId": row.customer_id,
                    "name": row.name,
                    "lastName": row.last_name,
                    "address": row.address,
                    "cityLine": row.city_line,
                    "lat": row.lat,
                    "lng": row.lng,
                    "appointmentIds": view.appointment_ids,
                    "services": view.services,
                    "order": row.position,
                    "checkIn": iso(view.check_in),
                    "checkOut": iso(view.check_out),
                    "done": view.done,
                    "video": video_ref(row.customer_id, view.lead),
                }
            )

    return {
        "date": day,
        "serverTime": iso(db.now()),
        "tech": {"id": device.tech_id, "firstName": device.tech_first_name, "demo": device.demo},
        "config": {
            "maxSeconds": settings["max_seconds"],
            "holdMinutes": settings["hold_minutes"],
            "farMeters": settings["far_meters"],
            "minBuild": settings["min_build"],
            "requireVideo": settings["require_video"],
            "recommendButton": settings["recommend_button"],
            "leaveReminders": settings["leave_reminders"],
            "prompts": settings["prompts"],
            "officePhone": ctx.cfg.office_phone,
        },
        "stops": stops,
        "videos": [
            {
                "id": v.id,
                "customerId": v.customer_id,
                "state": public_state(v.state, v.first_viewed_at),
                "emailAt": iso(v.email_due_at),
                # Only the App Review account is handed the customer's link, so a reviewer
                # can see the other half of the product. A real tech's phone never gets one.
                **({"watchUrl": f"{ctx.cfg.base_url}/w/{v.token}"} if device.demo and v.state != "awaiting_upload" else {}),
            }
            for v in videos
            if v.tech_id == device.tech_id
        ],
    }


def in_work_hours(ctx: Ctx, when: datetime | None = None) -> bool:
    hour = (when or db.now()).astimezone(ctx.cfg.tz).hour
    return ctx.cfg.work_start_hour <= hour < ctx.cfg.work_end_hour


def time_check(stop: Any, recorded_at: datetime, slack: timedelta = timedelta(minutes=20)) -> str:
    """Server's own look at whether the recording fits the stop's check-in window.

    Check-in is a hint, not proof: on a real day several stops had check-in and
    check-out 1 to 4 seconds apart, so the window gets generous slack on both sides
    and a mismatch only ever raises a flag.
    """
    if stop is None or stop.check_in is None:
        return "unknown"
    start = stop.check_in - slack
    end = (stop.check_out or db.now()) + slack
    return "match" if start <= recorded_at <= end else "mismatch"
