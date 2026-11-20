"""Hardening of the two layers every request crosses.

Client (`FieldRoutesClient.call`): a write gate that matches action *families* -- the old
exact-name checks let `deleteAddOn`, `setAddOns`, `payment/createRefund` and every
employee write through -- an optional per-deployment endpoint allowlist, and a write that
is never sent twice after an answer that may mean FieldRoutes already saved it.

HTTP (`_Hardening`): constant-time bearer check on raw bytes, two tokens for rotation,
a body-size cap, and no-store/security headers on every response.
"""

from __future__ import annotations

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from fr_mcp import server
from fr_mcp.client import FieldRoutesError, RateLimiter, WriteNotAllowed, write_block_reason

from conftest import FakeFR, _client_for  # noqa: E402

GATE_FLAGS = ("FR_ALLOW_DELETE", "FR_ALLOW_CHARGES", "FR_ALLOW_STAFF_ADMIN", "FR_WRITE_ENDPOINTS")


@pytest.fixture(autouse=True)
def _clean_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in GATE_FLAGS:
        monkeypatch.delenv(name, raising=False)


# --- write gate --------------------------------------------------------------

@pytest.mark.parametrize(
    "endpoint",
    [
        "ticket/deleteAddOn",
        "subscription/deleteInitialAddOn",
        "ticket/setAddOns",
        "subscription/setInitialAddOns",
        "note/delete",
        "payment/createRefund",
        "payment/create",
        "paymentProfile/create",
        "customer/createPaymentProfile",
        "employee/create",
        "employee/update",
        "employee/reset",
        "group/update",
        "skill/create",
    ],
)
async def test_dangerous_writes_never_reach_fieldroutes(endpoint: str) -> None:
    fake = FakeFR()
    entity, action = endpoint.split("/")
    async with _client_for(fake) as client:
        with pytest.raises(WriteNotAllowed, match="refused by this deployment"):
            await client.call(entity, action, {"customerID": 1})
    assert fake.requests == [], "blocked before anything was sent, so no quota was spent either"


@pytest.mark.parametrize(
    ("flag", "endpoint"),
    [
        ("FR_ALLOW_DELETE", "ticket/setAddOns"),
        ("FR_ALLOW_CHARGES", "payment/createRefund"),
        ("FR_ALLOW_STAFF_ADMIN", "employee/update"),
    ],
)
def test_each_family_has_its_own_switch(monkeypatch: pytest.MonkeyPatch, flag: str, endpoint: str) -> None:
    entity, action = endpoint.split("/")
    assert write_block_reason(entity, action) is not None
    monkeypatch.setenv(flag, "1")
    assert write_block_reason(entity, action) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        # Quoting and routing
        "customer/create", "customer/update", "subscription/create", "subscription/update",
        "subscription/updateLeadStage", "contract/create", "appointment/create", "appointment/update",
        "appointment/cancel", "spot/reserve", "note/create", "note/update", "task/create",
        # The video server's writes go through this same client
        "review/create", "appointment/complete",
    ],
)
def test_everyday_writes_are_not_blocked_by_default(endpoint: str) -> None:
    assert write_block_reason(*endpoint.split("/")) is None


def test_reads_are_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITE_ENDPOINTS", "note/create")
    for endpoint in ("payment/search", "employee/get", "customer/search", "ticket/getAddOns"):
        assert write_block_reason(*endpoint.split("/")) is None


