"""Wire-format and tool tests against a fake FieldRoutes served by httpx.MockTransport.

No network and no credentials: `FakeFR` (in conftest.py, shared with the other
test files in this suite) implements just enough of `POST /api/{entity}/{action}`
(form body, auth in body, search filter objects, PHP-style ID arrays, 1000-record
get chunks, HTTP 200 on failure, the real response envelope) to prove the client
speaks FieldRoutes' dialect and the tools shape output correctly.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any

import httpx
import pytest

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

from conftest import TODAY, FakeFR, Req, _client_for, _last, loads  # noqa: E402

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
    # Verified live: `phone` is an exact 10-digit match and CONTAINS silently returns nothing,
    # so the filter must be a bare normalized number, not an operator object.
    out = loads(await server.find_customer(phone="(916) 555-1234"))
    assert out["customers"][0]["customerID"] == 1
    assert _last(fake, "customer", "search").one("phone") == "9165551234"
    out = loads(await server.find_customer(phone="+1 916-555-1234"))  # leading country code dropped
    assert out["customers"][0]["customerID"] == 1
    with pytest.raises(ToolError, match="10-digit"):
        await server.find_customer(phone="555-1234")  # partial numbers can't be searched
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
    # Live values are strings: "reserved": "0" is an *open* spot, not a reserved one
    # (seen on every open spot of a real route before this was fixed).
    assert states[35] == "open"
    booked = next(s for s in out["stops"] if s["spotID"] == 30)
    assert booked["appointment"]["customer"]["name"] == "Jane Smith"


async def test_subscription_shaping_handles_live_string_sentinels(fake: FakeFR) -> None:
    # Subscription 101 mirrors a real record: frequency "CUSTOM", regionID "0", soldBy2/3 "0".
    out = loads(await server.subscription_details(101))
    sub = out["subscription"]
    assert sub["frequencyText"] == "custom schedule"
    assert "region" not in sub  # "0" means unassigned -- must not render as region "0"
    assert sub["soldByName"] == "Iggy Artemenko"
    assert "soldBy2Name" not in sub and "soldBy3Name" not in sub  # no null-valued noise keys
    assert sub["preferredTechName"] is None


async def test_appointment_tech_falls_back_to_route_not_booking_employee(fake: FakeFR) -> None:
    # `employeeID` is who booked the appointment (office staff / the import job), not the tech.
    # Unassigned appointments take the route's tech; with no route they stay unassigned.
    on_route = {"appointmentID": "900", "customerID": "1", "routeID": "20", "assignedTech": "0", "employeeID": "9"}
    no_route = {"appointmentID": "901", "customerID": "1", "routeID": "999", "assignedTech": "0", "employeeID": "9",
                "servicedBy": "0"}
    rows = await server._shape_appointments([on_route, no_route], customers={})
    assert rows[0]["tech"] == "Carlos Quijas" and rows[0]["bookedBy"] == "Office Admin"
    assert rows[1]["tech"] is None and rows[1]["bookedBy"] == "Office Admin"
    assert "servicedBy" not in rows[1]  # "0" = nobody


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


async def test_due_for_service_trims_billing_and_sales_fields_but_keeps_scheduling_ones(fake: FakeFR) -> None:
    # due_for_service is a skim-a-list view -- billing/sales/administrative fields are
    # dropped to keep responses small at real-office volume, but subscription_details
    # (the full-record tool) must still show them for the same subscription (100).
    out = loads(await server.due_for_service(days=3, service_type_id=3))
    sub = out["subscriptions"][0]
    assert sub["subscriptionID"] == 100 and sub["frequencyText"] == "every 365 days"
    for dropped in ("soldBy", "soldByName", "billingFrequency", "billingFrequencyText", "contractValue"):
        assert dropped not in sub

    details = loads(await server.subscription_details(100))
    full = details["subscription"]
    assert full["soldByName"] == "Iggy Artemenko" and full["contractValue"] == 208.0
    assert full["billingFrequencyText"] == "one-time"


def test_due_for_service_default_limit_is_50() -> None:
    import inspect

    assert inspect.signature(server.due_for_service).parameters["limit"].default == 50


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


async def test_add_note_accepts_note_type_id_zero(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    # Note Type 0 ("Notes") is a real, valid type on live FieldRoutes accounts -- `note_type_id
    # or _default_note_type()` used to treat an explicit 0 as falsy and fall through to the
    # env default (or raise if unset), silently ignoring the caller's choice.
    monkeypatch.delenv("FR_DEFAULT_NOTE_TYPE_ID")
    await server.add_note(1, "x", note_type_id=0)
    assert _last(fake, "note", "create").one("contactType") == "0"


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


async def test_service_schedule_groups_by_subscription_and_zone(fake: FakeFR) -> None:
    out = loads(await server.service_schedule(service_type_id=3, start="2025-10-01", days=400))
    assert out["subscriptionCount"] == 1
    group = out["groups"][0]
    assert group["subscriptionID"] == 100
    assert group["zone"] == "Folsom"
    assert group["customer"]["name"] == "Jane Smith"
    assert group["frequencyText"] == "every 365 days"
    # appointment 500 (today, pending) and 502 (2025-10-09, completed) both belong
    # to subscription 100 and both fall in this window; sorted oldest-first.
    assert [a["appointmentID"] for a in group["appointments"]] == [502, 500]
    assert group["appointmentCount"] == 2
    assert out["moved"] == []


async def test_service_schedule_appointment_with_no_subscription_gets_its_own_group(fake: FakeFR) -> None:
    out = loads(await server.service_schedule(service_type_id=4))
    # appointment 501 (customer 2, service type 4) has no subscriptionID in the seed data.
    group = next(g for g in out["groups"] if g["subscriptionID"] is None)
    assert group["zone"] is None
    assert group["customer"]["name"] == "John Smithers"
    assert [a["appointmentID"] for a in group["appointments"]] == [501]


async def test_service_schedule_orphan_appointments_for_different_customers_stay_separate(fake: FakeFR) -> None:
    # A second customer's stand-alone appointment (same service type, no subscription) must
    # not be merged into customer 2's "unassigned" group just because both lack a subscription.
    fake.data["appointment"][503] = {
        "appointmentID": 503, "officeID": 7, "customerID": 3, "date": TODAY, "type": 4, "status": 0,
    }
    out = loads(await server.service_schedule(service_type_id=4))
    orphan_groups = {g["customer"]["customerID"]: g for g in out["groups"] if g["subscriptionID"] is None}
    assert set(orphan_groups) == {2, 3}
    assert [a["appointmentID"] for a in orphan_groups[2]["appointments"]] == [501]
    assert [a["appointmentID"] for a in orphan_groups[3]["appointments"]] == [503]


async def test_service_schedule_stand_alone_sentinel_excluded_from_subscription_lookup(fake: FakeFR) -> None:
    # subscriptionID -1 means "stand-alone service/reservice", not a real subscription to
    # fetch -- it must group like "no subscription" (by customer), and never reach subscription/get.
    fake.data["appointment"][503] = {
        "appointmentID": 503, "officeID": 7, "customerID": 3, "subscriptionID": -1,
        "date": TODAY, "type": 4, "status": 0,
    }
    out = loads(await server.service_schedule(service_type_id=4))
    orphan_groups = {g["customer"]["customerID"]: g for g in out["groups"] if g["subscriptionID"] is None}
    assert set(orphan_groups) == {2, 3}
    assert [a["appointmentID"] for a in orphan_groups[3]["appointments"]] == [503]
    assert not any(r.entity == "subscription" and r.action == "get" for r in fake.requests)


async def test_service_schedule_region_filter_excludes_non_matching_and_unassigned_groups(fake: FakeFR) -> None:
    out = loads(await server.service_schedule(start="2025-10-01", days=400, region_id=2))
    assert {g["subscriptionID"] for g in out["groups"]} == {100}  # only Folsom (region 2) subscription 100
    # region_id is pushed down to subscription/search, then appointment/search's
    # subscriptionIDs filter -- not fetched in full and filtered in memory.
    sub_search = _last(fake, "subscription", "search")
    assert json.loads(sub_search.one("regionID")) == 2
    appt_search = _last(fake, "appointment", "search")
    assert json.loads(appt_search.one("subscriptionIDs")) == {"operator": "IN", "value": [100]}

    before = len(fake.requests)
    out_other = loads(await server.service_schedule(start="2025-10-01", days=400, region_id=999))
    assert out_other["groups"] == []
    # no subscriptions in that region -> short-circuits before ever searching appointments.
    assert not any(r.entity == "appointment" and r.action == "search" for r in fake.requests[before:])


async def test_service_schedule_status_filter(fake: FakeFR) -> None:
    pending = loads(await server.service_schedule(service_type_id=3, status="pending"))
    assert [a["appointmentID"] for g in pending["groups"] for a in g["appointments"]] == [500]
    completed = loads(await server.service_schedule(service_type_id=3, start="2025-10-01", days=400, status="completed"))
    assert [a["appointmentID"] for g in completed["groups"] for a in g["appointments"]] == [502]
    with pytest.raises(ToolError, match="status must be one of"):
        await server.service_schedule(status="bogus")


async def test_service_schedule_move_reschedules_via_the_same_tool(fake: FakeFR) -> None:
    out = loads(await server.service_schedule(move=[{"appointment_id": 500, "spot_id": 32}]))
    assert out["moved"][0]["appointmentID"] == 500
    assert out["moved"][0]["result"]["success"] is True
    req = _last(fake, "appointment", "update")
    assert req.one("appointmentID") == "500" and req.one("spotID") == "32"


async def test_service_schedule_move_requires_appointment_id() -> None:
    with pytest.raises(ToolError, match="appointment_id"):
        await server.service_schedule(move=[{"spot_id": 32}])


async def test_service_schedule_move_rejects_non_numeric_appointment_id_cleanly() -> None:
    # A bad appointment_id must raise a clean ToolError (via _int), not an unhandled
    # ValueError from a raw int(...) call.
    with pytest.raises(ToolError, match="numeric appointment_id"):
        await server.service_schedule(move=[{"appointment_id": "abc", "spot_id": 32}])


async def test_service_schedule_move_stops_at_first_failure_before_any_read(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FR_WRITES", "off")
    with pytest.raises(ToolError, match="writes are disabled"):
        await server.service_schedule(move=[{"appointment_id": 500, "spot_id": 32}])
    assert fake.requests == []  # the guarded move fails before the grouped read ever runs


async def test_service_schedule_move_preserves_earlier_successes_on_a_later_failure(fake: FakeFR) -> None:
    fake.data["appointment"][504] = {
        "appointmentID": 504, "officeID": 7, "customerID": 1, "subscriptionID": 100, "routeID": 20,
        "date": TODAY, "type": 3, "status": 0,
    }
    fake.fail_write_after = 1  # the 2nd appointment/update call fails; the 1st must still succeed
    out = loads(
        await server.service_schedule(
            move=[{"appointment_id": 500, "spot_id": 32}, {"appointment_id": 504, "spot_id": 33}]
        )
    )
    assert len(out["moved"]) == 2
    assert out["moved"][0]["appointmentID"] == 500
    assert out["moved"][0]["result"]["success"] is True
    assert out["moved"][1]["appointmentID"] == 504
    assert "conflict" in out["moved"][1]["error"]
    updates = [r for r in fake.requests if r.entity == "appointment" and r.action == "update"]
    assert [u.one("appointmentID") for u in updates] == ["500", "504"]  # stopped after the failure


async def test_service_schedule_move_batches_allowlist_customer_resolution(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake.data["appointment"][504] = {
        "appointmentID": 504, "officeID": 7, "customerID": 1, "subscriptionID": 100, "routeID": 20,
        "date": TODAY, "type": 3, "status": 0,
    }
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    out = loads(
        await server.service_schedule(
            move=[{"appointment_id": 500, "spot_id": 32}, {"appointment_id": 504, "spot_id": 33}]
        )
    )
    assert [m["appointmentID"] for m in out["moved"]] == [500, 504]
    assert all(m["result"]["success"] is True for m in out["moved"])
    # The first appointment/get is the allowlist-customer resolution for the move batch --
    # it must cover both targets in one call, not one request per move entry. (A second,
    # unrelated appointment/get follows later to hydrate the grouped read.)
    gets = [r for r in fake.requests if r.entity == "appointment" and r.action == "get"]
    assert set(gets[0].form["appointmentIDs[]"]) == {"500", "504"}


async def test_service_schedule_move_blocks_a_customer_outside_the_allowlist(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "2")  # appointment 500 belongs to customer 1
    with pytest.raises(ToolError, match="writes are restricted"):
        await server.service_schedule(move=[{"appointment_id": 500, "spot_id": 32}])
    assert not any(r.entity == "appointment" and r.action == "update" for r in fake.requests)


async def test_service_schedule_move_preserves_earlier_success_when_a_later_entry_is_outside_the_allowlist(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch
) -> None:
    # appointment 500 belongs to customer 1 (allowed); appointment 501 belongs to customer 2
    # (not allowed) -- the allowlist rejection on the 2nd entry must not discard the 1st's
    # already-completed move, same as a FieldRoutesError on a later entry.
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    out = loads(
        await server.service_schedule(
            move=[{"appointment_id": 500, "spot_id": 32}, {"appointment_id": 501, "spot_id": 33}]
        )
    )
    assert len(out["moved"]) == 2
    assert out["moved"][0]["appointmentID"] == 500
    assert out["moved"][0]["result"]["success"] is True
    assert out["moved"][1]["appointmentID"] == 501
    assert "writes are restricted" in out["moved"][1]["error"]
    updates = [r for r in fake.requests if r.entity == "appointment" and r.action == "update"]
    assert [u.one("appointmentID") for u in updates] == ["500"]  # stopped before attempting 501


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
        assert len(names) == 31
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
