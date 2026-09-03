"""Security invariants: no tool output, error message, or log line can ever
carry the FieldRoutes credentials or the MCP secret path.

This is a sweep, not a single assertion: every curated tool (all 30, reads
and writes) is called once against the fake server and its JSON output is
checked for the credential field names FieldRoutes' `params` echo uses
(`authenticationKey`/`authenticationToken`) as substrings, and for the
literal credential *values* as exact JSON leaf values (a stricter, renamed-
field-proof check) -- because the leak we're guarding against is exactly
"some response passed the raw envelope through instead of the trimmed
output", and that can happen to any one tool, not just the ones already
covered elsewhere.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from conftest import FakeFR, loads

from fr_mcp import server  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

FORBIDDEN_SUBSTRINGS = ("authenticationKey", "authenticationToken", "SCRUBBED")


def _walk_leaf_strings(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _walk_leaf_strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _walk_leaf_strings(v)]
    if isinstance(obj, str):
        return [obj]
    return []


def _assert_no_leak(label: str, raw_output: str) -> None:
    for bad in FORBIDDEN_SUBSTRINGS:
        assert bad not in raw_output, f"{label}: output contains {bad!r}"
    leaves = _walk_leaf_strings(json.loads(raw_output))
    assert "key" not in leaves, f"{label}: a JSON string value is exactly the credential 'key'"
    assert "token" not in leaves, f"{label}: a JSON string value is exactly the credential 'token'"


# Every curated tool, called once with valid args against the seeded fake data
# (customer 1/2/3, subscription 100, appointment 500/501/502, route 20, note 800).
READ_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("list_endpoints", {}),
    ("describe_endpoint", {"endpoint": "customer/search"}),
    ("search", {"entity": "customer", "filters": {"lname": "Smith"}}),
    ("get", {"entity": "customer", "ids": [1]}),
    ("health_check", {}),
    ("find_customer", {"query": "Jane Smith"}),
    ("customer_360", {"customer_id": 1}),
    ("customer_alerts", {"customer_id": 1}),
    ("day_schedule", {}),
    ("property_assignments", {"customer_id": 1}),
    ("route_stops", {"route_id": 20}),
    ("crew_schedule", {}),
    ("due_for_service", {}),
    ("open_slots", {}),
    ("ar_aging", {}),
    ("lookups", {}),
    ("subscription_details", {"subscription_id": 100}),
    ("list_notes", {"customer_id": 1}),
    ("service_schedule", {}),
]

WRITE_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("add_note", {"customer_id": 1, "text": "Left gate open."}),
    ("update_note", {"note_id": 800, "text": "Updated."}),
    ("set_red_notes", {"customer_id": 1, "text": "Beware of dog."}),
    ("create_task", {"customer_id": 1, "text": "Call back."}),
    ("schedule_appointment", {"customer_id": 1, "service_type_id": 3, "spot_id": 32}),
    ("reschedule_appointment", {"appointment_id": 500, "spot_id": 32}),
    ("update_appointment_notes", {"appointment_id": 500, "notes": "Aerate front."}),
    ("cancel_appointment", {"appointment_id": 500}),
    ("complete_appointment", {"appointment_id": 500}),
    ("reserve_slot", {"spot_id": 32}),
    ("update_subscription", {"subscription_id": 100, "frequency": 30}),
    ("service_schedule", {"move": [{"appointment_id": 500, "spot_id": 32}]}),
    ("call", {"entity": "note", "action": "create", "params": {"customerID": 1, "notes": "x", "contactType": 5}}),
]


@pytest.mark.parametrize("tool_name,kwargs", READ_CALLS, ids=[c[0] for c in READ_CALLS])
async def test_no_credential_leak_in_read_tool_output(
    fake: FakeFR, tool_name: str, kwargs: dict[str, Any]
) -> None:
    fn = getattr(server, tool_name)
    out = await fn(**kwargs)
    _assert_no_leak(tool_name, out)


@pytest.mark.parametrize("tool_name,kwargs", WRITE_CALLS, ids=[c[0] for c in WRITE_CALLS])
async def test_no_credential_leak_in_write_tool_output(
    fake: FakeFR, tool_name: str, kwargs: dict[str, Any]
) -> None:
    fn = getattr(server, tool_name)
    out = await fn(**kwargs)
    _assert_no_leak(tool_name, out)


async def test_no_credential_leak_when_a_tool_raises(fake: FakeFR) -> None:
    # The error path is exactly where a raw envelope is most tempting to pass
    # through unfiltered -- confirm ToolError's own message is clean too.
    fake.fail_with = "Permission denied: editRedNotes"
    with pytest.raises(ToolError) as exc_info:
        await server.set_red_notes(1, "x")
    _assert_no_leak("set_red_notes error", json.dumps(str(exc_info.value)))


async def test_secret_path_not_in_any_tool_output(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    # Covers health_check too (it's in READ_CALLS) -- the secret path segment
    # should never be readable through any tool, only through Railway's own
    # environment variable configuration.
    monkeypatch.setenv("MCP_PATH_SECRET", "UNIQUE_PATH_SECRET_VALUE_zz99")
    for name, kwargs in READ_CALLS:
        out = await getattr(server, name)(**kwargs)
        assert "UNIQUE_PATH_SECRET_VALUE_zz99" not in out, f"{name} leaked MCP_PATH_SECRET"
