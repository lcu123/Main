"""MCP protocol/transport tests: real JSON-RPC over the Starlette HTTP app.

Everything else in this suite calls tool functions directly in Python.
These go through the actual wire protocol Claude speaks to the server --
`initialize`, `tools/list`, `tools/call` -- via `starlette.testclient.TestClient`
against `server.build_http_app()`, to catch bugs that only exist at that layer:
a tool error surfacing as HTTP 500 instead of a clean `isError` result, an
unknown tool or bad arguments crashing the transport, a session header being
required despite `stateless_http=True`, or the response coming back as SSE
instead of JSON despite `json_response=True`.
"""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from conftest import FakeFR
from fr_mcp import server

HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture
def http(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MCP_PATH_SECRET", "s3cret")
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    with TestClient(server.build_http_app()) as client:
        yield client


def _rpc(http: TestClient, method: str, params: dict | None = None, id_: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    resp = http.post("/s3cret/mcp", json=body, headers=HEADERS)
    assert resp.status_code == 200, f"{method} must never be HTTP 500, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"] == "application/json", "must respond JSON, not SSE, per json_response=True"
    return resp.json()


def _call_tool(http: TestClient, name: str, arguments: dict, id_: int = 2) -> dict:
    return _rpc(http, "tools/call", {"name": name, "arguments": arguments}, id_=id_)


def _tool_text(rpc_response: dict) -> str:
    return rpc_response["result"]["content"][0]["text"]


def test_initialize_handshake(http: TestClient) -> None:
    resp = _rpc(
        http,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
    )
    result = resp["result"]
    assert result["serverInfo"]["name"] == "fieldroutes"
    assert result["protocolVersion"] == "2025-06-18"
    assert "find_customer" in result["instructions"]
    assert result["capabilities"]["tools"] is not None


def test_tools_call_read_tool_returns_isError_false(http: TestClient, fake: FakeFR) -> None:
    resp = _call_tool(http, "find_customer", {"query": "Jane Smith"})
    assert resp["result"]["isError"] is False
    out = json.loads(_tool_text(resp))
    assert out["count"] == 1
    assert out["customers"][0]["name"] == "Jane Smith"


def test_tools_call_write_tool_returns_isError_false(http: TestClient, fake: FakeFR) -> None:
    resp = _call_tool(http, "add_note", {"customer_id": 1, "text": "Left gate open."})
    assert resp["result"]["isError"] is False
    out = json.loads(_tool_text(resp))
    assert out["success"] is True
    req = next(r for r in fake.requests if r.entity == "note" and r.action == "create")
    assert req.one("customerID") == "1"


def test_tool_error_surfaces_as_isError_not_http_500(http: TestClient) -> None:
    resp = _call_tool(http, "describe_endpoint", {"endpoint": "nope/nothing"})
    assert resp["result"]["isError"] is True
    assert "Unknown endpoint" in _tool_text(resp)
    # No JSON-RPC-level error object either -- it's a normal result, just flagged.
    assert "error" not in resp


def test_unknown_tool_name_is_isError_not_http_500(http: TestClient) -> None:
    resp = _call_tool(http, "this_tool_does_not_exist", {})
    assert resp["result"]["isError"] is True
    assert "this_tool_does_not_exist" in _tool_text(resp)


def test_missing_required_argument_is_isError_not_http_500(http: TestClient) -> None:
    resp = _call_tool(http, "describe_endpoint", {})
    assert resp["result"]["isError"] is True
    assert "endpoint" in _tool_text(resp).lower()


def test_tools_list_matches_curated_plus_generic_count(http: TestClient) -> None:
    resp = _rpc(http, "tools/list")
    names = {t["name"] for t in resp["result"]["tools"]}
    assert len(names) == 31
    assert {"find_customer", "call", "update_subscription"} <= names


def test_stateless_no_session_id_required_across_requests(http: TestClient) -> None:
    # stateless_http=True: no mcp-session-id header on the response, and a
    # second, unrelated call succeeds without ever having received or sent one.
    first = http.post(
        "/s3cret/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=HEADERS
    )
    assert "mcp-session-id" not in first.headers
    second = http.post(
        "/s3cret/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_endpoints", "arguments": {}}},
        headers=HEADERS,
    )
    assert second.status_code == 200
    assert second.json()["result"]["isError"] is False
