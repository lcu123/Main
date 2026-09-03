"""Wire-format and tool tests against a fake FieldRoutes served by httpx.MockTransport.

No network and no credentials: `FakeFR` implements just enough of
`POST /api/{entity}/{action}` (form body, auth in body, search filter objects,
PHP-style ID arrays, 1000-record get chunks, HTTP 200 on failure) to prove
the client speaks FieldRoutes' dialect and the tools shape output correctly.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from dataclasses import dataclass, field
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

from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from fr_mcp import generated, server  # noqa: E402
from fr_mcp.client import (  # noqa: E402
    FieldRoutesClient,
    FieldRoutesError,
    RateLimiter,
    UsageCounter,
    encode_form,
    is_read_action,
)

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
        },
        "task": {
            700: {"taskIDs": 700, "officeID": 7, "customerID": 1, "type": 1, "status": 0, "task": "Dog in backyard"},
            701: {"taskIDs": 701, "officeID": 7, "customerID": 1, "type": 0, "status": 3, "task": "Call about renewal",
                  "assignedTo": 9},
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


ID_FIELD_ALIASES = {"officeIDs": "officeID", "officeID": "officeID", "typeIDs": "typeID"}


class FakeFR:
    """In-memory FieldRoutes that records every request it receives."""

    def __init__(self, data: dict[str, dict[int, dict[str, Any]]] | None = None):
        self.data = data if data is not None else _seed()
        self.requests: list[Req] = []
        self.fail_with: str | None = None
        self.next_status: int | None = None
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
            body: dict[str, Any] = {"success": False, "errorMessage": "Invalid authentication"}
        elif self.fail_with:
            msg, self.fail_with = self.fail_with, None
            body = {"success": False, "errorMessage": msg}
        elif action == "search":
            body = self._search(entity, form)
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
            body = {"success": True, "count": len(records), "propertyName": f"{entity}s", f"{entity}s": records}
        else:
            body = self._write(entity, action, form)
        return httpx.Response(200, json=self._envelope(entity, action, form, body))

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
            "ignoredParams": [],
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
            return any(op == "CONTAINS" and str(value) in str(record.get(f, "")) for f in ("phone1", "phone2"))
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


# ---------------------------------------------------------------------------
# Client wire format
# ---------------------------------------------------------------------------


def test_encode_form_conventions() -> None:
    pairs = encode_form(
        {
            "skip": None,
            "flag": True,
            "customerIDs": [1, 2],
            "balance": {"operator": ">", "value": "0"},
            "name": "Jane",
        }
    )
    assert pairs == [
        ("flag", "1"),
        ("customerIDs[]", "1"),
        ("customerIDs[]", "2"),
        ("balance", '{"operator": ">", "value": "0"}'),
        ("name", "Jane"),
    ]


async def test_call_posts_form_encoded_with_auth_in_body() -> None:
    fake = FakeFR()
    async with _client_for(fake) as client:
        data = await client.call("customer", "search", {"lname": "Smith"})
    assert data["success"] is True
    req = fake.requests[0]
    assert req.entity == "customer" and req.action == "search"
    assert req.one("authenticationKey") == "key"
    assert req.one("authenticationToken") == "token"
    assert req.one("lname") == "Smith"
    assert not req.body.startswith("{"), "body must be form-encoded, not JSON"


async def test_base_url_override_and_subdomain_default() -> None:
    fake = FakeFR()
    c1 = FieldRoutesClient(subdomain="zest", auth_key="k", auth_token="t", transport=httpx.MockTransport(fake.handler))
    assert c1.base_url == "https://zest.fieldroutes.com/api"
    c2 = FieldRoutesClient(
        base_url="https://zest.pestroutes.com/api/", auth_key="k", auth_token="t",
        transport=httpx.MockTransport(fake.handler),
    )
    assert c2.base_url == "https://zest.pestroutes.com/api"
    await c1.aclose()
    await c2.aclose()


async def test_get_encodes_ids_as_php_arrays() -> None:
    fake = FakeFR()
    async with _client_for(fake) as client:
        rows = await client.get("customer", [1, 2])
    assert [r["customerID"] for r in rows] == [1, 2]
    body = fake.requests[0].body
    assert "customerIDs%5B%5D=1" in body and "customerIDs%5B%5D=2" in body
    assert urllib.parse.unquote(body).count("customerIDs[]=") == 2


async def test_search_filters_are_json_and_lists_become_in() -> None:
    fake = FakeFR()
    async with _client_for(fake) as client:
        data = await client.search(
            "customer", {"balance": {"operator": ">", "value": "100"}, "customerIDs": [1, 3], "active": 1}
        )
    assert data["customerIDs"] == [1, 3]
    req = fake.requests[0]
    assert json.loads(req.one("balance")) == {"operator": ">", "value": "100"}
    assert json.loads(req.one("customerIDs")) == {"operator": "IN", "value": [1, 3]}
    assert req.one("active") == "1"
    assert "customerIDs[]" not in urllib.parse.unquote(req.body)


async def test_search_and_get_paginates_in_chunks_of_1000() -> None:
    data = {"widget": {i: {"widgetID": i, "n": i} for i in range(1, 2501)}}
    fake = FakeFR(data)
    async with _client_for(fake) as client:
        rows = await client.search_and_get("widget", {})
    assert len(rows) == 2500
    gets = [r for r in fake.requests if r.action == "get"]
    assert [len(r.form["widgetIDs[]"]) for r in gets] == [1000, 1000, 500]


async def test_call_strips_auth_echo_from_every_response() -> None:
    fake = FakeFR()
    async with _client_for(fake) as client:
        data = await client.call("office", "search")
        written = await client.call("note", "create", {"customerID": 1})
    for body in (data, written):
        assert "params" not in body
        dumped = json.dumps(body)
        assert "authenticationKey" not in dumped and "authenticationToken" not in dumped
    # the rest of the envelope is harmless and stays
    assert data["tokenUsage"]["requestsReadToday"] == 1


async def test_get_finds_records_under_the_plural_data_key() -> None:
    fake = FakeFR()
    async with _client_for(fake) as client:
        rows = await client.get("office", [7])
    assert rows[0]["officeName"] == "Zest Lawn & Pest"


def test_extract_records_trusts_property_name_and_never_a_metadata_list() -> None:
    get = {"success": True, "ignoredParams": [], "count": 1, "propertyName": "offices", "offices": [{"officeID": 1}]}
    assert FieldRoutesClient.extract_records("office", get) == [{"officeID": 1}]
    search = {
        "success": True, "ignoredParams": [], "officeIDs": [1], "propertyName": "officeIDs",
        "officeIDsNoDataExported": [], "offices": [{"officeID": 1}], "propertyNameData": "offices",
    }
    assert FieldRoutesClient.extract_records("office", search) == [{"officeID": 1}]
    # no name hints at all: plural fallback, and a metadata list is never mistaken for data
    assert FieldRoutesClient.extract_records("office", {"ignoredParams": [], "offices": [{"a": 1}]}) == [{"a": 1}]
    assert FieldRoutesClient.extract_records("office", {"ignoredParams": [], "officeIDs": [1]}) == []


async def test_usage_counter_adopts_fieldroutes_authoritative_counts() -> None:
    fake = FakeFR()
    fake.reads_today = 40  # other integrations already spent some of today's shared quota
    async with _client_for(fake) as client:
        await client.call("office", "search")
        assert client.usage.snapshot()["reads"] == 41
        await client.call("note", "create", {"customerID": 1})
        snap = client.usage.snapshot()
    assert snap["reads"] == 41 and snap["writes"] == 1


async def test_search_include_data_returns_first_1000() -> None:
    data = {"widget": {i: {"widgetID": i} for i in range(1, 1201)}}
    fake = FakeFR(data)
    async with _client_for(fake) as client:
        resp = await client.search("widget", {}, include_data=True)
    assert fake.requests[0].one("includeData") == "1"
    assert len(client.extract_records("widget", resp)) == 1000
    assert len(resp["widgetIDsNoDataExported"]) == 200


async def test_failure_is_http_200_with_success_false() -> None:
    fake = FakeFR()
    fake.fail_with = "Permission denied: editRedNotes"
    async with _client_for(fake) as client:
        with pytest.raises(FieldRoutesError) as exc:
            await client.call("customer", "update", {"customerID": 1, "notes": "x"})
    assert "editRedNotes" in str(exc.value)
    assert exc.value.entity == "customer" and exc.value.action == "update"


async def test_bad_credentials_surface_as_error() -> None:
    fake = FakeFR()
    client = FieldRoutesClient(subdomain="zest", auth_key="wrong", auth_token="t", transport=httpx.MockTransport(fake.handler))
    with pytest.raises(FieldRoutesError, match="Invalid authentication"):
        await client.call("office", "search")
    await client.aclose()


async def test_retries_on_5xx_then_succeeds() -> None:
    fake = FakeFR()
    fake.next_status = 503
    async with _client_for(fake) as client:
        data = await client.call("office", "search")
    assert data["success"] is True
    assert len(fake.requests) == 2


async def test_rate_limiter_is_a_sliding_window() -> None:
    limiter = RateLimiter(limit=3, window=0.3)
    start = time.monotonic()
    for _ in range(3):
        await limiter.acquire()
    assert time.monotonic() - start < 0.1
    await limiter.acquire()
    assert time.monotonic() - start >= 0.25


def test_id_list_parses_comma_strings_not_character_by_character() -> None:
    # This is the exact live bug: customer.subscriptionIDs/appointmentIDs come back
    # as comma-separated strings, not JSON arrays. Iterating the raw string used to
    # silently turn "12,34" into ids 1, 2, 3, 4 -- always route through _id_list().
    assert server._id_list("100") == [100]
    assert server._id_list("10002,10006,16404") == [10002, 10006, 16404]
    assert server._id_list([100, 101]) == [100, 101]
    assert server._id_list(None) == []
    assert server._id_list("") == []
    assert server._id_list("12, 34") == [12, 34]  # tolerate a space after the comma


def test_is_read_action() -> None:
    assert is_read_action("search") and is_read_action("get") and is_read_action("getAddOns")
    assert is_read_action("summary")
    assert not is_read_action("create") and not is_read_action("update") and not is_read_action("delete")


def test_usage_counter_refuses_at_95_percent_and_resets_per_kind() -> None:
    usage = UsageCounter(read_limit=100, write_limit=10)
    for _ in range(94):
        usage.check(True)
        usage.record(True)
    usage.check(True)  # 94 used, threshold is 95: still fine
    for _ in range(9):
        usage.check(False)
        usage.record(False)
    with pytest.raises(FieldRoutesError, match="Daily write quota"):
        usage.check(False)  # 9 writes used, threshold is 9 (int(10*0.95)=9)
    usage.record(True)  # 95th read
    with pytest.raises(FieldRoutesError, match="Daily read quota"):
        usage.check(True)
    # a snapshot always reports usage regardless of whether the threshold is hit
    snap = usage.snapshot()
    assert snap == {"date": snap["date"], "reads": 95, "readLimit": 100, "writes": 9, "writeLimit": 10}


async def test_client_call_refuses_near_quota() -> None:
    # read_limit=2 -> threshold is int(2*0.95)=1, so the second call is refused
    # before it reaches the server, and the refusal itself isn't counted.
    fake = FakeFR()
    async with _client_for(fake, usage=UsageCounter(read_limit=2, write_limit=100)) as client:
        await client.call("office", "search")
        with pytest.raises(FieldRoutesError, match="Daily read quota"):
            await client.call("office", "search")
    assert len(fake.requests) == 1


# ---------------------------------------------------------------------------
# Office scoping + generic tools
# ---------------------------------------------------------------------------


async def test_office_filter_is_auto_applied_to_searches(fake: FakeFR) -> None:
    await server.search("customer", {"lname": "Smith"})
    assert _last(fake, "customer", "search").one("officeIDs") == "7"
    await server.search("customer", {"lname": "Smith", "officeIDs": 3})
    assert _last(fake, "customer", "search").one("officeIDs") == "3"
    await server.get("customer", [1])
    assert "officeIDs" not in _last(fake, "customer", "get").form


async def test_list_and_describe_endpoints() -> None:
    all_eps = loads(await server.list_endpoints())
    assert all_eps["count"] == 187
    assert "cancel" in all_eps["entities"]["appointment"]
    appt = loads(await server.list_endpoints("appointment"))
    assert set(appt["actions"]) == {"search", "get", "create", "update", "cancel", "complete"}
    desc = loads(await server.describe_endpoint("subscription/update"))
    names = {p["name"] for p in desc["params"]}
    assert {"frequency", "billingFrequency", "seasonalStart", "customDate", "regionID"} <= names
    assert next(p for p in desc["params"] if p["name"] == "subscriptionID")["required"] is True
    with pytest.raises(ToolError):
        await server.describe_endpoint("nope/nothing")


async def test_generic_search_caps_and_includes_data(fake: FakeFR) -> None:
    out = loads(await server.search("customer", {"active": 1}, include_data=True, limit=2, fields=["customerID", "lname"]))
    assert out["total"] == 3 and out["ids"] == [1, 2] and out["truncated"] is True
    assert out["data"] == [{"customerID": 1, "lname": "Smith"}, {"customerID": 2, "lname": "Smithers"}]


async def test_generic_call_guards(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ToolError, match="FR_ALLOW_DELETE"):
        await server.call("note", "delete", {"noteID": 800})
    with pytest.raises(ToolError, match="FR_ALLOW_CHARGES"):
        await server.call("payment", "create", {"customerID": 1, "amount": 10, "doCharge": 1, "paymentMethod": 3})
    await server.call("payment", "create", {"customerID": 1, "amount": 10, "doCharge": 0, "paymentMethod": 2})
    assert _last(fake, "payment", "create").one("doCharge") == "0"

    monkeypatch.setenv("FR_ALLOW_DELETE", "1")
    out = loads(await server.call("note", "delete", {"noteID": 800}))
    assert out["success"] is True

    monkeypatch.setenv("FR_WRITES", "off")
    with pytest.raises(ToolError, match="writes are disabled"):
        await server.call("task", "create", {"customerID": 1, "type": 0})
    assert loads(await server.call("customer", "search", {"lname": "Smith"}))["customerIDs"] == [1]


async def test_health_check(fake: FakeFR) -> None:
    out = loads(await server.health_check())
    assert out["ok"] is True
    assert out["offices"][0]["officeName"] == "Zest Lawn & Pest"
    assert out["config"]["officeID"] == 7 and out["config"]["writes"] is True
    assert out["dailyUsage"]["reads"] == 2 and out["dailyUsage"]["readLimit"] == 3000
    assert out["dailyUsage"]["writes"] == 0 and out["dailyUsage"]["date"] == TODAY


# ---------------------------------------------------------------------------
# Curated reads
# ---------------------------------------------------------------------------


async def test_find_customer_by_full_name(fake: FakeFR) -> None:
    out = loads(await server.find_customer("Jane Smith"))
    assert out["count"] == 1
    c = out["customers"][0]
    assert c["name"] == "Jane Smith" and c["propertyAddress"] == "123 Main St, Folsom, CA, 95630"
    req = _last(fake, "customer", "search")
    assert json.loads(req.one("fname")) == {"operator": "STARTSWITH", "value": "Jane"}
    assert json.loads(req.one("lname")) == {"operator": "STARTSWITH", "value": "Smith"}
    assert req.one("active") == "1"


async def test_find_customer_single_token_unions_last_first_company(fake: FakeFR) -> None:
    out = loads(await server.find_customer("smith"))
    assert [c["customerID"] for c in out["customers"]] == [1, 2]
    acme = loads(await server.find_customer("acme"))
    assert acme["customers"][0]["name"] == "Acme Corp"


async def test_find_customer_by_phone_and_id(fake: FakeFR) -> None:
    out = loads(await server.find_customer(phone="(916) 555-1234"))
    assert out["customers"][0]["customerID"] == 1
    assert json.loads(_last(fake, "customer", "search").one("phone")) == {"operator": "CONTAINS", "value": "9165551234"}
    out = loads(await server.find_customer(customer_id=3))
    assert out["customers"][0]["name"] == "Acme Corp"
    with pytest.raises(ToolError):
        await server.find_customer()


async def test_day_schedule_resolves_names_addresses_and_techs(fake: FakeFR) -> None:
    out = loads(await server.day_schedule())
    assert out["date"] == TODAY and out["count"] == 2
    first = out["appointments"][0]
    assert first["appointmentID"] == 500
    assert first["customer"]["name"] == "Jane Smith"
    assert first["customer"]["address"] == "123 Main St, Folsom, CA, 95630"
    assert first["service"] == "Fall Aeration and Seeding"
    assert first["route"] == "Carlos - Folsom"
    assert first["tech"] == "Carlos Quijas"
    assert first["additionalTechs"] == ["Iggy Artemenko"]
    assert first["statusText"] == "pending"
    assert _last(fake, "appointment", "search").one("status") == "0"

    only_tech = loads(await server.day_schedule(tech=11, status="all"))
    assert only_tech["count"] == 2
    assert "status" not in _last(fake, "appointment", "search").form


async def test_customer_360(fake: FakeFR) -> None:
    out = loads(await server.customer_360(1))
    assert out["customer"]["name"] == "Jane Smith"
    assert out["customer"]["preferredTechName"] == "Carlos Quijas"
    assert _last(fake, "customer", "get").one("includeSubscriptions") == "1"
    assert out["subscriptions"][0]["service"] == "Fall Aeration and Seeding"
    assert out["subscriptions"][0]["frequencyText"] == "every 365 days"
    assert out["subscriptions"][0]["region"] == "Folsom"
    assert out["subscriptions"][0]["soldByName"] == "Iggy Artemenko"
    assert [a["appointmentID"] for a in out["upcomingAppointments"]] == [500]
    assert [a["appointmentID"] for a in out["recentAppointments"]] == [502]
    assert {t["kind"] for t in out["openTasks"]} == {"alert", "task"}
    assert out["recentNotes"][0]["notes"] == "Gate code 1234"
    with pytest.raises(ToolError):
        await server.customer_360(404)


async def test_customer_alerts(fake: FakeFR) -> None:
    out = loads(await server.customer_alerts(1))
    assert out["specialScheduling"] == "Gate on left side"
    assert [a["task"] for a in out["alerts"]] == ["Dog in backyard"]
    assert [t["task"] for t in out["urgentTasks"]] == ["Call about renewal"]
    assert out["flags"][0]["flag"] == "purpleDragon"
    assert out["balance"] == 150.0 and out["preferredTech"] == "Carlos Quijas"


async def test_property_assignments_by_address(fake: FakeFR) -> None:
    out = loads(await server.property_assignments(address="123 Main"))
    assert out["customer"]["customerID"] == 1
    assert out["subscriptions"][0]["preferredTechName"] == "Carlos Quijas"
    assert out["upcomingAppointments"][0]["route"] == "Carlos - Folsom"
    with pytest.raises(ToolError):
        await server.property_assignments()


async def test_route_stops_marks_open_blocked_reserved(fake: FakeFR) -> None:
    out = loads(await server.route_stops(20))
    assert out["route"]["tech"] == "Carlos Quijas"
    states = {s["spotID"]: s.get("state") for s in out["stops"]}
    assert states[32] == "open" and states[33] == "blocked" and states[34] == "reserved"
    booked = next(s for s in out["stops"] if s["spotID"] == 30)
    assert booked["appointment"]["customer"]["name"] == "Jane Smith"


async def test_crew_schedule_counts_pending_stops(fake: FakeFR) -> None:
    out = loads(await server.crew_schedule())
    req = _last(fake, "route", "search")
    assert json.loads(req.one("date"))["operator"] == "BETWEEN"
    routes = out["days"][TODAY]
    assert routes[0]["title"] == "Carlos - Folsom" and routes[0]["pendingStops"] == 2


async def test_due_for_service_window_and_shape(fake: FakeFR) -> None:
    out = loads(await server.due_for_service(days=3, service_type_id=3))
    req = _last(fake, "subscription", "search")
    assert req.one("active") == "1" and req.one("serviceID") == "3"
    window = json.loads(req.one("nextService"))
    assert window["operator"] == "BETWEEN" and window["value"][0] == TODAY
    sub = out["subscriptions"][0]
    assert sub["customer"]["address"] == "123 Main St, Folsom, CA, 95630"
    assert sub["customer"]["lat"] == 38.67
    assert sub["preferredTechName"] == "Carlos Quijas"


async def test_open_slots_skips_taken_blocked_reserved(fake: FakeFR) -> None:
    out = loads(await server.open_slots())
    assert [s["spotID"] for s in out["spots"]] == [32]
    assert out["spots"][0]["route"] == "Carlos - Folsom" and out["spots"][0]["distanceToPrevious"] == 1.2
    assert _last(fake, "spot", "search").one("apiCanSchedule") == "1"


async def test_ar_aging_buckets(fake: FakeFR) -> None:
    out = loads(await server.ar_aging())
    req = _last(fake, "customer", "search")
    assert json.loads(req.one("balance")) == {"operator": ">", "value": "0.01"}
    assert out["customerCount"] == 2 and out["totalOutstanding"] == 1050.0
    assert out["buckets"] == {"0-30": 0.0, "31-60": 150.0, "61-90": 0.0, "90+": 900.0}
    assert out["customers"][0]["name"] == "Acme Corp" and out["customers"][0]["bucket"] == "90+"


async def test_lookups(fake: FakeFR) -> None:
    out = loads(await server.lookups("employees"))
    assert {e["name"]: e["typeText"] for e in out["employees"]} == {
        "Office Admin": "office", "Carlos Quijas": "tech", "Iggy Artemenko": "sales"
    }
    everything = loads(await server.lookups())
    assert everything["service_types"][0]["description"] == "Fall Aeration and Seeding"
    assert everything["regions"][0]["description"] == "Folsom"
    with pytest.raises(ToolError):
        await server.lookups("nope")


async def test_subscription_details_and_list_notes(fake: FakeFR) -> None:
    out = loads(await server.subscription_details(100))
    sub = out["subscription"]
    assert sub["customDate"] == "2026-10-09" and sub["billingFrequencyText"] == "one-time"
    assert sub["customer"]["name"] == "Jane Smith"
    assert out["upcomingAppointments"][0]["appointmentID"] == 500

    notes = loads(await server.list_notes(1))
    assert [n["noteID"] for n in notes["notes"]] == [800, 801]
    assert notes["notes"][0]["employeeName"] == "Office Admin"


# ---------------------------------------------------------------------------
# Curated writes
# ---------------------------------------------------------------------------


async def test_add_note_uses_defaults(fake: FakeFR) -> None:
    out = loads(await server.add_note(1, "Left gate open.", show_tech=True))
    assert out["success"] is True
    req = _last(fake, "note", "create")
    assert req.one("customerID") == "1" and req.one("notes") == "Left gate open."
    assert req.one("contactType") == "5" and req.one("employeeID") == "9"
    assert req.one("showOnInvoice") == "0" and req.one("showTech") == "1" and req.one("showCustomer") == "0"
    assert req.one("date") == TODAY


async def test_add_note_requires_note_type(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FR_DEFAULT_NOTE_TYPE_ID")
    with pytest.raises(ToolError, match="FR_DEFAULT_NOTE_TYPE_ID"):
        await server.add_note(1, "x")
    await server.add_note(1, "x", note_type_id=8)
    assert _last(fake, "note", "create").one("contactType") == "8"


async def test_update_note_merges_existing_values(fake: FakeFR) -> None:
    await server.update_note(800, text="Gate code 4321")
    req = _last(fake, "note", "update")
    assert req.one("contactID") == "800" and req.one("customerID") == "1"
    assert req.one("notes") == "Gate code 4321" and req.one("contactType") == "5"
    assert req.one("showTech") == "1" and req.one("date") == "2026-08-01"


async def test_set_red_notes(fake: FakeFR) -> None:
    await server.set_red_notes(1, "Beware of dog. Gate code 1234.")
    req = _last(fake, "customer", "update")
    assert req.one("customerID") == "1" and req.one("notes") == "Beware of dog. Gate code 1234."


async def test_create_task_and_alert(fake: FakeFR) -> None:
    await server.create_task(1, "Call back re: renewal", due="2026-09-10", urgent=True, category=10)
    req = _last(fake, "task", "create")
    assert req.one("type") == "0" and req.one("status") == "3" and req.one("dueDate") == "2026-09-10"
    assert req.one("addedBy") == "9" and req.one("category") == "10"
    await server.create_task(1, "Dog in yard", alert=True)
    assert _last(fake, "task", "create").one("type") == "1"


async def test_schedule_reschedule_cancel_complete(fake: FakeFR) -> None:
    with pytest.raises(ToolError):
        await server.schedule_appointment(1, 3)
    await server.schedule_appointment(1, 3, spot_id=32, subscription_id=100, reservation="tok-123")
    req = _last(fake, "appointment", "create")
    assert req.one("customerID") == "1" and req.one("type") == "3" and req.one("spotID") == "32"
    assert req.one("rejectOccupiedSpots") == "1" and req.one("reservation") == "tok-123"
    assert req.one("employeeID") == "9"

    await server.reschedule_appointment(500, spot_id=32, allow_double_booking=True)
    req = _last(fake, "appointment", "update")
    assert req.one("appointmentID") == "500" and req.one("rejectOccupiedSpots") == "0"
    with pytest.raises(ToolError):
        await server.reschedule_appointment(500)

    await server.cancel_appointment(500, reason="Customer request")
    req = _last(fake, "appointment", "cancel")
    assert req.one("cancelReason") == "Customer request" and req.one("cancelledBy") == "9"

    await server.complete_appointment(500, completion_notes="Done", amount_collected=50.0, payment_method=2)
    req = _last(fake, "appointment", "complete")
    assert req.one("status") == "1" and req.one("amountCollected") == "50.0" and req.one("completedBy") == "9"
    await server.complete_appointment(501, no_show=True)
    assert _last(fake, "appointment", "complete").one("status") == "2"


async def test_update_appointment_notes(fake: FakeFR) -> None:
    await server.update_appointment_notes(500, notes="Aerate + overseed, skip side yard", office_notes="Prepaid")
    req = _last(fake, "appointment", "update")
    assert req.one("notes") == "Aerate + overseed, skip side yard" and req.one("officeNotes") == "Prepaid"
    with pytest.raises(ToolError):
        await server.update_appointment_notes(500)


async def test_reserve_slot(fake: FakeFR) -> None:
    out = loads(await server.reserve_slot(spot_ids=[32, 34], minutes=10))
    assert out["reservation"] == "tok-123"
    req = _last(fake, "spot", "reserve")
    assert req.form["spotOptions[]"] == ["32", "34"] and req.one("duration") == "10"
    with pytest.raises(ToolError):
        await server.reserve_slot()


async def test_update_subscription_sends_only_given_fields(fake: FakeFR) -> None:
    out = loads(
        await server.update_subscription(
            100, frequency=30, billing_frequency=30, custom_date="2026-10-09", seasonal_start="03-01",
            seasonal_end="11-30", region_id=2, preferred_tech=11, preferred_day=3,
        )
    )
    assert out["changed"] == {
        "frequency": 30, "billingFrequency": 30, "customDate": "2026-10-09", "seasonalStart": "03-01",
        "seasonalEnd": "11-30", "regionID": 2, "preferredTech": 11, "preferredDays": 3,
    }
    req = _last(fake, "subscription", "update")
    assert req.one("subscriptionID") == "100" and req.one("frequency") == "30"
    assert req.one("billingFrequency") == "30" and req.one("customDate") == "2026-10-09"
    assert req.one("regionID") == "2" and req.one("preferredDays") == "3"
    assert "active" not in req.form and "soldBy" not in req.form
    with pytest.raises(ToolError, match="nothing to change"):
        await server.update_subscription(100)


async def test_writes_off_blocks_every_write_but_not_reads(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITES", "off")
    for coro in (
        server.add_note(1, "x"),
        server.set_red_notes(1, "x"),
        server.create_task(1, "x"),
        server.schedule_appointment(1, 3, spot_id=32),
        server.reschedule_appointment(500, spot_id=32),
        server.cancel_appointment(500),
        server.complete_appointment(500),
        server.reserve_slot(32),
        server.update_subscription(100, frequency=30),
        server.update_appointment_notes(500, notes="x"),
        server.update_note(800, text="x"),
    ):
        with pytest.raises(ToolError, match="writes are disabled"):
            await coro
    assert not [r for r in fake.requests if r.action not in ("search", "get")]
    assert loads(await server.find_customer("Jane Smith"))["count"] == 1


async def test_write_allowlist_blocks_other_customers_direct(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.add_note(2, "x")
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.set_red_notes(2, "x")
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.create_task(2, "x")
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.schedule_appointment(2, 3, spot_id=32)
    assert not [r for r in fake.requests if r.action not in ("search", "get")]

    await server.add_note(1, "allowed")
    assert _last(fake, "note", "create").one("customerID") == "1"


async def test_write_allowlist_resolves_existing_records(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    # appointment 500 belongs to customer 1 (allowed); 501 belongs to customer 2 (blocked).
    await server.update_appointment_notes(500, notes="ok")
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.update_appointment_notes(501, notes="blocked")
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.cancel_appointment(501)
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.complete_appointment(501)
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.reschedule_appointment(501, spot_id=32)

    # note 800 and subscription 100 both belong to customer 1.
    await server.update_note(800, text="ok")
    await server.update_subscription(100, frequency=30)

    assert not [r for r in fake.requests if r.entity in ("note", "appointment", "subscription") and r.one("customerID") == "2"]


async def test_write_allowlist_exempts_reserve_slot(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    out = loads(await server.reserve_slot(32))
    assert out["reservation"] == "tok-123"


async def test_write_allowlist_covers_generic_call_and_generated_tools(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    note_params = {"contactType": 5, "notes": "x", "date": TODAY, "showOnInvoice": False}
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await server.call("note", "create", {**note_params, "customerID": 2})
    await server.call("note", "create", {**note_params, "customerID": 1})
    assert _last(fake, "note", "create").one("customerID") == "1"

    monkeypatch.setenv("FR_EXPOSE_ENTITIES", "note")
    mcp = MCPServer("t")
    generated.register_generated_tools(
        mcp,
        _client_for(fake),
        server.SPEC,
        writes_enabled=lambda: True,
        allow_delete=lambda: True,
        allow_charges=lambda: False,
        resolve_write_customer=server._resolve_write_customer,
        check_customer_allowed=server._require_customer_allowed,
    )
    note_args = {"contactType": 5, "notes": "x", "date": TODAY, "showOnInvoice": False}
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await mcp.call_tool("fr_note_create", {**note_args, "customerID": 2})
    result = await mcp.call_tool("fr_note_create", {**note_args, "customerID": 1})
    assert json.loads(result.content[0].text)["success"] is True


async def test_write_allowlist_reported_in_health_check(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1, 2")
    out = loads(await server.health_check())
    assert out["config"]["writeCustomerAllowlist"] == [1, 2]
    monkeypatch.delenv("FR_WRITE_CUSTOMER_IDS")
    out = loads(await server.health_check())
    assert out["config"]["writeCustomerAllowlist"] is None


# ---------------------------------------------------------------------------
# Generated tools + HTTP app
# ---------------------------------------------------------------------------


def test_generated_tools_off_by_default() -> None:
    assert server._generated_tools == []


async def test_generated_tools_for_selected_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_EXPOSE_ENTITIES", "customer, appointment")
    fake = FakeFR()
    mcp = MCPServer("t")
    names = generated.register_generated_tools(
        mcp, _client_for(fake), server.SPEC,
        writes_enabled=lambda: True, allow_delete=lambda: False, allow_charges=lambda: False,
    )
    assert len(names) == 12 and "fr_appointment_cancel" in names and "fr_customer_search" in names
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["fr_appointment_cancel"].input_schema["required"] == ["appointmentID"]
    assert "Write action" in tools["fr_appointment_cancel"].description
    result = await mcp.call_tool("fr_customer_search", {"lname": "Smith"})
    assert json.loads(result.content[0].text)["customerIDs"] == [1]
    assert _last(fake, "customer", "search").one("lname") == "Smith"


async def test_generated_tools_expose_all(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_EXPOSE_ALL", "1")
    mcp = MCPServer("t")
    names = generated.register_generated_tools(
        mcp, _client_for(FakeFR()), server.SPEC,
        writes_enabled=lambda: True, allow_delete=lambda: False, allow_charges=lambda: False,
    )
    assert len(names) == 187


async def test_generated_delete_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_EXPOSE_ENTITIES", "note")
    mcp = MCPServer("t")
    generated.register_generated_tools(
        mcp, _client_for(FakeFR()), server.SPEC,
        writes_enabled=lambda: True, allow_delete=lambda: False, allow_charges=lambda: False,
    )
    with pytest.raises(ToolError, match="FR_ALLOW_DELETE"):
        await mcp.call_tool("fr_note_delete", {"customerID": 1, "contactID": 800})


def test_http_app_secret_path_healthz_and_bearer(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    from starlette.testclient import TestClient

    monkeypatch.setenv("MCP_PATH_SECRET", "s3cret")
    monkeypatch.setenv("MCP_BEARER_TOKEN", "bear")
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with TestClient(server.build_http_app()) as http:
        assert http.get("/healthz").text == "ok"
        assert http.post("/s3cret/mcp", json=body, headers=headers).status_code == 401
        auth = {**headers, "Authorization": "Bearer bear"}
        r = http.post("/s3cret/mcp", json=body, headers=auth)
        assert r.status_code == 200
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert {"find_customer", "update_subscription", "call", "cancel_appointment"} <= names
        assert len(names) == 30
        assert http.post("/mcp", json=body, headers=auth).status_code == 404


def test_main_never_logs_the_secret_path(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import uvicorn

    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_PATH_SECRET", "s3cret-value")
    monkeypatch.setenv("PORT", "9999")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append(kwargs))

    server.main()

    assert calls and calls[0]["access_log"] is False, "uvicorn access log must be off: it would log the secret path"
    printed = capsys.readouterr().err
    assert "s3cret-value" not in printed
