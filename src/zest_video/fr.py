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
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from fr_mcp.client import FieldRoutesClient, FieldRoutesError

from .config import Config

__all__ = ["FieldRoutes", "FieldRoutesError", "LineWrite", "WriteBlocked", "WriteRefused"]

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


def own_words(value: Any) -> str:
    """Something a person wrote, kept as they wrote it: their line breaks and spacing stay.
    Only NUL characters go, and the blank space before and after the whole text. (`clean`
    would run a technician's three lines together into one.)"""
    return str(value).replace("\x00", "").strip() if value is not None else ""


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


class WriteRefused(Exception):
    """A FieldRoutes write that must not be made, by FieldRoutes' own record as it was read
    a moment before: the appointment is not completed, or is another customer's. Final:
    trying again would not change the answer."""


def completed(row: dict[str, Any]) -> bool:
    """Does FieldRoutes' own record say this appointment is completed? Only the status "1"
    says so. Every value arrives as a string, so it goes through the int helper; and
    because a "yes" here lets a call go out that completes and invoices a visit when it
    is wrong, a number that merely rounds to 1 (1.9, `true`) is not a yes either."""
    status = row.get("status")
    return to_int(status) == 1 and str(status).strip() == "1"


# What must not move on a visit when a line is added to its comments. A change in one of
# these between the read before the write and the read after it is told to the office at
# once: the write is the call that completes and invoices a visit when it goes wrong.
WATCHED = (
    "status", "customerID", "dateCompleted", "completedBy", "servicedBy", "timeIn", "timeOut", "checkIn", "checkOut",
    "ticketID", "amountCollected", "paymentMethod",
)


@dataclass(frozen=True)
class LineWrite:
    """What became of one try to add a video's line to a technician's comments."""

    wrote: bool  # this try sent the comments. False: the link was already there
    there: bool | None  # read again after the write: is the link in the comments? None: that read failed
    words_kept: bool = True  # the technician's own words are still above the line
    words_before: str = ""  # only when they are not: his words as read before the write,
    words_after: str = ""  # and the whole comments as read after it
    changed: tuple[str, ...] = ()  # the WATCHED fields that read differently after the write
    also_changed: tuple[str, ...] = ()  # any other field that does
    said: tuple[str, ...] = ()  # FieldRoutes' own messages in its answer (soft errors, warnings)


