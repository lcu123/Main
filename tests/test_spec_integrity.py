"""Spec integrity: the shipped fieldroutes_spec.json is internally consistent,
and every entity's tools can actually be generated from it.

These exist because the spec is regenerated from FieldRoutes' own swagger.js,
which has real defects (see `scripts/extract_spec.py`'s `slim_param` and
CLAUDE.md) -- a future re-extraction could silently reintroduce garbage that
would only surface at runtime, e.g. under `FR_EXPOSE_ALL=1` in production. A
test here catches it before it ships.
"""

from __future__ import annotations

import json
import os

import pytest

os.environ.setdefault("FR_SUBDOMAIN", "zest")
os.environ.setdefault("FR_AUTH_KEY", "key")
os.environ.setdefault("FR_AUTH_TOKEN", "token")

from mcp.server.mcpserver import MCPServer  # noqa: E402

from conftest import FakeFR, _client_for  # noqa: E402
from fr_mcp import generated, server  # noqa: E402

SPEC = server.SPEC


def test_exactly_187_endpoints_and_56_entities() -> None:
    assert len(SPEC["endpoints"]) == 187
    assert len(SPEC["entities"]) == 56


def test_every_endpoint_key_matches_its_entity_action() -> None:
    for key, ep in SPEC["endpoints"].items():
        assert key == f"{ep['entity']}/{ep['action']}"


def test_every_param_name_is_a_valid_python_identifier() -> None:
    # The real defect this guards: FieldRoutes' own swagger had compassCustomer/search
    # params named 0, 1, 2 (a string exploded character-by-character by their spec
    # generator). extract_spec.py's slim_param drops these; this asserts none survived.
    bad = [
        (key, p["name"])
        for key, ep in SPEC["endpoints"].items()
        for p in ep["params"]
        if not isinstance(p["name"], str) or not p["name"].isidentifier()
    ]
    assert bad == []


def test_every_get_endpoint_has_exactly_one_array_param() -> None:
    # This is the invariant extract_spec.py's build_get_id_params relies on to
    # derive getIdParam. If a future FieldRoutes API change breaks it for some
    # entity, that entity's bulk fetch would silently target the wrong param.
    for key, ep in SPEC["endpoints"].items():
        if ep["action"] != "get":
            continue
        array_params = [p["name"] for p in ep["params"] if p["type"] == "array"]
        assert len(array_params) == 1, f"{key}: expected exactly one array param, got {array_params}"


def test_get_id_param_overrides_are_used_not_just_recorded() -> None:
    # 24 entities are known to deviate from the {entity}IDs convention (see
    # CLAUDE.md). Confirm server._id_param() actually returns the override for
    # each one, not just that extract_spec.py recorded it in the JSON.
    overridden = {name: e["getIdParam"] for name, e in SPEC["entities"].items() if "getIdParam" in e}
    assert len(overridden) == 24
    for entity, expected_param in overridden.items():
        assert server._id_param(entity) == expected_param
    # And entities with no override fall back to the {entity}IDs convention.
    assert server._id_param("customer") == "customerIDs"
    assert server._id_param("appointment") == "appointmentIDs"


async def test_describe_endpoint_works_for_every_endpoint() -> None:
    for key in SPEC["endpoints"]:
        entity, action = key.split("/")
        out = json.loads(await server.describe_endpoint(key))
        assert out["endpoint"] == key
        assert isinstance(out["params"], list)
        if generated.is_read_action(action):
            assert "fields" in out


async def test_list_endpoints_covers_every_entity() -> None:
    out = json.loads(await server.list_endpoints())
    assert out["count"] == 187
    for key, ep in SPEC["endpoints"].items():
        assert ep["action"] in out["entities"][ep["entity"]]


def test_generating_all_56_entities_registers_without_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_EXPOSE_ALL", "1")
    mcp = MCPServer("spec-integrity-probe")
    names = generated.register_generated_tools(
        mcp,
        _client_for(FakeFR()),
        SPEC,
        writes_enabled=lambda: True,
        allow_delete=lambda: False,
        allow_charges=lambda: False,
    )
    assert len(names) == 187
    assert len(set(names)) == 187, "no duplicate tool names"
    for name in names:
        assert name.startswith("fr_")


async def test_generated_tool_required_fields_match_spec_required_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FR_EXPOSE_ALL", "1")
    mcp = MCPServer("spec-integrity-probe-2")
    generated.register_generated_tools(
        mcp,
        _client_for(FakeFR()),
        SPEC,
        writes_enabled=lambda: True,
        allow_delete=lambda: False,
        allow_charges=lambda: False,
    )
    tools = {t.name: t for t in await mcp.list_tools()}
    mismatches = []
    for key, ep in SPEC["endpoints"].items():
        tool = tools[f"fr_{key.replace('/', '_')}"]
        expected_required = {p["name"] for p in ep["params"] if p.get("required")}
        actual_required = set(tool.input_schema.get("required") or [])
        if expected_required != actual_required:
            mismatches.append((key, expected_required, actual_required))
    assert mismatches == []
