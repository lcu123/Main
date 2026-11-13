"""Everything this server asks of FieldRoutes, in one place.

Reuses the MCP server's `FieldRoutesClient` (same auth, wire format, rate
limit and daily quota), so the quirks written up in CLAUDE.md are handled
once. The reads are sized for the shared 3,000-a-day quota: a full refresh of
the day is one `appointment/search` plus, at most, one `customer/get`.

Rules carried over from the MCP server, because live data broke the obvious code:
- every field value arrives as a string, so nothing is truth-tested raw;
- an appointment's `employeeID` is who booked it; the tech is `assignedTech`
  (checked against a real day: it matched the route's tech and `servicedBy`
  on all 44 completed appointments), falling back to the route's tech;
- names and addresses carry stray whitespace.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from importlib import resources
from typing import Any
from zoneinfo import ZoneInfo

from fr_mcp.client import FieldRoutesClient, FieldRoutesError

from .config import Config

__all__ = ["FieldRoutes", "FieldRoutesError", "WriteBlocked"]

_SPEC: dict[str, Any] | None = None


def _spec() -> dict[str, Any]:
    global _SPEC
    if _SPEC is None:
        _SPEC = json.loads(resources.files("fr_mcp").joinpath("fieldroutes_spec.json").read_text())
    return _SPEC


def to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


INT32_MAX = 2_147_483_647


def to_id(value: Any) -> int | None:
    """An ID from outside: a whole number that fits the database's integer column, or None.
    (Postgres refuses anything larger outright, which would otherwise surface as a crash.)"""
    number = to_int(value)
    if number is None or isinstance(value, bool) or not -INT32_MAX <= number <= INT32_MAX:
        return None
    return number


def to_float(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f != 0.0 else None  # FieldRoutes stores "no map pin" as 0


def clean(value: Any) -> str:
    """Collapse whitespace and drop NUL characters (Postgres refuses them in text)."""
    return " ".join(str(value).replace("\x00", "").split()) if value is not None else ""


def id_list(value: Any) -> list[int]:
    """ID lists arrive as JSON arrays, comma-separated strings, or a bare scalar."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        items: list[Any] = list(value)
    else:
        items = str(value).split(",")
    return [i for i in (to_int(str(x).strip()) for x in items) if i is not None and i > 0]


def local_time(value: Any, tz: ZoneInfo) -> datetime | None:
    """FieldRoutes datetimes are office-local with no zone; '0000-00-00 00:00:00' means never."""
    text = clean(value)
    if not text or text.startswith("0000"):
        return None
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=tz).astimezone(timezone.utc)


class WriteBlocked(Exception):
    """A FieldRoutes write this deployment is configured not to make (FR_WRITES=off, or the
    customer is outside FR_WRITE_CUSTOMER_IDS). Not an error to retry."""