async def test_endpoint_allowlist_limits_a_deployment_to_its_job(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITE_ENDPOINTS", " note/create , customer/create ")
    fake = FakeFR()
    async with _client_for(fake) as client:
        with pytest.raises(WriteNotAllowed, match="FR_WRITE_ENDPOINTS"):
            await client.call("appointment", "cancel", {"appointmentID": 1})
        assert fake.requests == []
        await client.call("customer", "search", {})  # reads still work
    assert write_block_reason("note", "create") is None
    # The list narrows; it never unlocks a family that has its own switch.
    monkeypatch.setenv("FR_WRITE_ENDPOINTS", "payment/createRefund")
    assert write_block_reason("payment", "createRefund") is not None


async def test_generic_call_tool_obeys_the_gate(fake: FakeFR) -> None:
    with pytest.raises(ToolError, match="FR_ALLOW_CHARGES"):
        await server.call("payment", "createRefund", {"paymentID": 1, "amount": 5})
    with pytest.raises(ToolError, match="FR_ALLOW_STAFF_ADMIN"):
        await server.call("employee", "update", {"employeeID": 10002, "active": 0})
    with pytest.raises(ToolError, match="FR_ALLOW_DELETE"):
        await server.call("ticket", "setAddOns", {"ticketID": 1, "addons": []})
    assert not [r for r in fake.requests if r.entity in ("payment", "employee", "ticket")]


# --- writes are not resent after an ambiguous failure ------------------------

def _flaky_client(fake: FakeFR, failures: list[Exception]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        if failures:
            raise failures.pop(0)
        return fake.handler(request)

    from fr_mcp.client import FieldRoutesClient

    return FieldRoutesClient(
        subdomain="zest", auth_key="key", auth_token="token",
        transport=httpx.MockTransport(handler), rate_limiter=RateLimiter(limit=100000),
    )


async def test_a_write_answered_5xx_is_not_sent_again() -> None:
    fake = FakeFR()
    fake.next_status = 503
    async with _client_for(fake) as client:
        with pytest.raises(FieldRoutesError, match="OUTCOME UNKNOWN"):
            await client.call("note", "create", {"customerID": 1, "notes": "x", "contactType": 0})
    assert len(fake.requests) == 1


async def test_a_write_that_timed_out_mid_flight_is_not_sent_again() -> None:
    fake = FakeFR()
    client = _flaky_client(fake, [httpx.ReadTimeout("slow")])
    async with client:  # type: ignore[attr-defined]
        with pytest.raises(FieldRoutesError, match="OUTCOME UNKNOWN"):
            await client.call("customer", "create", {"fname": "A", "lname": "B"})  # type: ignore[attr-defined]
    assert fake.requests == []


async def test_a_write_whose_connection_never_opened_is_retried() -> None:
    fake = FakeFR()
    client = _flaky_client(fake, [httpx.ConnectError("refused")])
    async with client:  # type: ignore[attr-defined]
        await client.call("note", "create", {"customerID": 1, "notes": "x", "contactType": 0})  # type: ignore[attr-defined]
    assert len(fake.requests) == 1


async def test_reads_still_retry_after_a_5xx() -> None:
    fake = FakeFR()
    fake.next_status = 503
    async with _client_for(fake) as client:
        assert (await client.call("office", "search"))["success"] is True
    assert len(fake.requests) == 2


# --- HTTP layer --------------------------------------------------------------

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _http(monkeypatch: pytest.MonkeyPatch, **env: str):  # type: ignore[no-untyped-def]
    from starlette.testclient import TestClient

    monkeypatch.setenv("MCP_PATH_SECRET", "s3cret")
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("MCP_MAX_BODY_BYTES", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return TestClient(server.build_http_app())


def test_two_tokens_both_work_during_a_rotation(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    with _http(monkeypatch, MCP_BEARER_TOKEN="old-token, new-token") as http:
        for token in ("old-token", "new-token"):
            r = http.post("/s3cret/mcp", json=LIST, headers={**HEADERS, "Authorization": f"Bearer {token}"})
            assert r.status_code == 200
        bad = http.post("/s3cret/mcp", json=LIST, headers={**HEADERS, "Authorization": "Bearer new-toke"})
        assert bad.status_code == 401 and bad.headers["www-authenticate"] == "Bearer"
        assert http.post("/s3cret/mcp", json=LIST, headers={**HEADERS, "Authorization": "new-token"}).status_code == 401
        assert http.get("/healthz").text == "ok", "health check stays open for Railway"


def test_a_non_utf8_authorization_header_is_a_401_not_a_crash(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    with _http(monkeypatch, MCP_BEARER_TOKEN="tok") as http:
        r = http.post("/s3cret/mcp", content=b"{}", headers={**HEADERS, "Authorization": b"Bearer \xff\xfe"})
        assert r.status_code == 401


def test_every_response_says_no_store(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    with _http(monkeypatch) as http:
        for r in (
            http.get("/healthz"),
            http.post("/s3cret/mcp", json=LIST, headers=HEADERS),
            http.post("/wrong/mcp", json=LIST, headers=HEADERS),
        ):
            assert r.headers["cache-control"] == "no-store"
            assert r.headers["x-content-type-options"] == "nosniff"
            assert r.headers["referrer-policy"] == "no-referrer"


def test_oversized_bodies_are_refused_before_the_app(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    with _http(monkeypatch, MCP_MAX_BODY_BYTES="2048") as http:
        assert http.post("/s3cret/mcp", content=b"x" * 4096, headers=HEADERS).status_code == 413

        def chunks():  # no Content-Length: the cap still holds while streaming
            for _ in range(8):
                yield b"x" * 512

        assert http.post("/s3cret/mcp", content=chunks(), headers=HEADERS).status_code == 413
        assert http.post("/s3cret/mcp", json=LIST, headers=HEADERS).status_code == 200
