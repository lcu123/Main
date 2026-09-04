"""FieldRoutes MCP server: curated tools, generic tools, write guards, HTTP entrypoint.

Tool docstrings are the schema descriptions Claude reads, so they say when to
use a tool and what it returns, in one or two sentences. Outputs are trimmed
with `_pick(row, *_KEYS)` to what an office user needs; the generic `get`
tool returns full records.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from functools import wraps
from importlib import resources
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from . import __version__
from .client import FieldRoutesClient, FieldRoutesError
from .generated import is_read_action, register_generated_tools

SPEC: dict[str, Any] = json.loads(resources.files("fr_mcp").joinpath("fieldroutes_spec.json").read_text())

mcp = MCPServer(
    "fieldroutes",
    instructions=(
        "Read and write access to one FieldRoutes office. Start with find_customer or the "
        "schedule views (day_schedule, route_stops, crew_schedule, due_for_service, open_slots). "
        "Use lookups for employee, service type and region IDs. Confirm with the user before "
        "cancelling, completing, charging, freezing a subscription, or overwriting Red Notes. "
        "Dates are YYYY-MM-DD in the office's local time."
    ),
)

# --- output shaping -----------------------------------------------------
# What an office user sees for each record type. Add a key here rather than
# dumping whole records; `get` returns everything for the rare full-record need.

CUSTOMER_KEYS = [
    "customerID", "fname", "lname", "companyName", "status", "statusText", "phone1", "phone2",
    "email", "address", "city", "state", "zip", "lat", "lng", "regionID", "balance", "balanceAge",
    "responsibleBalance", "aPay", "preferredTechID", "specialScheduling", "notes",
    "subscriptionIDs", "dateAdded", "dateCancelled", "pendingCancel", "commercialAccount",
]
SUBSCRIPTION_KEYS = [
    "subscriptionID", "customerID", "serviceID", "serviceType", "active", "activeText",
    "frequency", "billingFrequency", "seasonalStart", "seasonalEnd", "customDate", "nextService",
    "lastCompleted", "lastAppointment", "preferredTech", "preferredDays", "preferredStart",
    "preferredEnd", "regionID", "duration", "callAhead", "recurringCharge", "contractValue",
    "annualRecurringValue", "soldBy", "soldBy2", "soldBy3", "sourceID", "source",
    "agreementLength", "expirationDate", "poNumber", "nextBillingDate", "renewalDate",
    "renewalFrequency", "initialAppointmentID", "dateAdded", "dateCancelled", "cxlNotes",
]
# due_for_service is a skim-a-list view ("what's coming up"), not a full subscription
# record -- drop billing/sales/administrative fields an office person wouldn't need to
# decide what's due, so more rows fit before a large office's real volume hits the
# response size ceiling. subscription_details/service_schedule keep the full SUBSCRIPTION_KEYS.
DUE_FOR_SERVICE_KEYS = [
    "subscriptionID", "customerID", "serviceID", "serviceType", "active", "activeText",
    "frequency", "nextService", "lastCompleted", "preferredTech", "preferredDays",
    "preferredStart", "preferredEnd", "regionID", "duration", "callAhead",
]
APPOINTMENT_KEYS = [
    "appointmentID", "customerID", "subscriptionID", "routeID", "spotID", "date", "start", "end",
    "timeWindow", "duration", "type", "status", "statusText", "employeeID", "assignedTech",
    "additionalTechs", "servicedBy", "completedBy", "dateCompleted", "notes", "officeNotes",
    "appointmentNotes", "isInitial", "callAhead", "doInterior", "targetPests", "ticketID",
    "dateCancelled", "cancellationReason", "sequence", "amountCollected",
]
TASK_KEYS = [
    "taskIDs", "customerID", "type", "category", "categoryDescription", "task", "status", "dueDate",
    "assignedTo", "addedBy", "completedBy", "dateAdded", "dateCompleted", "completionNotes",
    "referenceID", "phone",
]
NOTE_KEYS = [
    "noteID", "customerID", "customerName", "employeeID", "employeeName", "date", "typeID", "type",
    "notes", "showTech", "showCustomer", "referenceID", "dateAdded",
]
ROUTE_KEYS = [
    "routeID", "title", "date", "assignedTech", "additionalTechs", "groupTitle", "dayNotes",
    "dayAlert", "apiCanSchedule", "averageLatitude", "averageLongitude", "averageDistance",
    "distanceScore", "officeID",
]
SPOT_KEYS = [
    "spotID", "routeID", "date", "start", "end", "spotCapacity", "description",
    "currentAppointment", "currentAppointmentDuration", "blockReason", "apiCanSchedule",
    "assignedTech", "reserved", "reservationEnd", "distanceToPrevious", "distanceToNext",
    "prevCustomer", "nextCustomer", "previousLat", "previousLng", "nextLat", "nextLng",
]
EMPLOYEE_KEYS = [
    "employeeID", "fname", "lname", "nickname", "type", "active", "phone", "email", "teamIDs",
    "primaryTeam", "skillDescriptions",
]
SERVICE_TYPE_KEYS = [
    "typeID", "description", "category", "frequency", "defaultCharge", "defaultInitialCharge",
    "defaultLength", "seasonStart", "seasonEnd", "reservice", "regularService", "visible",
]
REGION_KEYS = ["regionID", "regionIDs", "description", "type", "active"]
OFFICE_KEYS = ["officeID", "officeName", "address", "city", "state", "zip", "timeZone", "contactNumber"]
FLAG_KEYS = ["customerID", "flag", "flagValue"]
REASON_KEYS = ["reasonID", "cancellationReasonID", "rescheduleReasonID", "reserviceReasonID", "description", "active"]
SOURCE_KEYS = ["sourceID", "source", "description", "active"]

EMPLOYEE_TYPES = {0: "office", 1: "tech", 2: "sales"}
APPOINTMENT_STATUS = {0: "pending", 1: "completed", 2: "no-show", -1: "cancelled"}
TASK_STATUS = {0: "pending", 1: "completed", 2: "in use", 3: "urgent", -1: "deleted"}
SUBSCRIPTION_ACTIVE = {1: "active", 0: "frozen", -3: "lead"}
DAYS = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str)


def _pick(row: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {k: row[k] for k in keys if k in row and row[k] not in (None, "", [])}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str:
    """Collapse whitespace: live records carry trailing spaces in names ("Pedro ") and stray
    tabs in addresses, which otherwise show up as double spaces in every resolved name."""
    return " ".join(str(value).split()) if value is not None else ""


def _name(row: dict[str, Any]) -> str:
    person = _clean(f"{row.get('fname') or ''} {row.get('lname') or ''}")
    company = _clean(row.get("companyName") or "")
    if person and company:
        return f"{person} ({company})"
    return person or company or ""


def _address(row: dict[str, Any]) -> str:
    parts = (_clean(row.get(k)) for k in ("address", "city", "state", "zip"))
    return ", ".join(p for p in parts if p)


def _customer_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "customerID": _int(row.get("customerID")),
        "name": _name(row),
        "address": _address(row),
        "phone": row.get("phone1"),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
    }


def _frequency_text(freq: Any) -> str:
    # Verified live: a subscription on a custom schedule comes back with the literal string
    # "CUSTOM" in `frequency` (not the -3 sentinel the spec implies), so handle both.
    if isinstance(freq, str) and freq.strip().upper() == "CUSTOM":
        return "custom schedule"
    f = _int(freq)
    if f is None:
        return ""
    if f == -1:
        return "one-time"
    if f == 0:
        return "as needed"
    if f == -3:
        return "custom schedule"
    if f > 0 and f % 30 == 0:
        months = f // 30
        return "monthly" if months == 1 else f"every {months} months"
    return f"every {f} days"


# --- config + guards ----------------------------------------------------


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _writes_enabled() -> bool:
    return os.environ.get("FR_WRITES", "on").strip().lower() in ("on", "1", "true", "yes")


def _allow_delete() -> bool:
    return _flag("FR_ALLOW_DELETE")


def _allow_charges() -> bool:
    return _flag("FR_ALLOW_CHARGES")


def _require_writes(tool: str) -> None:
    if not _writes_enabled():
        raise ToolError(f"{tool}: writes are disabled on this deployment (FR_WRITES=off).")


def _write_allowlist() -> set[int] | None:
    """`FR_WRITE_CUSTOMER_IDS`: when set, writes are refused unless they target one of these
    customers. Used to test full write capability against the live office without touching a
    real customer's record. Unset (the normal, post-testing state) means no restriction."""
    raw = os.environ.get("FR_WRITE_CUSTOMER_IDS", "").strip()
    if not raw:
        return None
    ids = {i for i in (_int(x) for x in raw.split(",")) if i is not None}
    return ids or None


