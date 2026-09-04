"""Shared fake FieldRoutes and fixtures for every test file in this suite.

`FakeFR` is an in-memory FieldRoutes server behind `httpx.MockTransport` that
speaks the real wire dialect: form-encoded bodies, PHP-style `[]` arrays, JSON
search filter objects, and the real response envelope (`params` echo with
auth included, `tokenUsage`, `ignoredParams` before the data key, plural data
keys named by `propertyName`/`propertyNameData`) -- all confirmed against a
real call, not guessed from the swagger. See `tests/test_cassettes.py` for
the complementary layer that replays verbatim recorded responses instead of
this hand-built approximation.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx
import pytest

os.environ.setdefault("FR_SUBDOMAIN", "zest")
os.environ.setdefault("FR_AUTH_KEY", "key")
os.environ.setdefault("FR_AUTH_TOKEN", "token")
os.environ.setdefault("FR_OFFICE_ID", "7")
os.environ.setdefault("FR_DEFAULT_EMPLOYEE_ID", "9")
os.environ.setdefault("FR_DEFAULT_NOTE_TYPE_ID", "5")
os.environ.setdefault("FR_DEFAULT_TASK_CATEGORY_ID", "10002")

from fr_mcp import server  # noqa: E402
from fr_mcp.client import FieldRoutesClient, RateLimiter  # noqa: E402

TODAY = date.today().isoformat()


# ---------------------------------------------------------------------------
# Fake FieldRoutes
# ---------------------------------------------------------------------------


@dataclass
class Req:
    entity: str
    action: str
    form: dict[str, list[str]]
    body: str

    def one(self, key: str) -> str | None:
        values = self.form.get(key)
        return values[0] if values else None


def _seed() -> dict[str, dict[int, dict[str, Any]]]:
    return {
        "office": {7: {"officeID": 7, "officeName": "Zest Lawn & Pest", "timeZone": "America/Los_Angeles"}},
        "employee": {
            9: {"employeeID": 9, "officeID": 7, "fname": "Office", "lname": "Admin", "type": 0, "active": 1},
            11: {"employeeID": 11, "officeID": 7, "fname": "Carlos", "lname": "Quijas", "type": 1, "active": 1},
            12: {"employeeID": 12, "officeID": 7, "fname": "Iggy", "lname": "Artemenko", "type": 2, "active": 1},
        },
        "serviceType": {
            3: {"typeID": 3, "officeID": 7, "description": "Fall Aeration and Seeding", "frequency": 365},
            4: {"typeID": 4, "officeID": 7, "description": "Lawn Fertilization", "frequency": 45},
        },
        "region": {2: {"regionID": 2, "officeID": 7, "description": "Folsom", "active": 1}},
        "customer": {
            1: {
                "customerID": 1, "officeID": 7, "active": 1, "fname": "Jane", "lname": "Smith",
                "phone1": "9165551234", "email": "jane@example.com", "address": "123 Main St",
                "city": "Folsom", "state": "CA", "zip": "95630", "lat": 38.67, "lng": -121.17,
                "balance": 150.0, "balanceAge": 45, "aPay": 1, "preferredTechID": 11,
                # Comma-separated string, not a JSON array -- confirmed live on the real
                # API despite the swagger's declared type: array. A multi-digit id here
                # (100, not e.g. 1) catches the old character-by-character iteration bug.
                "subscriptionIDs": "100", "specialScheduling": "Gate on left side", "status": 1,
            },
            2: {
                "customerID": 2, "officeID": 7, "active": 1, "fname": "John", "lname": "Smithers",
                "phone1": "9165559999", "address": "456 Oak Ave", "city": "Folsom", "state": "CA",
                "zip": "95630", "balance": 0, "balanceAge": 0, "subscriptionIDs": [], "status": 1,
            },
            3: {
                "customerID": 3, "officeID": 7, "active": 1, "fname": "", "lname": "", "companyName": "Acme Corp",
                "address": "1 Industrial Way", "city": "Sacramento", "state": "CA", "zip": "95814",
                "balance": 900.0, "balanceAge": 95, "aPay": 0, "subscriptionIDs": [], "status": 1,
            },
        },
        "subscription": {
            100: {
                "subscriptionID": 100, "officeID": 7, "customerID": 1, "serviceID": 3, "active": 1,
                "frequency": 365, "billingFrequency": -1, "nextService": TODAY, "lastCompleted": "2025-10-09",
                "preferredTech": 11, "preferredDays": -1, "regionID": 2, "customDate": "2026-10-09",
                "soldBy": 12, "contractValue": 208.0, "dateAdded": "2025-03-23",
            },
            # Live shape (customer 14699 on Zest's account): a custom-schedule subscription has the
            # literal string "CUSTOM" in frequency, and unassigned IDs are the string "0".
            # nextService is outside every default window so due_for_service tests are unaffected.
            101: {
                "subscriptionID": "101", "officeID": "7", "customerID": "2", "serviceID": "4", "active": "1",
                "frequency": "CUSTOM", "billingFrequency": "28", "customDate": "2026-10-01",
                "nextService": "2026-10-01", "preferredTech": "0", "preferredDays": "-1", "regionID": "0",
                "soldBy": "12", "soldBy2": "0", "soldBy3": "0", "duration": "-1",
            },
        },
        "appointment": {
            500: {
                "appointmentID": 500, "officeID": 7, "customerID": 1, "subscriptionID": 100, "routeID": 20,
                "spotID": 30, "date": TODAY, "start": "08:00:00", "end": "10:00:00", "type": 3, "status": 0,
                "assignedTech": 11, "sequence": 1, "notes": "Aerate front and back", "additionalTechs": "12",
            },
            501: {
                "appointmentID": 501, "officeID": 7, "customerID": 2, "routeID": 20, "spotID": 31,
                "date": TODAY, "start": "10:00:00", "end": "12:00:00", "type": 4, "status": 0,
                "assignedTech": 11, "sequence": 2,
            },
            502: {
                "appointmentID": 502, "officeID": 7, "customerID": 1, "subscriptionID": 100, "routeID": 19,
                "date": "2025-10-09", "type": 3, "status": 1, "assignedTech": 11, "servicedBy": 11,
            },
        },
        "route": {
            20: {"routeID": 20, "officeID": 7, "title": "Carlos - Folsom", "date": TODAY, "assignedTech": 11,
                 "apiCanSchedule": 1, "distanceScore": 0.8},
            19: {"routeID": 19, "officeID": 7, "title": "Old route", "date": "2025-10-09", "assignedTech": 11},
        },
        "spot": {
            30: {"spotID": 30, "officeID": 7, "routeID": 20, "date": TODAY, "start": "08:00:00", "end": "10:00:00",
                 "currentAppointment": 500, "apiCanSchedule": 1, "assignedTech": 11},
            31: {"spotID": 31, "officeID": 7, "routeID": 20, "date": TODAY, "start": "10:00:00", "end": "12:00:00",
                 "currentAppointment": 501, "apiCanSchedule": 1, "assignedTech": 11},
            32: {"spotID": 32, "officeID": 7, "routeID": 20, "date": TODAY, "start": "12:00:00", "end": "14:00:00",
                 "currentAppointment": 0, "apiCanSchedule": 1, "assignedTech": 11, "distanceToPrevious": 1.2,
                 "distanceToNext": 0.4},
            33: {"spotID": 33, "officeID": 7, "routeID": 20, "date": TODAY, "start": "14:00:00", "end": "15:00:00",
                 "currentAppointment": 0, "apiCanSchedule": 1, "blockReason": "Lunch"},
            34: {"spotID": 34, "officeID": 7, "routeID": 20, "date": TODAY, "start": "15:00:00", "end": "16:00:00",
                 "currentAppointment": 0, "apiCanSchedule": 1, "reserved": 1},
            # Live shape: every value is a *string*, so an open, unreserved spot arrives as
            # "reserved": "0" -- which is truthy in Python. Not API-schedulable, so it stays
            # out of open_slots and only exercises route_stops' state labeling.
            35: {"spotID": 35, "officeID": 7, "routeID": 20, "date": TODAY, "start": "16:00:00", "end": "17:00:00",
                 "currentAppointment": "0", "apiCanSchedule": "0", "reserved": "0", "assignedTech": "11"},
        },
        "task": {
            700: {"taskIDs": 700, "officeID": 7, "customerID": 1, "type": 1, "status": 0, "task": "Dog in backyard"},
            701: {"taskIDs": 701, "officeID": 7, "customerID": 1, "type": 0, "status": 3, "task": "Call about renewal",
                  "assignedTo": 9, "category": "10002", "categoryDescription": "Billing"},
            702: {"taskIDs": 702, "officeID": 7, "customerID": 1, "type": 0, "status": 1, "task": "Done already"},
        },
        "note": {
            800: {"noteID": 800, "officeID": 7, "customerID": 1, "typeID": 5, "notes": "Gate code 1234",
                  "date": "2026-08-01", "employeeID": 9, "showTech": 1, "showCustomer": 0},
            801: {"noteID": 801, "officeID": 7, "customerID": 1, "typeID": 5, "notes": "Older note",
                  "date": "2026-07-01", "employeeID": 9},
        },
        "customerFlag": {1: {"customerID": 1, "flag": "purpleDragon", "flagValue": 1}},
    }


ID_FIELD_ALIASES = {
    "officeIDs": "officeID",
    "officeID": "officeID",
    "typeIDs": "typeID",
    # appointment/search's real filter name is serviceIDs, but the field it
    # actually filters on the appointment record is `type` (confirmed against
    # the real spec: "type integer -- serviceID to perform").
    "serviceIDs": "type",
}


class FakeFR:
    """In-memory FieldRoutes that records every request it receives."""

    def __init__(self, data: dict[str, dict[int, dict[str, Any]]] | None = None):
        self.data = data if data is not None else _seed()
        self.requests: list[Req] = []
        self.fail_with: str | None = None
        self.next_status: int | None = None
        # Fail the Nth write (0-indexed by writes already completed), then stop failing --
        # unlike fail_with (which fires on whatever request comes next, read or write), this
        # lets a test make an *earlier* write in the same batch succeed and a *later* one fail.
        self.fail_write_after: int | None = None
        self.fail_write_message: str = "conflict"
        # Params the fake "doesn't recognise": echoed back in the envelope's
        # `ignoredParams` while still reporting success, exactly as the real API does.
        self.ignore_params: set[str] = set()
        # FieldRoutes' own per-office counters, reported back on every response.
        self.reads_today = 0
        self.writes_today = 0

    # -- request plumbing ----------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.headers["content-type"].startswith("application/x-www-form-urlencoded")
        body = request.content.decode()
        form = urllib.parse.parse_qs(body, keep_blank_values=True)
        _, api, entity, action = request.url.path.split("/")
        assert api == "api"
        self.requests.append(Req(entity, action, form, body))

        if self.next_status is not None:
            status, self.next_status = self.next_status, None
            return httpx.Response(status, text="upstream error")
        if form.get("authenticationKey") != ["key"] or form.get("authenticationToken") != ["token"]:
            resp_body: dict[str, Any] = {"success": False, "errorMessage": "Invalid authentication"}
        elif self.fail_with:
            msg, self.fail_with = self.fail_with, None
            resp_body = {"success": False, "errorMessage": msg}
        elif action == "search":
            resp_body = self._search(entity, form)
        elif action == "get":
            id_field = server._id_param(entity)
            ids = [int(i) for i in form.get(f"{id_field}[]", [])]
            assert len(ids) <= 1000, "get must be chunked at 1000"
            if entity == "customerFlag":
                # customerFlag has no ID of its own; get takes customerIDs and
                # returns every flag row for those customers.
                records = [r for r in self.data.get(entity, {}).values() if r.get("customerID") in ids]
            else:
                records = [self.data[entity][i] for i in ids if i in self.data.get(entity, {})]
            # Verified live: records sit under the plural name, and the
            # response says so via propertyName.
            resp_body = {"success": True, "count": len(records), "propertyName": f"{entity}s", f"{entity}s": records}
        else:
            resp_body = self._write(entity, action, form)
        return httpx.Response(200, json=self._envelope(entity, action, form, resp_body))

    def _envelope(self, entity: str, action: str, form: dict[str, list[str]], body: dict[str, Any]) -> dict[str, Any]:
        """Wrap a body the way the real API does (verified live on office/get and
        office/search): it echoes the whole request -- auth key and token included --
        under `params`, reports its own quota counters, and puts the list-valued
        `ignoredParams` *before* the data key."""
        if action in ("search", "summary") or action.startswith("get"):
            self.reads_today += 1
        else:
            self.writes_today += 1
        return {
            "params": {"endpoint": entity, "action": action, **{k: v[0] for k, v in form.items()}},
            "tokenUsage": {
                "requestsReadToday": self.reads_today,
                "requestsWriteToday": self.writes_today,
                "requestsReadInLastMinute": 1,
                "requestsWriteInLastMinute": 0,
            },
            "tokenLimits": {
                "limitReadRequestsPerMinute": 60,
                "limitReadRequestsPerDay": 3000,
                "limitWriteRequestsPerMinute": 60,
                "limitWriteRequestsPerDay": 3000,
            },
            "requestAPIKeyType": "standard",
            "requestAction": action,
            "endpoint": entity,
            "ignoredParams": [k for k in form if k.rstrip("[]") in self.ignore_params],
            "processingTime": "1 milliseconds",
            **body,
        }

    # -- search semantics ----------------------------------------------

    def _search(self, entity: str, form: dict[str, list[str]]) -> dict[str, Any]:
        records = list(self.data.get(entity, {}).values())
        for key, values in form.items():
            if key in ("authenticationKey", "authenticationToken", "includeData"):
                continue
            records = [r for r in records if self._matches(r, key, values[0])]
        ids = [self._id_of(entity, r) for r in records]
        # Verified live: on a search, propertyName names the ID list; with
        # includeData the records sit under the plural name and
        # propertyNameData names that key.
        out: dict[str, Any] = {
            "success": True,
            "idName": f"{entity}IDs",
            "count": len(ids),
            f"{entity}IDs": ids,
            "propertyName": f"{entity}IDs",
        }
        if form.get("includeData") == ["1"]:
            out[f"{entity}IDsNoDataExported"] = ids[1000:]
            out[f"{entity}s"] = records[:1000]
            out["propertyNameData"] = f"{entity}s"
        return out

    @staticmethod
    def _id_of(entity: str, record: dict[str, Any]) -> int:
        for key in (f"{entity}ID", f"{entity}IDs", "typeID", "taskIDs", "regionID", "customerID"):
            if key in record:
                return int(record[key])
        raise KeyError(f"no id field on {entity} record")

    @staticmethod
    def _matches(record: dict[str, Any], key: str, raw: str) -> bool:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = raw
        op, value = "=", parsed
        if isinstance(parsed, dict) and "operator" in parsed:
            op, value = parsed["operator"], parsed.get("value")

        if key == "phone":
            # Verified live: `phone` is an exact 10-digit match across phone1/phone2 (and
            # additional-contact phones); CONTAINS -- on `phone` or `phone1` -- returns nothing.
            if op != "=":
                return False
            return any(str(value) == str(record.get(f, "")) for f in ("phone1", "phone2"))
        if key == "dateStart":
            return str(record.get("date", "")) >= str(value)
        if key == "dateEnd":
            return str(record.get("date", "")) <= str(value)

        field_name = ID_FIELD_ALIASES.get(key) or (key[:-1] if key.endswith("IDs") else key)
        actual = record.get(field_name)
        if actual is None:
            return False
        a, v = str(actual), value
        if op == "=":
            return a == str(v)
        if op == "!=":
            return a != str(v)
        if op == "IN":
            return a in {str(x) for x in v}
        if op == "BETWEEN":
            return str(v[0]) <= a <= str(v[1])
        if op in (">", "<", ">=", "<="):
            fa, fv = float(actual), float(v)
            return {">": fa > fv, "<": fa < fv, ">=": fa >= fv, "<=": fa <= fv}[op]
        if op == "CONTAINS" or op == "LIKE":
            return str(v).lower() in a.lower()
        if op == "STARTSWITH":
            return a.lower().startswith(str(v).lower())
        if op == "ENDSWITH":
            return a.lower().endswith(str(v).lower())
        raise AssertionError(f"unsupported operator {op}")

    # -- writes ------------------------------------------------------------

    def _write(self, entity: str, action: str, form: dict[str, list[str]]) -> dict[str, Any]:
        if self.fail_write_after is not None and self.writes_today == self.fail_write_after:
            self.fail_write_after = None
            return {"success": False, "errorMessage": self.fail_write_message}
        if entity == "spot" and action == "reserve":
            return {"success": True, "reservation": "tok-123"}
        if action == "create":
            return {"success": True, f"{entity}ID": 999}
        return {"success": True}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _client_for(fake: FakeFR, **kwargs: Any) -> FieldRoutesClient:
    return FieldRoutesClient(
        subdomain="zest",
        auth_key="key",
        auth_token="token",
        transport=httpx.MockTransport(fake.handler),
        rate_limiter=RateLimiter(limit=100000),
        **kwargs,
    )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFR:
    fr = FakeFR()
    monkeypatch.setattr(server, "_client", _client_for(fr))
    server._reset_cache()
    monkeypatch.setenv("FR_WRITES", "on")
    monkeypatch.delenv("FR_ALLOW_DELETE", raising=False)
    monkeypatch.delenv("FR_ALLOW_CHARGES", raising=False)
    return fr


def _last(fake: FakeFR, entity: str, action: str) -> Req:
    for req in reversed(fake.requests):
        if req.entity == entity and req.action == action:
            return req
    raise AssertionError(f"no {entity}/{action} request")


def loads(result: str) -> Any:
    return json.loads(result)