class FieldRoutes:
    def __init__(self, client: FieldRoutesClient, cfg: Config):
        self.client = client
        self.cfg = cfg
        self._service_names: dict[int, str] | None = None

    # -- plumbing ----------------------------------------------------------

    @staticmethod
    def _id_param(entity: str) -> str:
        return _spec()["entities"].get(entity, {}).get("getIdParam") or f"{entity}IDs"

    def _with_office(self, entity: str, filters: dict[str, Any]) -> dict[str, Any]:
        office = self.cfg.office_id
        if office is None:
            return filters
        ep = _spec()["endpoints"].get(f"{entity}/search") or {}
        names = {p["name"] for p in ep.get("params", [])}
        if "officeIDs" in names:
            return {**filters, "officeIDs": office}
        if "officeID" in names:
            return {**filters, "officeID": office}
        return filters

    async def search_rows(self, entity: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        """One call for up to 1,000 records; anything past that is fetched by ID."""
        data = await self.client.search(entity, self._with_office(entity, filters), include_data=True)
        rows = self.client.extract_records(entity, data)
        rest = id_list(data.get(f"{entity}IDsNoDataExported"))
        if rest:
            rows = rows + await self.client.get(entity, rest, id_field=self._id_param(entity))
        return rows

    async def get_rows(self, entity: str, ids: list[int]) -> list[dict[str, Any]]:
        ids = sorted({i for i in ids if i})
        if not ids:
            return []
        return await self.client.get(entity, ids, id_field=self._id_param(entity))

    # -- reads -------------------------------------------------------------

    async def technicians(self) -> list[dict[str, Any]]:
        """Active employees of type 1 (technician): the only people who may get a login code."""
        rows = await self.search_rows("employee", {"active": 1})
        out = []
        for r in rows:
            if to_int(r.get("type")) != 1 or to_int(r.get("active")) != 1:
                continue
            emp_id = to_int(r.get("employeeID"))
            if emp_id is None:
                continue
            out.append(
                {
                    "id": emp_id,
                    "fname": clean(r.get("fname")),
                    "lname": clean(r.get("lname")),
                    "email": clean(r.get("email")).lower(),
                    "phone": "".join(ch for ch in clean(r.get("phone")) if ch.isdigit())[-10:],
                }
            )
        return out

    async def day_appointments(self, day: str) -> list[dict[str, Any]]:
        """Pending and completed appointments for one office-local date. Cancelled (-1)
        and no-show (2) are left out: nobody films those."""
        rows = await self.search_rows("appointment", {"date": day})
        return [r for r in rows if to_int(r.get("status")) in (0, 1)]

    async def day_routes(self, day: str) -> dict[int, dict[str, Any]]:
        rows = await self.search_rows("route", {"date": day})
        return {rid: r for r in rows if (rid := to_int(r.get("routeID"))) is not None}

    async def customers(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        rows = await self.get_rows("customer", ids)
        return {cid: r for r in rows if (cid := to_int(r.get("customerID"))) is not None}

    async def service_names(self) -> dict[int, str]:
        if self._service_names is None:
            rows = await self.search_rows("serviceType", {})
            self._service_names = {
                tid: clean(r.get("description")) for r in rows if (tid := to_int(r.get("typeID"))) is not None
            }
        return self._service_names

    def forget_service_names(self) -> None:
        self._service_names = None

    # -- writes ------------------------------------------------------------

    def check_write(self, customer_id: int) -> None:
        if not self.cfg.fr_writes:
            raise WriteBlocked("FieldRoutes writes are off (FR_WRITES=off)")
        allow = self.cfg.fr_write_customer_ids
        if allow is not None and customer_id not in allow:
            raise WriteBlocked(f"customer {customer_id} is not in FR_WRITE_CUSTOMER_IDS")

    async def create_note(self, customer_id: int, text: str, *, day: str, show_customer: bool) -> None:
        self.check_write(customer_id)
        if self.cfg.fr_note_type_id is None:
            raise WriteBlocked("FR_DEFAULT_NOTE_TYPE_ID is not set, so there is no Note Type to file the link under")
        await self.client.call(
            "note",
            "create",
            {
                "customerID": customer_id,
                "date": day,
                "contactType": self.cfg.fr_note_type_id,
                "notes": text,
                "showOnInvoice": 0,
                "showTech": 0,
                "showCustomer": 1 if show_customer else 0,
                "employeeID": self.cfg.fr_employee_id,
            },
        )

    async def create_review(
        self, customer_id: int, appointment_id: int, stars: int, feedback: str, when: datetime
    ) -> None:
        self.check_write(customer_id)
        await self.client.call(
            "review",
            "create",
            {
                "appointmentID": appointment_id,
                "customerID": customer_id,
                "starRating": stars,
                "feedback": feedback,
                "time": when.astimezone(self.cfg.tz).strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    async def create_task(self, customer_id: int, text: str, *, due: str) -> None:
        self.check_write(customer_id)
        if self.cfg.fr_sales_task_category_id is None:
            # Verified live: task/create needs one of the office's own Task Category IDs.
            raise WriteBlocked("no Task Category set (VIDEO_SALES_TASK_CATEGORY_ID or FR_DEFAULT_TASK_CATEGORY_ID)")
        await self.client.call(
            "task",
            "create",
            {
                "type": 0,
                "customerID": customer_id,
                "task": text,
                "dueDate": due,
                "status": 0,
                "addedBy": self.cfg.fr_employee_id,
                "category": self.cfg.fr_sales_task_category_id,
            },
        )