def _require_customer_allowed(tool: str, customer_id: int | None) -> None:
    """Fail closed: if the allowlist is set, a write must resolve to an allowed customer."""
    allowlist = _write_allowlist()
    if allowlist is None:
        return
    if customer_id is None or customer_id not in allowlist:
        where = f"customer {customer_id}" if customer_id is not None else "an unresolved customer"
        raise ToolError(
            f"{tool}: writes are restricted to customer(s) {sorted(allowlist)} "
            f"(FR_WRITE_CUSTOMER_IDS is set); this write targets {where}."
        )


def _office_id() -> int | None:
    return _int(os.environ.get("FR_OFFICE_ID"))


def _default_employee() -> int | None:
    return _int(os.environ.get("FR_DEFAULT_EMPLOYEE_ID"))


def _default_note_type() -> int | None:
    return _int(os.environ.get("FR_DEFAULT_NOTE_TYPE_ID"))


def _default_task_category() -> int | None:
    return _int(os.environ.get("FR_DEFAULT_TASK_CATEGORY_ID"))


def _endpoint_params(entity: str, action: str) -> set[str]:
    ep = SPEC["endpoints"].get(f"{entity}/{action}")
    return {p["name"] for p in ep["params"]} if ep else set()


def _id_param(entity: str) -> str:
    """The param name `{entity}/get` takes for its bulk-ID list.

    Usually `{entity}IDs`, but roughly a quarter of entities deviate (e.g.
    `serviceType/get` takes `typeIDs`); `extract_spec.py` records the real
    one in `getIdParam` when it's not the default.
    """
    return SPEC["entities"].get(entity, {}).get("getIdParam") or f"{entity}IDs"


def _with_office(entity: str, filters: dict[str, Any]) -> dict[str, Any]:
    """Apply FR_OFFICE_ID to a search unless the caller already scoped it."""
    office = _office_id()
    if office is None or "officeID" in filters or "officeIDs" in filters:
        return filters
    params = _endpoint_params(entity, "search")
    if "officeIDs" in params:
        return {**filters, "officeIDs": office}
    if "officeID" in params:
        return {**filters, "officeID": office}
    return filters


# --- client -------------------------------------------------------------

_client: FieldRoutesClient | None = None


def client() -> FieldRoutesClient:
    """Lazy singleton; tests replace `server._client` with a mock-transport client."""
    global _client
    if _client is None:
        _client = FieldRoutesClient()
    return _client


