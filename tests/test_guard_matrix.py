"""Exhaustive write-guard matrix: every write tool, checked against every guard.

`test_wire_format.py` already checks "writes off blocks every write" and "the
allowlist blocks/allows" with hand-written loops over the write tools -- which
means a new write tool added to server.py without also being added to those
loops would silently ship *unguarded* and the suite would stay green. This
file closes that gap two ways:

1. A coverage test that introspects the live registered tool set and fails
   the moment it no longer matches this file's READ/WRITE/GENERIC
   classification -- forcing whoever adds a tool to classify it here.
2. A single parametrized matrix (write tool x guard condition) built from
   that classification, so every write tool is automatically checked against
   every guard with no per-tool test to remember to add.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeFR
from fr_mcp import server
from mcp.server.mcpserver.exceptions import ToolError
from test_security import WRITE_CALLS  # each entry targets customer 1's data

GENERIC_TOOL_NAMES = {"list_endpoints", "describe_endpoint", "search", "get", "call", "health_check"}
READ_TOOL_NAMES = {
    "find_customer", "customer_360", "customer_alerts", "day_schedule", "property_assignments",
    "route_stops", "crew_schedule", "due_for_service", "open_slots", "ar_aging", "lookups",
    "subscription_details", "list_notes",
}
WRITE_TOOL_NAMES = {
    "add_note", "update_note", "set_red_notes", "create_task", "schedule_appointment",
    "reschedule_appointment", "update_appointment_notes", "cancel_appointment",
    "complete_appointment", "reserve_slot", "update_subscription",
}

# reserve_slot is the one deliberate allowlist exemption (see server.py: a spot
# hold names no customer and isn't customer data) -- excluded from the
# allowlist-blocks/allows matrix below, which has its own dedicated test for it.
ALLOWLIST_GATED_WRITE_CALLS = [(name, kwargs) for name, kwargs in WRITE_CALLS if name != "reserve_slot"]


async def test_tool_inventory_is_exhaustively_classified() -> None:
    # If this fails, a tool was added or removed in server.py without this
    # file's classification being updated to match -- fix the sets above,
    # not this test.
    live_names = {t.name for t in await server.mcp.list_tools()}
    classified = GENERIC_TOOL_NAMES | READ_TOOL_NAMES | WRITE_TOOL_NAMES
    assert live_names == classified
    assert len(live_names) == 30


def test_write_calls_fixture_covers_every_write_tool() -> None:
    # Every write tool must appear in the shared WRITE_CALLS fixture (imported
    # from test_security.py) or the parametrized matrix below silently skips it.
    covered = {name for name, _ in WRITE_CALLS}
    assert covered == WRITE_TOOL_NAMES | {"call"}


@pytest.mark.parametrize("tool_name,kwargs", WRITE_CALLS, ids=[c[0] for c in WRITE_CALLS])
async def test_writes_off_blocks_this_tool(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch, tool_name: str, kwargs: dict
) -> None:
    monkeypatch.setenv("FR_WRITES", "off")
    with pytest.raises(ToolError, match="writes are disabled"):
        await getattr(server, tool_name)(**kwargs)
    assert not [r for r in fake.requests if r.action not in ("search", "get")]


@pytest.mark.parametrize(
    "tool_name,kwargs", ALLOWLIST_GATED_WRITE_CALLS, ids=[c[0] for c in ALLOWLIST_GATED_WRITE_CALLS]
)
async def test_allowlist_blocks_this_tool_for_a_different_customer(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch, tool_name: str, kwargs: dict
) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "2")  # every WRITE_CALLS entry targets customer 1
    with pytest.raises(ToolError, match="FR_WRITE_CUSTOMER_IDS"):
        await getattr(server, tool_name)(**kwargs)
    assert not [r for r in fake.requests if r.action not in ("search", "get")]


@pytest.mark.parametrize(
    "tool_name,kwargs", ALLOWLIST_GATED_WRITE_CALLS, ids=[c[0] for c in ALLOWLIST_GATED_WRITE_CALLS]
)
async def test_allowlist_allows_this_tool_for_the_matching_customer(
    fake: FakeFR, monkeypatch: pytest.MonkeyPatch, tool_name: str, kwargs: dict
) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "1")
    out = await getattr(server, tool_name)(**kwargs)
    assert json.loads(out).get("success", True) is not False


async def test_reserve_slot_is_exempt_from_the_allowlist(fake: FakeFR, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_WRITE_CUSTOMER_IDS", "999")  # no customer this could plausibly resolve to
    out = json.loads(await server.reserve_slot(spot_id=32))
    assert out["reservation"] == "tok-123"
