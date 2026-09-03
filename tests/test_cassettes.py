"""Recorded-response replay: prove the client parses FieldRoutes' *real* envelope.

`tests/test_wire_format.py`'s `FakeFR` speaks a dialect built from reading the
swagger and the wire-format notes in CLAUDE.md -- which is exactly how the
`office/get` bug shipped: the fake faithfully reproduced our own misreading of
the envelope, so it passed while the real API returned nothing. These tests
replay verbatim, credential-scrubbed responses captured from a real call
against the Zest office (see each cassette's `_comment`) instead of a
hand-written approximation, so a future misreading of the envelope shape
fails here before it ships.

Cassettes are `tests/cassettes/*.json`: `{"entity", "action", "response"}`,
where `response` is the exact JSON body FieldRoutes sent back. Add a new one
by calling the live server through the deployed MCP connector, copying the
`call` tool's raw output, and scrubbing `authenticationKey`/`authenticationToken`
in the `params` echo to `"SCRUBBED"`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

os.environ.setdefault("FR_SUBDOMAIN", "zest")
os.environ.setdefault("FR_AUTH_KEY", "key")
os.environ.setdefault("FR_AUTH_TOKEN", "token")

from fr_mcp import server  # noqa: E402
from fr_mcp.client import FieldRoutesClient, RateLimiter  # noqa: E402

CASSETTE_DIR = Path(__file__).parent / "cassettes"


def _load_cassettes() -> dict[str, dict[str, Any]]:
    cassettes: dict[str, dict[str, Any]] = {}
    for path in sorted(CASSETTE_DIR.glob("*.json")):
        data = json.loads(path.read_text())
        cassettes[f"{data['entity']}/{data['action']}"] = data["response"]
    return cassettes


CASSETTES = _load_cassettes()


class CassetteTransport:
    """Replays a fixed, pre-recorded response per `entity/action` -- no request
    matching beyond that, since these prove envelope parsing, not search semantics
    (FakeFR in test_wire_format.py already covers filter/pagination behavior)."""

    def __init__(self, cassettes: dict[str, dict[str, Any]]):
        self.cassettes = cassettes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        _, api, entity, action = request.url.path.split("/")
        assert api == "api"
        key = f"{entity}/{action}"
        if key not in self.cassettes:
            raise AssertionError(f"no cassette recorded for {key}; add one to tests/cassettes/")
        return httpx.Response(200, json=self.cassettes[key])


def _client(transport: CassetteTransport) -> FieldRoutesClient:
    return FieldRoutesClient(
        subdomain="zest",
        auth_key="key",
        auth_token="token",
        transport=httpx.MockTransport(transport.handler),
        rate_limiter=RateLimiter(limit=100000),
    )


def test_all_cassettes_present() -> None:
    assert {"office/get", "office/search", "customer/get"} <= set(CASSETTES)


async def test_cassette_office_get_extracts_the_plural_key() -> None:
    # The real bug: extract_records used to guess the singular entity name
    # ("office") and, failing that, fall back to "first list value in the
    # body" -- which found `ignoredParams: []` before ever reaching `offices`,
    # and returned nothing. This is the exact response that exposed it.
    transport = CassetteTransport(CASSETTES)
    async with _client(transport) as client:
        rows = await client.get("office", [1])
    assert len(rows) == 1
    assert rows[0]["officeName"] == "Zest Lawn & Pest"
    assert rows[0]["timeZone"] == "America/Los_Angeles"


async def test_cassette_office_search_extracts_ids() -> None:
    transport = CassetteTransport(CASSETTES)
    async with _client(transport) as client:
        data = await client.search("office", {"officeID": 1})
        ids = client.extract_ids("office", data)
    assert ids == [1]


async def test_cassette_no_credentials_survive_extraction() -> None:
    # client.call() strips `params` (the request echo, auth included) before
    # returning; prove it against the real recorded envelope, not just our
    # own fake's copy of the shape.
    transport = CassetteTransport(CASSETTES)
    async with _client(transport) as client:
        data = await client.call("office", "get", {"officeIDs": [1]})
    assert "params" not in data
    dumped = json.dumps(data)
    assert "authenticationKey" not in dumped
    assert "authenticationToken" not in dumped
    assert "SCRUBBED" not in dumped


async def test_cassette_usage_adopts_fieldroutes_authoritative_count() -> None:
    # tokenUsage in the real envelope is FieldRoutes' own count, shared across
    # every integration touching the office -- authoritative over our estimate.
    transport = CassetteTransport(CASSETTES)
    async with _client(transport) as client:
        await client.call("office", "get", {"officeIDs": [1]})
        client.usage.sync(CASSETTES["office/get"]["tokenUsage"])
    snap = client.usage.snapshot()
    assert snap["reads"] == 5  # requestsReadToday from the cassette, not our post-one-call count of 1


async def test_cassette_customer_get_ids_are_comma_strings_not_arrays() -> None:
    # The second real bug: customer.subscriptionIDs/appointmentIDs are
    # comma-separated strings on the wire, not JSON arrays, despite the
    # swagger's declared type: array. The raw field must stay a string here
    # (that's what FieldRoutes actually sent); _id_list() is what parses it.
    transport = CassetteTransport(CASSETTES)
    async with _client(transport) as client:
        rows = await client.get("customer", [10000])
    assert len(rows) == 1
    c = rows[0]
    assert c["subscriptionIDs"] == "1"
    assert c["appointmentIDs"] == "10002,10006,16404"
    assert server._id_list(c["subscriptionIDs"]) == [1]
    assert server._id_list(c["appointmentIDs"]) == [10002, 10006, 16404]


async def test_cassette_customer_get_all_values_are_strings() -> None:
    # Confirmed live: every field is a JSON string, even numeric/ID ones.
    # _int()/float() handle this; nothing here should need "fixing".
    transport = CassetteTransport(CASSETTES)
    async with _client(transport) as client:
        rows = await client.get("customer", [10000])
    c = rows[0]
    assert c["customerID"] == "10000" and isinstance(c["customerID"], str)
    assert c["balance"] == "0.00" and isinstance(c["balance"], str)
    assert server._int(c["customerID"]) == 10000


async def test_cassette_customer_360_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    # The full curated tool, against the real recorded envelope: proves the
    # office/get + customer/get fixes and the subscriptionIDs parsing fix
    # together produce a working customer_360 call, not just that each piece
    # works in isolation. The subscription/task/note/appointment searches
    # this also triggers have no cassette, so add minimal ones for those
    # entities on the CassetteTransport rather than failing on a missing key.
    transport = CassetteTransport(
        {
            **CASSETTES,
            "subscription/get": {"success": True, "propertyName": "subscriptions", "subscriptions": []},
            "appointment/search": {"success": True, "propertyName": "appointmentIDs", "appointmentIDs": []},
            "task/search": {"success": True, "propertyName": "taskIDs", "taskIDs": []},
            "note/search": {"success": True, "propertyName": "noteIDs", "noteIDs": []},
            "employee/search": {"success": True, "propertyName": "employeeIDs", "employeeIDs": []},
            "serviceType/search": {"success": True, "propertyName": "typeIDs", "typeIDs": []},
            "region/search": {"success": True, "propertyName": "regionIDs", "regionIDs": []},
        }
    )
    monkeypatch.setattr(server, "_client", _client(transport))
    server._reset_cache()
    out = json.loads(await server.customer_360(10000))
    assert out["customer"]["name"] == "Test Sean"
    assert out["customer"]["propertyAddress"] == "6948 W 2nd St, Rio Linda, CA, 95673"
    # subscriptionIDs "1" must have driven a subscription/get for id 1, not garbage ids.
    get_reqs = [r for r in transport.requests if r.url.path.endswith("/subscription/get")]
    assert len(get_reqs) == 1
    body = get_reqs[0].content.decode()
    assert "subscriptionIDs%5B%5D=1" in body
    assert "subscriptionIDs%5B%5D=0" not in body  # the character-by-character bug's signature