class _LazyClient:
    """Lets generated tools bind at import time while the real client is created lazily."""

    async def call(self, entity: str, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await client().call(entity, action, params)


async def _search_ids(entity: str, filters: dict[str, Any]) -> list[int]:
    data = await client().search(entity, _with_office(entity, filters))
    return client().extract_ids(entity, data)


async def _search_rows(
    entity: str, filters: dict[str, Any], *, limit: int | None = None, extra_get: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    return await client().search_and_get(
        entity, _with_office(entity, filters), limit=limit, extra_get_params=extra_get, id_field=_id_param(entity)
    )


async def _get_rows(
    entity: str, ids: list[int], extra: dict[str, Any] | None = None, optional_extra: set[str] | None = None
) -> list[dict[str, Any]]:
    ids = sorted({i for i in (_int(x) for x in ids) if i is not None})
    if not ids:
        return []
    return await client().get(entity, ids, extra_params=extra, id_field=_id_param(entity), optional_params=optional_extra)


def _pk_param(entity: str) -> str:
    """The param name a write uses for the entity's own row, e.g. `appointmentID`.

    `note` is the one exception: its write endpoints call the note's own ID
    `contactID`, not `noteID` (confirmed against the real spec).
    """
    return "contactID" if entity == "note" else f"{entity}ID"


async def _resolve_write_customer(entity: str, params: dict[str, Any]) -> int | None:
    """Best-effort customerID a write touches, for the FR_WRITE_CUSTOMER_IDS allowlist.

    Cheap path: the write already names `customerID`/`customerIDs` directly
    (true for `customer`, `note`, `task`, `ticket`, `contract`, `payment`, ...).
    Otherwise, if the write names the entity's own row (`appointmentID`,
    `subscriptionID`, `contactID`, ...) and that entity has a `get` endpoint,
    fetch the row and read its `customerID`. Entities with neither (route,
    spot, employee, service type, office, ...) aren't customer-scoped at all
    and resolve to None -- which the allowlist then treats as unresolved and
    refuses, not as "safe to skip".
    """
    for key in ("customerID", "customerIDs"):
        if key in params:
            value = params[key]
            value = value[0] if isinstance(value, list) and value else value
            cid = _int(value)
            if cid is not None:
                return cid
    if f"{entity}/get" not in SPEC["endpoints"]:
        return None
    record_id = _int(params.get(_pk_param(entity)))
    if record_id is None:
        return None
    rows = await _get_rows(entity, [record_id])
    return _int(rows[0].get("customerID")) if rows else None


async def _write_target_customer(entity: str, record_id: int) -> int | None:
    """Look up an existing record's customerID for the allowlist check -- but only when the
    allowlist is actually set, so this costs nothing in normal (unrestricted) operation."""
    if _write_allowlist() is None:
        return None
    rows = await _get_rows(entity, [record_id])
    return _int(rows[0].get("customerID")) if rows else None


def _tool(fn: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Register `fn` as an MCP tool and surface FieldRoutesError as ToolError so Claude sees the message."""

    @wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except FieldRoutesError as exc:
            raise ToolError(str(exc)) from exc

    return mcp.tool()(wrapper)


# --- date helpers ---------------------------------------------------------


def _today() -> date:
    return date.today()


def _parse_date(value: str | None, default: date | None = None) -> date:
    if not value:
        return default or _today()
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ToolError(f"Bad date {value!r}; use YYYY-MM-DD.") from exc


def _window(start: str | None, days: int) -> tuple[str, str]:
    """Inclusive [start, start + days - 1]; the default of 7 days is today plus six."""
    s = _parse_date(start)
    e = s + timedelta(days=max(days, 1) - 1)
    return s.isoformat(), e.isoformat()


def _between(start: str, end: str) -> dict[str, Any]:
    return {"operator": "BETWEEN", "value": [start, end]}


# --- lookup caches --------------------------------------------------------

_cache: dict[str, Any] = {}


def _reset_cache() -> None:
    _cache.clear()


async def _emp_names() -> dict[int, str]:
    if "employees" not in _cache:
        rows = await _search_rows("employee", {})
        _cache["employees"] = rows
    names: dict[int, str] = {}
    for r in _cache["employees"]:
        eid = _int(r.get("employeeID"))
        if eid is not None:
            names[eid] = _name(r) or r.get("nickname") or str(eid)
    return names


async def _service_type_names() -> dict[int, str]:
    if "serviceTypes" not in _cache:
        _cache["serviceTypes"] = await _search_rows("serviceType", {})
    return {
        tid: r.get("description", "")
        for r in _cache["serviceTypes"]
        if (tid := _int(r.get("typeID"))) is not None
    }


async def _region_names() -> dict[int, str]:
    if "regions" not in _cache:
        _cache["regions"] = await _search_rows("region", {})
    out: dict[int, str] = {}
    for r in _cache["regions"]:
        rid = _int(r.get("regionID") if r.get("regionID") is not None else r.get("regionIDs"))
        if rid is not None:
            out[rid] = r.get("description", "")
    return out


def _emp(names: dict[int, str], value: Any) -> str | None:
    eid = _int(value)
    if eid in (None, 0):
        return None
    return names.get(eid, str(eid))


def _emp_list(names: dict[int, str], value: Any) -> list[str]:
    if not value:
        return []
    raw = value if isinstance(value, list) else str(value).split(",")
    return [n for n in (_emp(names, v) for v in raw) if n]


def _id_list(value: Any) -> list[int]:
    """Parse an ID-list field that may arrive as a JSON array or a comma-separated
    string. Confirmed live: `customer.subscriptionIDs` and `customer.appointmentIDs`
    are comma-separated strings (e.g. "10002,10006,16404"), not arrays, despite the
    swagger's declared `type: array` -- same pattern as `additionalTechs`. Iterating
    a raw string like that character-by-character (the old bug here) silently turns
    "12,34" into IDs 1, 2, 3, 4 instead of 12 and 34 -- always parse through this."""
    if value in (None, ""):
        return []
    raw = value if isinstance(value, (list, tuple)) else str(value).split(",")
    return [i for i in (_int(v) for v in raw) if i is not None]


async def _customers_by_id(ids: list[Any]) -> dict[int, dict[str, Any]]:
    rows = await _get_rows("customer", ids)
    return {cid: r for r in rows if (cid := _int(r.get("customerID"))) is not None}


async def _routes_by_id(ids: list[Any]) -> dict[int, dict[str, Any]]:
    rows = await _get_rows("route", ids)
    return {rid: r for r in rows if (rid := _int(r.get("routeID"))) is not None}


async def _shape_appointments(
    appts: list[dict[str, Any]], customers: dict[int, dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Trim appointments and attach customer name/address, service type, route and tech names.

    Pass `customers` when the caller already has the record (e.g. customer_360
    already fetched the one customer every appointment belongs to) to avoid a
    redundant `customer/get` call.
    """
    names, types = await _emp_names(), await _service_type_names()
    if customers is None:
        customers = await _customers_by_id([a.get("customerID") for a in appts])
    routes = await _routes_by_id([a.get("routeID") for a in appts])
    out = []
    for a in appts:
        row = _pick(a, APPOINTMENT_KEYS)
        cust = customers.get(_int(a.get("customerID")) or -1)
        if cust:
            row["customer"] = _customer_summary(cust)
        row["service"] = types.get(_int(a.get("type")) or -1, a.get("type"))
        row["statusText"] = APPOINTMENT_STATUS.get(_int(a.get("status")) or 0, a.get("statusText"))
        route = routes.get(_int(a.get("routeID")) or -1)
        if route:
            row["route"] = route.get("title")
        # `employeeID` on an appointment is who *booked* it (office staff, or the import job),
        # not who does the work -- falling back to it labeled office admins as the tech on
        # every unassigned stop (seen live). When the appointment has no tech, FieldRoutes
        # uses the route's tech, so fall back to that; otherwise leave it unassigned.
        row["tech"] = _emp(names, a.get("assignedTech")) or (_emp(names, route.get("assignedTech")) if route else None)
        booked_by = _emp(names, a.get("employeeID"))
        if booked_by:
            row["bookedBy"] = booked_by
        extra = _emp_list(names, a.get("additionalTechs"))
        if extra:
            row["additionalTechs"] = extra
        if _int(a.get("servicedBy")):
            row["servicedBy"] = _emp(names, a.get("servicedBy"))
        else:
            row.pop("servicedBy", None)  # _pick kept the raw "0"; that means nobody
        out.append(row)
    return out


async def _shape_subscriptions(
    subs: list[dict[str, Any]], customers: dict[int, dict[str, Any]] | None = None, keys: list[str] | None = None
) -> list[dict[str, Any]]:
    active_keys = keys or SUBSCRIPTION_KEYS
    names, types, regions = await _emp_names(), await _service_type_names(), await _region_names()
    out = []
    for s in subs:
        row = _pick(s, active_keys)
        row["service"] = types.get(_int(s.get("serviceID")) or -1, s.get("serviceType"))
        row["activeText"] = SUBSCRIPTION_ACTIVE.get(_int(s.get("active")) or 0, s.get("activeText"))
        row["frequencyText"] = _frequency_text(s.get("frequency"))
        if "billingFrequency" in active_keys:
            row["billingFrequencyText"] = _frequency_text(s.get("billingFrequency"))
        if _int(s.get("regionID")):  # live value is a string; "0" means unassigned, not region 0
            row["region"] = regions.get(_int(s.get("regionID")) or -1, s.get("regionID"))
        row["preferredTechName"] = _emp(names, s.get("preferredTech"))
        pd = _int(s.get("preferredDays"))
        if pd is not None and pd >= 0:
            row["preferredDayText"] = DAYS.get(pd, str(pd))
        for k in ("soldBy", "soldBy2", "soldBy3"):
            if k in active_keys and _int(s.get(k)):  # "0" = nobody; don't emit a null *Name key
                row[k + "Name"] = _emp(names, s.get(k))
        if customers is not None:
            cust = customers.get(_int(s.get("customerID")) or -1)
            if cust:
                row["customer"] = _customer_summary(cust)
        out.append(row)
    return out


# =====================================================================
# Generic tools
# =====================================================================


@_tool
async def list_endpoints(entity: str | None = None) -> str:
    """List FieldRoutes API entities, or the actions of one entity (e.g. "appointment"). Use before describe_endpoint or call."""
    endpoints = SPEC["endpoints"]
    if entity:
        actions = {
            ep["action"]: ep.get("summary") or ep["action"]
            for ep in endpoints.values()
            if ep["entity"] == entity
        }
        if not actions:
            raise ToolError(f"Unknown entity {entity!r}. Call list_endpoints() for the list.")
        return _j({"entity": entity, "actions": actions})
    by_entity: dict[str, list[str]] = {}
    for ep in endpoints.values():
        by_entity.setdefault(ep["entity"], []).append(ep["action"])
    return _j({"entities": {k: sorted(v) for k, v in sorted(by_entity.items())}, "count": len(endpoints)})


@_tool
async def describe_endpoint(endpoint: str) -> str:
    """Show the exact params (name, type, required, description) for "entity/action", plus the entity's fields for search/get. Do not invent param names; check here first."""
    ep = SPEC["endpoints"].get(endpoint.strip().strip("/"))
    if not ep:
        raise ToolError(f"Unknown endpoint {endpoint!r}; expected entity/action, see list_endpoints.")
    out: dict[str, Any] = {
        "endpoint": endpoint,
        "summary": ep.get("summary"),
        "description": ep.get("description"),
        "params": ep["params"],
    }
    if is_read_action(ep["action"]):
        fields = SPEC["entities"].get(ep["entity"], {}).get("fields", {})
        out["fields"] = {k: v.get("description", "") for k, v in fields.items()}
    return _j(out)


@_tool
async def search(
    entity: str,
    filters: dict[str, Any] | None = None,
    include_data: bool = False,
    limit: int = 100,
    fields: list[str] | None = None,
) -> str:
    """Run {entity}/search with filters (scalar, list -> IN, or {"operator": ">", "value": "0"}); returns matching IDs (capped at limit) and, with include_data, the first records. FR_OFFICE_ID is applied unless you pass officeIDs."""
    if f"{entity}/search" not in SPEC["endpoints"]:
        raise ToolError(f"No search endpoint for {entity!r}.")
    data = await client().search(entity, _with_office(entity, filters or {}), include_data=include_data)
    ids = client().extract_ids(entity, data)
    out: dict[str, Any] = {
        "entity": entity,
        "total": len(ids),
        "ids": ids[:limit],
        "truncated": len(ids) > limit,
    }
    if include_data:
        rows = client().extract_records(entity, data)[:limit]
        out["data"] = [_pick(r, fields) for r in rows] if fields else rows
        if data.get(f"{entity}IDsNoDataExported"):
            out["idsWithoutData"] = len(data[f"{entity}IDsNoDataExported"])
    return _j(out)


@_tool
async def get(entity: str, ids: list[int], include: dict[str, Any] | None = None) -> str:
    """Fetch full records for IDs via {entity}/get (chunked at 1000). `include` passes extras like {"includeSubscriptions": 1}. Returns whole records, so keep the ID list short."""
    if f"{entity}/get" not in SPEC["endpoints"]:
        raise ToolError(f"No get endpoint for {entity!r}.")
    rows = await _get_rows(entity, ids, include)
    return _j({"entity": entity, "count": len(rows), "records": rows})


@_tool
async def call(entity: str, action: str, params: dict[str, Any] | None = None) -> str:
    """Call any FieldRoutes endpoint directly (see describe_endpoint for params). Writes need FR_WRITES=on; delete needs FR_ALLOW_DELETE=1; payment/create with doCharge=1 needs FR_ALLOW_CHARGES=1. Confirm with the user before anything that cancels, charges, or deletes."""
    key = f"{entity}/{action}"
    if key not in SPEC["endpoints"]:
        raise ToolError(f"Unknown endpoint {key!r}; see list_endpoints.")
    params = dict(params or {})
    if not is_read_action(action):
        _require_writes("call")
        _require_customer_allowed(f"call({key})", await _resolve_write_customer(entity, params))
    if action == "delete" and not _allow_delete():
        raise ToolError("call: delete actions are disabled (set FR_ALLOW_DELETE=1 to enable).")
    if key == "payment/create" and str(params.get("doCharge")) in ("1", "true", "True") and not _allow_charges():
        raise ToolError("call: real card charges are disabled (set FR_ALLOW_CHARGES=1 to enable).")
    if action == "search":
        data = await client().search(entity, _with_office(entity, params))
    else:
        data = await client().call(entity, action, params)
    return _j(data)


@_tool
async def health_check() -> str:
    """Verify the FieldRoutes credentials work and report this deployment's config (office, writes on/off, delete/charge flags, tool counts)."""
    offices = await _search_rows("office", {})
    return _j(
        {
            "ok": True,
            "version": __version__,
            "baseUrl": client().base_url,
            "offices": [_pick(o, OFFICE_KEYS) for o in offices],
            "dailyUsage": client().usage.snapshot(),
            "config": {
                "officeID": _office_id(),
                "defaultEmployeeID": _default_employee(),
                "defaultNoteTypeID": _default_note_type(),
                "writes": _writes_enabled(),
                "allowDelete": _allow_delete(),
                "allowCharges": _allow_charges(),
                "writeCustomerAllowlist": sorted(_write_allowlist()) if _write_allowlist() else None,
                "generatedTools": len(_generated_tools),
            },
        }
    )


# =====================================================================
# Reads
# =====================================================================


@_tool
async def find_customer(
    query: str | None = None,
    phone: str | None = None,
    email: str | None = None,
    address: str | None = None,
    customer_id: int | None = None,
    include_inactive: bool = False,
    limit: int = 10,
) -> str:
    """Find customers by name or company ("Smith", "Jane Smith"), phone, email, street address, or ID. Returns trimmed customers with property address, balance, and subscription IDs."""
    rows: list[dict[str, Any]] = []
    base: dict[str, Any] = {} if include_inactive else {"active": 1}

    if customer_id is not None:
        rows = await _get_rows("customer", [customer_id])
    elif phone:
        # Verified live: customer/search's `phone` param is an exact 10-digit match across
        # phone1/phone2/additional contacts ("Numbers only"), and neither `phone` nor `phone1`
        # honors CONTAINS -- a CONTAINS filter silently returns zero rows. So normalize to the
        # last 10 digits and send a plain equality; partial numbers can't be searched.
        digits = "".join(ch for ch in phone if ch.isdigit())
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise ToolError("phone needs a full 10-digit number (FieldRoutes can't search partial numbers).")
        rows = await _search_rows("customer", {**base, "phone": digits}, limit=limit)
    elif email:
        rows = await _search_rows("customer", {**base, "email": {"operator": "CONTAINS", "value": email}}, limit=limit)
    elif address:
        rows = await _search_rows("customer", {**base, "address": {"operator": "CONTAINS", "value": address}}, limit=limit)
    elif query:
        parts = query.split()
        seen: set[int] = set()
        if len(parts) >= 2:
            searches = [{"fname": {"operator": "STARTSWITH", "value": parts[0]}, "lname": {"operator": "STARTSWITH", "value": " ".join(parts[1:])}}]
        else:
            searches = [
                {"lname": {"operator": "STARTSWITH", "value": query}},
                {"fname": {"operator": "STARTSWITH", "value": query}},
            ]
        for f in searches:
            for r in await _search_rows("customer", {**base, **f}, limit=limit):
                cid = _int(r.get("customerID"))
                if cid is not None and cid not in seen:
                    seen.add(cid)
                    rows.append(r)
            if len(rows) >= limit:
                break
        if not rows:
            for r in await _search_rows("customer", {**base, "companyName": {"operator": "CONTAINS", "value": query}}, limit=limit):
                cid = _int(r.get("customerID"))
                if cid is not None and cid not in seen:
                    seen.add(cid)
                    rows.append(r)
    else:
        raise ToolError("Give one of query, phone, email, address, or customer_id.")

    out = []
    for r in rows[:limit]:
        row = _pick(r, CUSTOMER_KEYS)
        row["name"] = _name(r)
        row["propertyAddress"] = _address(r)
        out.append(row)
    return _j({"count": len(out), "customers": out})


@_tool
async def customer_360(customer_id: int) -> str:
    """Everything about one customer: profile and property, flags, subscriptions (frequency, next service, preferred tech), upcoming and recent appointments, open tasks/alerts, latest notes, balance."""
    # Verified live: the spec declares these on customer/get, but Zest's account ignores all
    # three (success:true, ignoredParams: [...]) -- sent best-effort; the fallbacks below
    # (a separate subscription/get, and "flags"/"additionalContacts" defaulting to []) cover it.
    include_params = {"includeSubscriptions": 1, "includeCustomerFlag": 1, "includeAdditionalContacts": 1}
    rows = await _get_rows("customer", [customer_id], include_params, optional_extra=set(include_params))
    if not rows:
        raise ToolError(f"Customer {customer_id} not found.")
    c = rows[0]
    names = await _emp_names()

    profile = _pick(c, CUSTOMER_KEYS)
    profile["name"] = _name(c)
    profile["propertyAddress"] = _address(c)
    if c.get("preferredTechID"):
        profile["preferredTechName"] = _emp(names, c.get("preferredTechID"))
    if c.get("notes"):
        profile["redNotes"] = c["notes"]

    subs = c.get("subscriptions") if isinstance(c.get("subscriptions"), list) else None
    if subs is None:
        subs = await _get_rows("subscription", _id_list(c.get("subscriptionIDs")))

    appts = await _search_rows("appointment", {"customerIDs": [customer_id]})
    today = _today().isoformat()
    upcoming = [a for a in appts if _int(a.get("status")) == 0 and str(a.get("date", ""))[:10] >= today]
    upcoming_ids = {id(a) for a in upcoming}
    past = [a for a in appts if id(a) not in upcoming_ids]
    upcoming.sort(key=lambda a: str(a.get("date", "")))
    past.sort(key=lambda a: str(a.get("date", "")), reverse=True)

    tasks = await _search_rows("task", {"customerID": customer_id, "status": [0, 2, 3]})
    notes = await _search_rows("note", {"customerIDs": [customer_id]})
    notes.sort(key=lambda n: str(n.get("date") or n.get("dateAdded") or ""), reverse=True)

    flags = c.get("customerFlag") if isinstance(c.get("customerFlag"), list) else c.get("customerFlags")
    contacts = c.get("additionalContacts") if isinstance(c.get("additionalContacts"), list) else None
    self_only = {customer_id: c}

    return _j(
        {
            "customer": profile,
            "flags": flags or [],
            "additionalContacts": contacts or [],
            "subscriptions": await _shape_subscriptions(subs, self_only),
            "upcomingAppointments": await _shape_appointments(upcoming[:10], self_only),
            "recentAppointments": await _shape_appointments(past[:5], self_only),
            "openTasks": [_task_row(t, names) for t in tasks],
            "recentNotes": [_note_row(n, names) for n in notes[:10]],
        }
    )


def _task_row(t: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    row = _pick(t, TASK_KEYS)
    row["kind"] = "alert" if _int(t.get("type")) == 1 else "task"
    row["statusText"] = TASK_STATUS.get(_int(t.get("status")) or 0)
    if t.get("assignedTo"):
        row["assignedToName"] = _emp(names, t.get("assignedTo"))
    return row


def _note_row(n: dict[str, Any], names: dict[int, str]) -> dict[str, Any]:
    row = _pick(n, NOTE_KEYS)
    if not row.get("employeeName") and n.get("employeeID"):
        row["employeeName"] = _emp(names, n.get("employeeID"))
    return row


@_tool
async def customer_alerts(customer_id: int) -> str:
    """What a tech must know before a stop: Red Notes (if the API key can read them), special scheduling notes, flags, open alerts and urgent tasks, pending cancel, balance. Fast; use before scheduling or dispatching."""
    # Best-effort, same as customer_360: Zest's account ignores includeCustomerFlag, and the
    # fallback below (a separate customerFlag/search) covers it either way.
    rows = await _get_rows("customer", [customer_id], {"includeCustomerFlag": 1}, optional_extra={"includeCustomerFlag"})
    if not rows:
        raise ToolError(f"Customer {customer_id} not found.")
    c = rows[0]
    names = await _emp_names()
    tasks = await _search_rows("task", {"customerID": customer_id, "status": [0, 2, 3]})
    alerts = [t for t in tasks if _int(t.get("type")) == 1]
    urgent = [t for t in tasks if _int(t.get("type")) != 1 and _int(t.get("status")) == 3]
    flags = c.get("customerFlag") if isinstance(c.get("customerFlag"), list) else c.get("customerFlags")
    if flags is None:
        flags = [_pick(f, FLAG_KEYS) for f in await _search_rows("customerFlag", {"customerIDs": [customer_id]})]
    return _j(
        {
            "customer": _customer_summary(c),
            "redNotes": c.get("notes"),
            "specialScheduling": c.get("specialScheduling"),
            "flags": flags,
            "alerts": [_task_row(t, names) for t in alerts],
            "urgentTasks": [_task_row(t, names) for t in urgent],
            "pendingCancel": c.get("pendingCancel"),
            "status": c.get("statusText") or c.get("status"),
            "balance": c.get("balance"),
            "balanceAge": c.get("balanceAge"),
            "autoPay": c.get("aPay"),
            "preferredTech": _emp(names, c.get("preferredTechID")),
        }
    )


@_tool
async def day_schedule(day: str | None = None, tech: int | None = None, status: str = "pending") -> str:
    """All appointments on a date (default today), optionally for one tech (employee ID). status: pending, completed, all. Each stop has customer name, property address, service, route, tech, time window."""
    d = _parse_date(day).isoformat()
    filters: dict[str, Any] = {"date": d}
    if tech is not None:
        filters["assignedTech"] = tech
    if status == "pending":
        filters["status"] = 0
    elif status == "completed":
        filters["status"] = 1
    appts = await _search_rows("appointment", filters)
    appts.sort(key=lambda a: (_int(a.get("routeID")) or 0, _int(a.get("sequence")) or 0, str(a.get("start") or "")))
    return _j({"date": d, "count": len(appts), "appointments": await _shape_appointments(appts)})


@_tool
async def property_assignments(customer_id: int | None = None, address: str | None = None) -> str:
    """Which route/tech a property is assigned to: each subscription's preferred tech, region, frequency and next service, plus the route and tech of any upcoming appointments. Give a customer ID or a street address."""
    if customer_id is None:
        if not address:
            raise ToolError("Give customer_id or address.")
        found = await _search_rows("customer", {"address": {"operator": "CONTAINS", "value": address}}, limit=5)
        if not found:
            raise ToolError(f"No customer with an address containing {address!r}.")
        if len(found) > 1:
            return _j({"ambiguous": True, "customers": [_customer_summary(c) for c in found]})
        c = found[0]
        customer_id = _int(c.get("customerID"))
    else:
        rows = await _get_rows("customer", [customer_id])
        if not rows:
            raise ToolError(f"Customer {customer_id} not found.")
        c = rows[0]

    subs = await _get_rows("subscription", _id_list(c.get("subscriptionIDs")))
    upcoming = await _search_rows(
        "appointment", {"customerIDs": [customer_id], "status": 0, "dateStart": _today().isoformat()}
    )
    upcoming.sort(key=lambda a: str(a.get("date", "")))
    names = await _emp_names()
    return _j(
        {
            "customer": _customer_summary(c),
            "customerPreferredTech": _emp(names, c.get("preferredTechID")),
            "specialScheduling": c.get("specialScheduling"),
            "subscriptions": await _shape_subscriptions(subs),
            "upcomingAppointments": await _shape_appointments(upcoming[:10]),
        }
    )


@_tool
async def route_stops(route_id: int) -> str:
    """Stops on one route in order, with customer, property address, service, status, and distance to the previous/next stop (route density). Get route IDs from crew_schedule or day_schedule."""
    routes = await _get_rows("route", [route_id])
    if not routes:
        raise ToolError(f"Route {route_id} not found.")
    route = routes[0]
    names = await _emp_names()
    spots = await _search_rows("spot", {"routeIDs": [route_id]})
    spots.sort(key=lambda s: str(s.get("start") or ""))

    appt_ids = [s.get("currentAppointment") for s in spots if _int(s.get("currentAppointment"))]
    appts = {
        _int(a.get("appointmentID")): a
        for a in await _shape_appointments(await _get_rows("appointment", appt_ids))
    }

    stops = []
    for s in spots:
        row = _pick(s, SPOT_KEYS)
        appt = appts.get(_int(s.get("currentAppointment")))
        if appt:
            row["appointment"] = appt
        elif s.get("blockReason"):
            row["state"] = "blocked"
        elif _int(s.get("reserved")):  # live value is the *string* "0"/"1"; "0" is truthy
            row["state"] = "reserved"
        else:
            row["state"] = "open"
        stops.append(row)

    header = _pick(route, ROUTE_KEYS)
    header["tech"] = _emp(names, route.get("assignedTech"))
    header["additionalTechs"] = _emp_list(names, route.get("additionalTechs"))
    return _j({"route": header, "stopCount": len(stops), "stops": stops})


@_tool
async def crew_schedule(start: str | None = None, days: int = 7, tech: int | None = None) -> str:
    """Routes per day for a window (default today + 6 days), with assigned tech, additional techs, stop count, notes/alerts, and route density score. Filter by tech (employee ID)."""
    s, e = _window(start, days)
    filters: dict[str, Any] = {"date": _between(s, e)}
    if tech is not None:
        filters["assignedTech"] = tech
    routes = await _search_rows("route", filters)
    names = await _emp_names()

    counts: dict[int, int] = {}
    route_ids = [r.get("routeID") for r in routes]
    if route_ids:
        appts = await _search_rows("appointment", {"routeIDs": route_ids, "status": 0})
        for a in appts:
            rid = _int(a.get("routeID"))
            if rid is not None:
                counts[rid] = counts.get(rid, 0) + 1

    by_day: dict[str, list[dict[str, Any]]] = {}
    for r in sorted(routes, key=lambda r: (str(r.get("date") or ""), str(r.get("title") or ""))):
        row = _pick(r, ROUTE_KEYS)
        row["tech"] = _emp(names, r.get("assignedTech"))
        row["additionalTechs"] = _emp_list(names, r.get("additionalTechs"))
        row["pendingStops"] = counts.get(_int(r.get("routeID")) or -1, 0)
        by_day.setdefault(str(r.get("date") or "")[:10], []).append(row)
    return _j({"from": s, "to": e, "routeCount": len(routes), "days": by_day})


@_tool
async def due_for_service(
    start: str | None = None,
    days: int = 7,
    service_type_id: int | None = None,
    region_id: int | None = None,
    tech: int | None = None,
    limit: int = 50,
) -> str:
    """Active subscriptions whose next service falls in a window (default today + 6 days), with customer, property address and lat/lng, service, frequency, preferred tech/day, last completed. Filter by service type, region, or preferred tech to narrow a large office's results; raise `limit` for a bigger pull once narrowed."""
    s, e = _window(start, days)
    filters: dict[str, Any] = {"active": 1, "nextService": _between(s, e)}
    if service_type_id is not None:
        filters["serviceID"] = service_type_id
    if region_id is not None:
        filters["regionID"] = region_id
    if tech is not None:
        filters["preferredTech"] = tech
    subs = await _search_rows("subscription", filters, limit=limit)
    subs.sort(key=lambda x: str(x.get("nextService") or ""))
    customers = await _customers_by_id([x.get("customerID") for x in subs])
    shaped = await _shape_subscriptions(subs, customers, keys=DUE_FOR_SERVICE_KEYS)
    return _j({"from": s, "to": e, "count": len(subs), "subscriptions": shaped})


@_tool
async def open_slots(
    start: str | None = None,
    days: int = 7,
    tech: int | None = None,
    route_id: int | None = None,
    limit: int = 100,
) -> str:
    """Open, API-schedulable spots in a window (default today + 6 days), with route, tech, time, and distance to the neighbouring stops so you can pick the densest fit. Use the spotID with schedule_appointment or reserve_slot."""
    s, e = _window(start, days)
    filters: dict[str, Any] = {"date": _between(s, e), "apiCanSchedule": 1}
    if tech is not None:
        filters["assignedTech"] = tech
    if route_id is not None:
        filters["routeIDs"] = [route_id]
    spots = await _search_rows("spot", filters)
    open_ = [
        sp
        for sp in spots
        if not _int(sp.get("currentAppointment")) and not sp.get("blockReason") and not _int(sp.get("reserved"))
    ]
    open_.sort(key=lambda sp: (str(sp.get("date") or ""), str(sp.get("start") or "")))
    open_ = open_[:limit]
    names = await _emp_names()
    routes = await _routes_by_id([sp.get("routeID") for sp in open_])
    out = []
    for sp in open_:
        row = _pick(sp, SPOT_KEYS)
        route = routes.get(_int(sp.get("routeID")) or -1)
        if route:
            row["route"] = route.get("title")
        row["tech"] = _emp(names, sp.get("assignedTech")) or (_emp(names, route.get("assignedTech")) if route else None)
        out.append(row)
    return _j({"from": s, "to": e, "count": len(out), "totalSchedulable": len(spots), "spots": out})


@_tool
async def ar_aging(min_balance: float = 0.01, min_age_days: int = 0, limit: int = 100, include_inactive: bool = False) -> str:
    """Who owes money: customers with a balance over min_balance (and at least min_age_days old), sorted largest first, bucketed 0-30/31-60/61-90/90+ days, with phone, email, autopay status, property address."""
    filters: dict[str, Any] = {"balance": {"operator": ">", "value": str(min_balance)}}
    if min_age_days > 0:
        filters["balanceAge"] = {"operator": ">=", "value": str(min_age_days)}
    if not include_inactive:
        filters["active"] = 1
    rows = await _search_rows("customer", filters)
    rows.sort(key=lambda r: float(r.get("balance") or 0), reverse=True)
    buckets = {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}
    out = []
    for r in rows:
        bal = float(r.get("balance") or 0)
        age = _int(r.get("balanceAge")) or 0
        key = "0-30" if age <= 30 else "31-60" if age <= 60 else "61-90" if age <= 90 else "90+"
        buckets[key] += bal
        if len(out) < limit:
            row = _customer_summary(r)
            row.update(_pick(r, ["balance", "balanceAge", "responsibleBalance", "aPay", "email", "phone2", "agingDate", "status", "pendingCancel"]))
            row["bucket"] = key
            out.append(row)
    return _j(
        {
            "customerCount": len(rows),
            "totalOutstanding": round(sum(float(r.get("balance") or 0) for r in rows), 2),
            "buckets": {k: round(v, 2) for k, v in buckets.items()},
            "customers": out,
        }
    )


@_tool
async def lookups(kind: str = "all", refresh: bool = False) -> str:
    """IDs you need for other tools: employees (techs/office/sales), service types, regions, offices, cancellation and reschedule reasons, customer sources, and the task categories in use (create_task needs one; they're per-office with no listing endpoint, so this reads them off existing tasks). kind picks one list or "all"; refresh re-reads FieldRoutes. Note Types have no API endpoint either; set FR_DEFAULT_NOTE_TYPE_ID."""
    if refresh:
        _reset_cache()
    if kind in ("all", "task_categories"):
        if "taskCategories" not in _cache:
            # No taskCategory endpoint exists (verified against the spec), and task/create only
            # accepts the office's own IDs -- so collect the (id, description) pairs FieldRoutes
            # attaches to existing tasks. Human-made tasks with no category carry "0" and no
            # description; those aren't creatable values, so skip them.
            cats: dict[int, str] = {}
            for t in await _search_rows("task", {}):
                cid = _int(t.get("category"))
                if cid and t.get("categoryDescription"):
                    cats[cid] = _clean(t.get("categoryDescription"))
            _cache["taskCategories"] = [{"categoryID": k, "description": v} for k, v in sorted(cats.items())]
        if kind == "task_categories":
            return _j({"task_categories": _cache["taskCategories"]})
    kinds = {
        "employees": ("employee", EMPLOYEE_KEYS, "employees"),
        "service_types": ("serviceType", SERVICE_TYPE_KEYS, "serviceTypes"),
        "regions": ("region", REGION_KEYS, "regions"),
        "offices": ("office", OFFICE_KEYS, "offices"),
        "cancellation_reasons": ("appointmentCancellationReason", None, "cancellationReasons"),
        "subscription_cancellation_reasons": ("cancellationReason", None, "subscriptionCancellationReasons"),
        "reschedule_reasons": ("appointmentRescheduleReason", None, "rescheduleReasons"),
        "reservice_reasons": ("reserviceReason", None, "reserviceReasons"),
        "customer_sources": ("customerSource", None, "customerSources"),
    }
    if kind != "all" and kind not in kinds:
        raise ToolError(f"kind must be one of: all, {', '.join(kinds)}, task_categories")
    out: dict[str, Any] = {}
    if kind == "all":
        out["task_categories"] = _cache["taskCategories"]
    for name, (entity, keys, cache_key) in kinds.items():
        if kind not in ("all", name):
            continue
        if cache_key not in _cache:
            _cache[cache_key] = await _search_rows(entity, {})
        rows = _cache[cache_key]
        if entity == "employee":
            out[name] = [
                {**_pick(r, EMPLOYEE_KEYS), "name": _name(r), "typeText": EMPLOYEE_TYPES.get(_int(r.get("type")) or 0)}
                for r in rows
            ]
        else:
            out[name] = [_pick(r, keys) if keys else r for r in rows]
    return _j(out)


@_tool
async def subscription_details(subscription_id: int) -> str:
    """One subscription's full setup as the office sees it: service, job frequency, billing frequency, seasonal window, custom date, region, preferred tech/day/time, duration, call-ahead, sales rep, source, sold date, contract length, expiration, PO, plus its upcoming appointments. Use before update_subscription."""
    subs = await _get_rows("subscription", [subscription_id])
    if not subs:
        raise ToolError(f"Subscription {subscription_id} not found.")
    sub = subs[0]
    customers = await _customers_by_id([sub.get("customerID")])
    appts = await _search_rows("appointment", {"subscriptionIDs": [subscription_id], "status": 0})
    appts.sort(key=lambda a: str(a.get("date", "")))
    shaped = await _shape_subscriptions([sub], customers)
    return _j({"subscription": shaped[0], "upcomingAppointments": await _shape_appointments(appts[:10])})


@_tool
async def list_notes(customer_id: int, limit: int = 50, note_type_id: int | None = None) -> str:
    """All notes on a customer's account (newest first), with author, note type, and tech/customer visibility. Red Notes are separate: see customer_alerts."""
    filters: dict[str, Any] = {"customerIDs": [customer_id]}
    if note_type_id is not None:
        filters["typeIDs"] = note_type_id
    notes = await _search_rows("note", filters)
    notes.sort(key=lambda n: str(n.get("date") or n.get("dateAdded") or ""), reverse=True)
    names = await _emp_names()
    return _j({"count": len(notes), "notes": [_note_row(n, names) for n in notes[:limit]]})


# =====================================================================
# Writes
# =====================================================================


@_tool
async def add_note(
    customer_id: int,
    text: str,
    note_type_id: int | None = None,
    show_tech: bool = False,
    show_customer: bool = False,
    subscription_id: int | None = None,
    employee_id: int | None = None,
    day: str | None = None,
) -> str:
    """Add a note to a customer's account. Keep it to two sentences. note_type_id defaults to FR_DEFAULT_NOTE_TYPE_ID; show_tech puts it on the tech's mobile app."""
    _require_writes("add_note")
    _require_customer_allowed("add_note", customer_id)
    note_type = note_type_id if note_type_id is not None else _default_note_type()
    if note_type is None:
        raise ToolError("add_note: give note_type_id or set FR_DEFAULT_NOTE_TYPE_ID (Admin > Preferences > Note Types).")
    params: dict[str, Any] = {
        "customerID": customer_id,
        "date": _parse_date(day).isoformat(),
        "contactType": note_type,
        "notes": text,
        "showOnInvoice": 0,
        "showTech": 1 if show_tech else 0,
        "showCustomer": 1 if show_customer else 0,
        "employeeID": employee_id or _default_employee(),
        "referenceID": subscription_id,
    }
    return _j(await client().call("note", "create", params))


@_tool
async def update_note(
    note_id: int,
    text: str | None = None,
    show_tech: bool | None = None,
    show_customer: bool | None = None,
    note_type_id: int | None = None,
) -> str:
    """Edit an existing note's text, type, or tech/customer visibility (get note IDs from list_notes). Fields you leave out keep their current value."""
    _require_writes("update_note")
    rows = await _get_rows("note", [note_id])
    if not rows:
        raise ToolError(f"Note {note_id} not found.")
    n = rows[0]
    _require_customer_allowed("update_note", _int(n.get("customerID")))
    params: dict[str, Any] = {
        "contactID": note_id,
        "customerID": n.get("customerID"),
        "date": str(n.get("date") or _today().isoformat())[:10],
        "contactType": note_type_id or n.get("typeID"),
        "showOnInvoice": 0,
        "notes": text if text is not None else n.get("notes"),
        "showTech": (1 if show_tech else 0) if show_tech is not None else n.get("showTech"),
        "showCustomer": (1 if show_customer else 0) if show_customer is not None else n.get("showCustomer"),
        "employeeID": n.get("employeeID") or _default_employee(),
        "referenceID": n.get("referenceID"),
    }
    return _j(await client().call("note", "update", params))


@_tool
async def set_red_notes(customer_id: int, text: str) -> str:
    """Replace a customer's Red Notes (the always-visible account warning). This overwrites the existing Red Notes, which the API cannot read back, so confirm the full new text with the user first."""
    _require_writes("set_red_notes")
    _require_customer_allowed("set_red_notes", customer_id)
    return _j(await client().call("customer", "update", {"customerID": customer_id, "notes": text}))


@_tool
async def create_task(
    customer_id: int,
    text: str,
    due: str | None = None,
    alert: bool = False,
    urgent: bool = False,
    assigned_to: int | None = None,
    category: int | None = None,
    subscription_id: int | None = None,
    phone: str | None = None,
) -> str:
    """Create a task, or an alert (alert=True) that pops for the office/tech on the account. category is a Task Category ID and is required by FieldRoutes -- they're per-office (get them from lookups(kind="task_categories")); it defaults to FR_DEFAULT_TASK_CATEGORY_ID."""
    _require_writes("create_task")
    _require_customer_allowed("create_task", customer_id)
    # Verified live: task/create rejects a missing category ("category required"), 0
    # ("must be a positive integer") and the spec's built-in IDs like 10 ("not a valid
    # taskCategoryID for the office") -- only the office's own categories are accepted.
    category = category if category is not None else _default_task_category()
    if category is None:
        raise ToolError(
            "create_task: give category or set FR_DEFAULT_TASK_CATEGORY_ID -- Task Categories are "
            "per-office; lookups(kind=\"task_categories\") lists the ones in use."
        )
    params: dict[str, Any] = {
        "type": 1 if alert else 0,
        "customerID": customer_id,
        "task": text,
        "dueDate": _parse_date(due).isoformat() if due else None,
        "status": 3 if urgent else 0,
        "assignedTo": assigned_to,
        "addedBy": _default_employee(),
        "category": category,
        "referenceID": subscription_id,
        "phone": phone,
    }
    return _j(await client().call("task", "create", params))


@_tool
async def schedule_appointment(
    customer_id: int,
    service_type_id: int,
    spot_id: int | None = None,
    route_id: int | None = None,
    subscription_id: int | None = None,
    start: str | None = None,
    end: str | None = None,
    duration: int | None = None,
    tech: int | None = None,
    notes: str | None = None,
    reservation: str | None = None,
    allow_double_booking: bool = False,
) -> str:
    """Book an appointment into a spot (from open_slots) or onto a route. Pass the reservation token from reserve_slot if you held the spot. Fails rather than double-booking unless allow_double_booking."""
    _require_writes("schedule_appointment")
    _require_customer_allowed("schedule_appointment", customer_id)
    if spot_id is None and route_id is None:
        raise ToolError("schedule_appointment: give spot_id (preferred; see open_slots) or route_id.")
    params: dict[str, Any] = {
        "customerID": customer_id,
        "type": service_type_id,
        "spotID": spot_id,
        "routeID": route_id,
        "subscriptionID": subscription_id,
        "start": start,
        "end": end,
        "duration": duration,
        "assignedTech": tech,
        "employeeID": _default_employee(),
        "notes": notes,
        "reservation": reservation,
        "rejectOccupiedSpots": 0 if allow_double_booking else 1,
    }
    return _j(await client().call("appointment", "create", params))


@_tool
async def reschedule_appointment(
    appointment_id: int,
    spot_id: int | None = None,
    route_id: int | None = None,
    start: str | None = None,
    end: str | None = None,
    duration: int | None = None,
    tech: int | None = None,
    reservation: str | None = None,
    allow_double_booking: bool = False,
) -> str:
    """Move an existing appointment to another spot or route, or change its time window, duration, or tech. Confirm the new slot with the user first."""
    _require_writes("reschedule_appointment")
    _require_customer_allowed("reschedule_appointment", await _write_target_customer("appointment", appointment_id))
    if all(v is None for v in (spot_id, route_id, start, end, duration, tech)):
        raise ToolError("reschedule_appointment: give at least one of spot_id, route_id, start, end, duration, tech.")
    params: dict[str, Any] = {
        "appointmentID": appointment_id,
        "spotID": spot_id,
        "routeID": route_id,
        "start": start,
        "end": end,
        "duration": duration,
        "assignedTech": tech,
        "reservation": reservation,
        "rejectOccupiedSpots": 0 if allow_double_booking else 1,
    }
    return _j(await client().call("appointment", "update", params))


@_tool
async def update_appointment_notes(appointment_id: int, notes: str) -> str:
    """Set the visit's notes (the text shown on the appointment, i.e. what the tech sees before the stop). Pass an empty string to clear. Office-only appointment notes can't be set through the FieldRoutes API (appointment/update has no such field; verified live), only in the FieldRoutes UI."""
    _require_writes("update_appointment_notes")
    _require_customer_allowed("update_appointment_notes", await _write_target_customer("appointment", appointment_id))
    # Verified live: the `notes` param lands in the record's `appointmentNotes`; there is no
    # office-notes param at all -- sending `officeNotes` came back success + ignoredParams.
    return _j(await client().call("appointment", "update", {"appointmentID": appointment_id, "notes": notes}))


@_tool
async def cancel_appointment(appointment_id: int, reason: str | None = None) -> str:
    """Cancel an appointment. Destructive: confirm the appointment (customer, date, service) with the user before calling."""
    _require_writes("cancel_appointment")
    _require_customer_allowed("cancel_appointment", await _write_target_customer("appointment", appointment_id))
    params: dict[str, Any] = {"appointmentID": appointment_id, "cancelReason": reason, "cancelledBy": _default_employee()}
    return _j(await client().call("appointment", "cancel", params))


@_tool
async def complete_appointment(
    appointment_id: int,
    no_show: bool = False,
    completion_notes: str | None = None,
    office_notes: str | None = None,
    tech: int | None = None,
    time_in: str | None = None,
    time_out: str | None = None,
    amount_collected: float | None = None,
    payment_method: int | None = None,
) -> str:
    """Mark an appointment completed (or a no-show), optionally with notes, check-in/out times, and money collected. Generates the invoice in FieldRoutes, so confirm with the user first."""
    _require_writes("complete_appointment")
    _require_customer_allowed("complete_appointment", await _write_target_customer("appointment", appointment_id))
    params: dict[str, Any] = {
        "appointmentID": appointment_id,
        "status": 2 if no_show else 1,
        "completionNotes": completion_notes,
        "officeNotes": office_notes,
        "employeeID": tech,
        "completedBy": _default_employee(),
        "timeIn": time_in,
        "timeOut": time_out,
        "amountCollected": amount_collected,
        "paymentMethod": payment_method,
    }
    return _j(await client().call("appointment", "complete", params))


@_tool
async def reserve_slot(spot_id: int | None = None, spot_ids: list[int] | None = None, minutes: int = 15) -> str:
    """Hold a spot (or the first available of several) for a few minutes while confirming with the customer; returns the reservation token for schedule_appointment."""
    # Exempt from FR_WRITE_CUSTOMER_IDS: a spot hold is routing capacity, not
    # customer data -- it names no customer and expires on its own.
    _require_writes("reserve_slot")
    if spot_id is None and not spot_ids:
        raise ToolError("reserve_slot: give spot_id or spot_ids.")
    params: dict[str, Any] = {"spotID": spot_id, "spotOptions": spot_ids, "duration": minutes}
    return _j(await client().call("spot", "reserve", params))


@_tool
async def update_subscription(
    subscription_id: int,
    frequency: int | None = None,
    billing_frequency: int | None = None,
    custom_date: str | None = None,
    custom_schedule_id: int | None = None,
    seasonal_start: str | None = None,
    seasonal_end: str | None = None,
    region_id: int | None = None,
    preferred_tech: int | None = None,
    preferred_day: int | None = None,
    preferred_start: str | None = None,
    preferred_end: str | None = None,
    duration: int | None = None,
    call_ahead: int | None = None,
    followup_delay: int | None = None,
    service_type_id: int | None = None,
    active: int | None = None,
    sold_by: int | None = None,
    sold_by2: int | None = None,
    sold_by3: int | None = None,
    source_id: int | None = None,
    agreement_length: int | None = None,
    expiration_date: str | None = None,
    po_number: str | None = None,
    next_billing_date: str | None = None,
    renewal_date: str | None = None,
) -> str:
    """Change a subscription's setup: job/visit frequency (days; -1 one-time, 0 as-needed, multiples of 30 = months; or custom_schedule_id for a custom schedule), billing_frequency, custom_date (next visit), seasonal window, region, preferred tech/day (0=Sun..6=Sat)/time window, duration, call-ahead, sales reps, source, contract length, expiration, PO. Only the fields you pass change. active=0 freezes the subscription: confirm with the user first."""
    _require_writes("update_subscription")
    _require_customer_allowed("update_subscription", await _write_target_customer("subscription", subscription_id))
    params: dict[str, Any] = {
        "subscriptionID": subscription_id,
        "frequency": frequency,
        "billingFrequency": billing_frequency,
        "customDate": _parse_date(custom_date).isoformat() if custom_date else None,
        "customScheduleID": custom_schedule_id,
        "seasonalStart": seasonal_start,
        "seasonalEnd": seasonal_end,
        "regionID": region_id,
        "preferredTech": preferred_tech,
        "preferredDays": preferred_day,
        "preferredStart": preferred_start,
        "preferredEnd": preferred_end,
        "duration": duration,
        "callAhead": call_ahead,
        "followupDelay": followup_delay,
        "serviceID": service_type_id,
        "active": active,
        "soldBy": sold_by,
        "soldBy2": sold_by2,
        "soldBy3": sold_by3,
        "sourceID": source_id,
        "agreementLength": agreement_length,
        "expirationDate": expiration_date,
        "poNumber": po_number,
        "nextBillingDate": next_billing_date,
        "renewalDate": renewal_date,
    }
    changes = {k: v for k, v in params.items() if v is not None and k != "subscriptionID"}
    if not changes:
        raise ToolError("update_subscription: nothing to change; pass at least one field.")
    result = await client().call("subscription", "update", params)
    return _j({"changed": changes, "result": result})


# =====================================================================
# Reads + writes combined
# =====================================================================


@_tool
async def service_schedule(
    service_type_id: int | None = None,
    region_id: int | None = None,
    start: str | None = None,
    days: int = 90,
    status: str = "all",
    move: list[dict[str, Any]] | None = None,
) -> str:
    """Appointment history and upcoming schedule for a service type and/or zone (region), grouped by subscription -- each group shows the zone, customer, and every appointment (completed and pending) in the window, sorted by date. Appointments with no subscription (stand-alone services/reservices) get their own group per customer instead of being merged together. Default window is today + 89 days; pass a past `start` to look at history. status: all, pending, completed.

    To move one or more appointments to a new day in the same call, pass `move`: a list of objects like {"appointment_id": 500, "spot_id": 32}. spot_id must come from open_slots -- FieldRoutes has no bare "move to this date" field, an appointment's date comes from the spot (or route) it's booked into, exactly like reschedule_appointment (each move entry accepts the same fields: spot_id, route_id, start, end, duration, tech, reservation, allow_double_booking). Moves are applied first, one at a time, and stop at the first failure -- earlier moves in the same call are NOT rolled back, so `moved` always reflects what actually happened even when a later entry fails. Confirm every move with the user before calling, same as reschedule_appointment."""
    moved: list[dict[str, Any]] = []
    if move:
        appt_ids: list[int] = []
        for entry in move:
            appointment_id = _int(entry.get("appointment_id"))
            if appointment_id is None:
                raise ToolError("service_schedule: each move entry needs a numeric appointment_id.")
            appt_ids.append(appointment_id)
        _require_writes("service_schedule(move)")
        # Batch-resolve the allowlist customer for every target appointment up front (one
        # `get`), instead of the N single-ID lookups `reschedule_appointment` would each do.
        target_customers: dict[int, int | None] = {}
        if _write_allowlist() is not None:
            rows = await _get_rows("appointment", appt_ids)
            by_id = {aid: row for row in rows if (aid := _int(row.get("appointmentID"))) is not None}
            target_customers = {aid: _int(by_id[aid].get("customerID")) if aid in by_id else None for aid in appt_ids}
        # A failure on the very first attempted move (nothing has changed yet) raises, same as
        # every other write tool -- fail closed and loud. Once at least one move in this batch
        # has actually happened, a later failure is captured into `moved` instead of raised, so
        # that real state change is never silently discarded by an exception.
        for entry, appointment_id in zip(move, appt_ids):
            if appointment_id in target_customers:
                try:
                    _require_customer_allowed("service_schedule(move)", target_customers[appointment_id])
                except ToolError as exc:
                    if not moved:
                        raise
                    moved.append({"appointmentID": appointment_id, "error": str(exc)})
                    break
            if all(entry.get(k) is None for k in ("spot_id", "route_id", "start", "end", "duration", "tech")):
                message = f"appointment {appointment_id}: give at least one of spot_id, route_id, start, end, duration, tech."
                if not moved:
                    raise ToolError(f"service_schedule: {message}")
                moved.append({"appointmentID": appointment_id, "error": message})
                break
            params: dict[str, Any] = {
                "appointmentID": appointment_id,
                "spotID": entry.get("spot_id"),
                "routeID": entry.get("route_id"),
                "start": entry.get("start"),
                "end": entry.get("end"),
                "duration": entry.get("duration"),
                "assignedTech": entry.get("tech"),
                "reservation": entry.get("reservation"),
                "rejectOccupiedSpots": 0 if entry.get("allow_double_booking") else 1,
            }
            try:
                result = await client().call("appointment", "update", params)
            except FieldRoutesError as exc:
                if not moved:
                    raise
                moved.append({"appointmentID": appointment_id, "error": str(exc)})
                break
            moved.append({"appointmentID": appointment_id, "result": result})

    window_start, window_end = _window(start, days)
    filters: dict[str, Any] = {"dateStart": window_start, "dateEnd": window_end}
    if service_type_id is not None:
        filters["serviceIDs"] = service_type_id
    if status == "pending":
        filters["status"] = 0
    elif status == "completed":
        filters["status"] = 1
    elif status != "all":
        raise ToolError("service_schedule: status must be one of: all, pending, completed.")
    if region_id is not None:
        # Push the region filter down to subscription/search (same pattern as due_for_service)
        # instead of fetching every appointment in the window and filtering in memory.
        region_sub_ids = await _search_ids("subscription", {"regionID": region_id})
        if not region_sub_ids:
            return _j(
                {
                    "from": window_start,
                    "to": window_end,
                    "serviceTypeID": service_type_id,
                    "regionID": region_id,
                    "subscriptionCount": 0,
                    "appointmentCount": 0,
                    "moved": moved,
                    "groups": [],
                }
            )
        filters["subscriptionIDs"] = region_sub_ids
    appts = await _search_rows("appointment", filters)

    # subscriptionID is -1 for stand-alone services/reservices -- not a real subscription to
    # look up, and not the same as "no subscription" (None) either; treat both as unassigned.
    sub_ids = sorted({sid for a in appts if (sid := _int(a.get("subscriptionID"))) not in (None, -1)})
    subs_by_id = {
        sid: sub for sub in await _get_rows("subscription", sub_ids) if (sid := _int(sub.get("subscriptionID"))) is not None
    }
    cust_ids = {cid for sub in subs_by_id.values() if (cid := _int(sub.get("customerID"))) is not None}
    cust_ids |= {cid for a in appts if (cid := _int(a.get("customerID"))) is not None}
    customers = await _customers_by_id(sorted(cust_ids))
    regions = await _region_names()

    shaped = await _shape_appointments(appts, customers)
    # Group by subscription when there is a real one; appointments with no subscription (or
    # the -1 stand-alone sentinel) group by their own customer instead, so two different
    # customers' stand-alone appointments never collapse into one shared "unassigned" group.
    groups: dict[tuple[str, int | None], list[dict[str, Any]]] = {}
    for raw, row in zip(appts, shaped):
        raw_sid = _int(raw.get("subscriptionID"))
        sid = raw_sid if raw_sid not in (None, -1) else None
        key = ("sub", sid) if sid is not None else ("cust", _int(raw.get("customerID")))
        groups.setdefault(key, []).append(row)

    out_groups = []
    for (kind, key_id), rows in groups.items():
        sub_id = key_id if kind == "sub" else None
        sub = subs_by_id.get(sub_id) if sub_id is not None else None
        sub_region_id = _int(sub.get("regionID")) if sub else None
        cust_id = _int(sub.get("customerID")) if sub else key_id
        cust = customers.get(cust_id) if cust_id is not None else None
        rows.sort(key=lambda a: str(a.get("date", "")))
        out_groups.append(
            {
                "subscriptionID": sub_id,
                "zone": regions.get(sub_region_id) if sub_region_id is not None else None,
                "customer": _customer_summary(cust) if cust else None,
                "frequencyText": _frequency_text(sub.get("frequency")) if sub else None,
                "appointmentCount": len(rows),
                "appointments": rows,
            }
        )
    out_groups.sort(key=lambda g: (g["zone"] or "", (g["customer"] or {}).get("name") or ""))

    return _j(
        {
            "from": window_start,
            "to": window_end,
            "serviceTypeID": service_type_id,
            "regionID": region_id,
            "subscriptionCount": len(out_groups),
            "appointmentCount": len(appts),
            "moved": moved,
            "groups": out_groups,
        }
    )


# =====================================================================
# Generated tools + HTTP app + entrypoint
# =====================================================================

_generated_tools: list[str] = register_generated_tools(
    mcp,
    _LazyClient(),  # type: ignore[arg-type]
    SPEC,
    writes_enabled=_writes_enabled,
    allow_delete=_allow_delete,
    allow_charges=_allow_charges,
    resolve_write_customer=_resolve_write_customer,
    check_customer_allowed=_require_customer_allowed,
)


def _mcp_path() -> str:
    secret = os.environ.get("MCP_PATH_SECRET", "").strip().strip("/")
    return f"/{secret}/mcp" if secret else "/mcp"


class _BearerAuth:
    """Optional second lock: require `Authorization: Bearer <MCP_BEARER_TOKEN>` on everything but /healthz."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("path") != "/healthz":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
            if headers.get("authorization") != f"Bearer {self.token}":
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


def build_http_app() -> Any:
    app = mcp.streamable_http_app(
        streamable_http_path=_mcp_path(),
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        host="0.0.0.0",
    )
    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    return _BearerAuth(app, token) if token else app


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "").strip().lower()
    print(
        f"fr-mcp {__version__}: {len(_generated_tools)} generated tools, writes={'on' if _writes_enabled() else 'off'}, "
        f"delete={'on' if _allow_delete() else 'off'}, charges={'on' if _allow_charges() else 'off'}",
        file=sys.stderr,
    )
    if transport == "http":
        import uvicorn

        port = int(os.environ.get("PORT", "8080"))
        path_note = "/<secret>/mcp" if os.environ.get("MCP_PATH_SECRET", "").strip() else _mcp_path()
        print(f"fr-mcp: serving http on :{port}{path_note}", file=sys.stderr)
        # access_log=False: uvicorn's default access log prints each request's
        # path, which would put the secret path segment in Railway's logs on
        # every single call.
        uvicorn.run(build_http_app(), host="0.0.0.0", port=port, log_level="info", access_log=False)
    else:
        mcp.run("stdio")


if __name__ == "__main__":
    main()