def _said(answer: Any) -> tuple[str, ...]:
    """The messages in FieldRoutes' answer to appointment/complete. Its help page says the
    call "returns an array of system messages (usually soft errors or warnings)" and shows
    a bare appointment ID in the same place, so this takes whatever words it finds."""
    out: list[str] = []

    def walk(value: Any, depth: int = 0) -> None:
        if depth > 3 or len(out) >= 10:
            return
        if isinstance(value, str):
            text = clean(value)
            if text and not text.isdigit():
                out.append(text[:200])
        elif isinstance(value, dict):
            for item in value.values():
                walk(item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for item in value:
                walk(item, depth + 1)

    if isinstance(answer, dict):
        for key in ("result", "messages", "warnings", "errors"):
            walk(answer.get(key))
    return tuple(out)


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

    # -- the one write that could cost money ------------------------------------

    async def _visit(self, appointment_id: int) -> dict[str, Any] | None:
        rows = await self.get_rows("appointment", [appointment_id])
        return next((r for r in rows if to_int(r.get("appointmentID")) == appointment_id), None)

    async def line_on(self, customer_id: int, appointment_ids: list[int], link: str) -> int | None:
        """Is this link in the technician's comments on one of these appointments? One
        read, and nothing is written. The appointment's ID when it is, None when this
        customer's appointments were read and none of them has it.

        Raises FieldRoutesError when that cannot be told: FieldRoutes did not answer, or
        answered without the technician's comments on one of them. "Cannot tell" must
        never be taken for "not there": the office is told the first and sends a second
        email on the second."""
        rows = await self.get_rows("appointment", appointment_ids)
        blind = False
        for row in sorted(rows, key=lambda r: to_int(r.get("appointmentID")) or 0):
            if to_int(row.get("customerID")) != customer_id:
                continue
            words = row.get("notes")
            if not isinstance(words, str):
                blind = True
            elif link in words:
                return to_int(row.get("appointmentID"))
        if blind:
            raise FieldRoutesError("FieldRoutes did not send the technician's comments with the visit")
        return None

    async def add_to_tech_comments(
        self, customer_id: int, appointment_id: int, line: str, link: str,
        *, go: Callable[[], Awaitable[None]] | None = None,
    ) -> LineWrite:
        """Add one line to the technician's comments on a COMPLETED appointment. FieldRoutes'
        own after-service email prints those comments where its template says
        {{techComments}}, so this is how a video link gets into that email.

        NOT PROVEN LIVE. It is built to FieldRoutes' own help page for appointment/complete
        ("Idempotent Re-completion": on an appointment that is already completed the call
        updates its completion data and keeps every field that was not sent). The only
        parameter that writes the technician's comments (`notes` on the record) is
        `completionNotes` on that call.

        The same call on an appointment that is NOT completed would complete it: its
        `status` defaults to Completed and it runs FieldRoutes' invoicing logic, which
        makes a real invoice. So, every time:

        - The appointment is read fresh first, and the write is refused for good
          (`WriteRefused`) unless it belongs to this customer, its status is completed
          (the string "1", nothing else: pending, no-show, cancelled, empty, missing and
          any number that only rounds to 1 all refuse), and the read carries the
          technician's comments. A read without them is not "he wrote nothing".
        - The write follows that read at once and is sent ONCE. The shared client would
          otherwise wait for a slot in its per-minute limit (up to a minute) and send the
          same request again after a timeout, with nobody looking in between; a visit set
          back to pending in that time would be completed and invoiced by the second
          send. So: there must be room in the limit before the read, nothing waits
          between the read and the send, and a timeout or an error is raised, not
          retried. The caller comes back later, and that try reads first again. What is
          left is the moment between FieldRoutes answering the read and taking the
          write, a fraction of a second, which nothing outside FieldRoutes can close.
        - The call carries exactly two parameters, `appointmentID` and `completionNotes`.
          Never `status`, never anything else.
        - The technician's own words are kept, and the line goes under them.
        - When the link is already in the comments nothing is written. That makes the
          call safe to repeat: a second try after a server was stopped mid-write, or
          after an answer that got lost, adds nothing.
        - After the write the appointment is read once more and compared with the read
          before it (`LineWrite`): is the link there, are his words still above it, and
          did anything else on the visit change. Nothing is taken on FieldRoutes' word
          "success" alone.

        `go` is awaited after the read and before the write: the caller's last word on
        whether the line is still wanted. It stops the write by raising."""
        self.check_write(customer_id)
        self.client.usage.check(False)  # no room left in the day's writes: said before anything is read for it
        limit = self.client.rate_limiter
        if limit.free_slots() < 3:  # the read, the write, the read after it
            raise FieldRoutesError("FieldRoutes' calls-per-minute limit is nearly used up; the line waits a moment")
        before = await self._visit(appointment_id)
        if before is None:
            raise WriteRefused("FieldRoutes no longer has that appointment")
        if to_int(before.get("customerID")) != customer_id:
            raise WriteRefused("in FieldRoutes that appointment belongs to another customer")
        if not completed(before):
            raise WriteRefused("FieldRoutes no longer shows that appointment as completed")
        if not isinstance(before.get("notes"), str):
            # FieldRoutes' published spec once called this very field write-only. If a read
            # ever comes back without it, "nothing there" would overwrite what he typed.
            raise WriteRefused(
                "FieldRoutes did not send the technician's comments with the visit, and adding the link without "
                "seeing them could wipe out his own words"
            )
        words = own_words(before["notes"])
        if link in words:
            return LineWrite(wrote=False, there=True)
        if go is not None:
            await go()
        # Nothing may wait from here to the send (see above).
        if limit.free_slots() < 1:
            raise FieldRoutesError("FieldRoutes' calls-per-minute limit filled up; the line waits a moment")
        try:
            answer = await self.client.call(
                "appointment",
                "complete",
                {"appointmentID": appointment_id, "completionNotes": f"{words}\n\n{line}" if words else line},
                attempts=1,
            )
        except FieldRoutesError as exc:
            ignored = (exc.response or {}).get("ignoredParams") if isinstance(exc.response, dict) else None
            if exc.response is not None and exc.response.get("success") and ignored:
                # FieldRoutes answered "done" and said it left out what was sent. Asking
                # again would get the same answer, so this is final, not a failure to retry.
                raise WriteRefused("FieldRoutes did not take the comments from this server") from None
            raise
        said = _said(answer)
        try:
            after = await self._visit(appointment_id)
        except FieldRoutesError:
            return LineWrite(wrote=True, there=None, said=said)
        if after is None:
            return LineWrite(wrote=True, there=False, changed=("appointmentID",), said=said)
        now_reads = own_words(after.get("notes")) if isinstance(after.get("notes"), str) else ""
        differ = sorted(k for k in set(before) | set(after) if k != "notes" and str(before.get(k)) != str(after.get(k)))
        kept = not words or clean(now_reads).startswith(clean(words))
        return LineWrite(
            wrote=True,
            there=link in now_reads,
            words_kept=kept,
            words_before="" if kept else words,
            words_after="" if kept else now_reads,
            changed=tuple(k for k in differ if k in WATCHED),
            also_changed=tuple(k for k in differ if k not in WATCHED),
            said=said,
        )
