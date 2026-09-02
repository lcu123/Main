"""Optional: one typed MCP tool per FieldRoutes endpoint, built from the spec at startup.

Off by default. The 25 curated tools in `server.py` cost roughly 2k tokens of
context; exposing all 187 generated ones (`fr_{entity}_{action}`) costs
roughly 70k. Turn generation on with:

- `FR_EXPOSE_ALL=1` -- every entity.
- `FR_EXPOSE_ENTITIES=customer,appointment,task` -- just those entities.
- `FR_EXPOSE_VERBOSE=1` -- keep the full endpoint description in the tool's
  docstring instead of a one-line summary (more context per tool).

Generated tools share the same write guards as the generic `call` tool in
`server.py`: writes need `FR_WRITES=on`, `*/delete` needs `FR_ALLOW_DELETE=1`,
and `payment/create` with `doCharge=1` needs `FR_ALLOW_CHARGES=1`.
"""

from __future__ import annotations

import inspect
import json
import os
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from .client import FieldRoutesClient, FieldRoutesError

_JSON_TYPES: dict[str, type] = {
    "integer": int,
    "int": int,
    "number": float,
    "string": str,
    "boolean": bool,
    "bool": bool,
    "array": list,
}


def _annotation_for(param: dict[str, Any]) -> Any:
    py_type = _JSON_TYPES.get(param.get("type", "string"), str)
    return py_type | None


def is_read_action(action: str) -> bool:
    """`search`, `get`, `getAddOns`, `summary`... read; everything else writes."""
    return action in ("search", "summary") or action.startswith("get")


def selected_entities(spec: dict[str, Any]) -> set[str]:
    """Entities to generate tools for. Empty set = generation stays off."""
    if os.environ.get("FR_EXPOSE_ALL", "").strip().lower() in ("1", "true", "yes"):
        return set(spec["entities"].keys())
    raw = os.environ.get("FR_EXPOSE_ENTITIES", "").strip()
    if not raw:
        return set()
    return {e.strip() for e in raw.split(",") if e.strip()}


def register_generated_tools(
    mcp: MCPServer,
    client: FieldRoutesClient,
    spec: dict[str, Any],
    *,
    writes_enabled: Callable[[], bool],
    allow_delete: Callable[[], bool],
    allow_charges: Callable[[], bool],
) -> list[str]:
    """Register `fr_{entity}_{action}` tools for the selected entities.

    Returns the list of registered tool names (empty if generation is off).
    """
    entities = selected_entities(spec)
    if not entities:
        return []
    verbose = os.environ.get("FR_EXPOSE_VERBOSE", "").strip().lower() in ("1", "true", "yes")

    registered: list[str] = []
    for endpoint in spec["endpoints"].values():
        entity, action = endpoint["entity"], endpoint["action"]
        if entity not in entities:
            continue
        name = _register_one(
            mcp,
            client,
            endpoint,
            verbose=verbose,
            writes_enabled=writes_enabled,
            allow_delete=allow_delete,
            allow_charges=allow_charges,
        )
        registered.append(name)
    return registered


def _register_one(
    mcp: MCPServer,
    client: FieldRoutesClient,
    endpoint: dict[str, Any],
    *,
    verbose: bool,
    writes_enabled: Callable[[], bool],
    allow_delete: Callable[[], bool],
    allow_charges: Callable[[], bool],
) -> str:
    entity, action = endpoint["entity"], endpoint["action"]
    tool_name = f"fr_{entity}_{action}"
    is_write = not is_read_action(action)
    is_delete = action == "delete"
    is_charge = entity == "payment" and action == "create"

    doc = f"FieldRoutes {entity}/{action}: {endpoint.get('summary') or action}."
    if verbose and endpoint.get("description"):
        doc += " " + endpoint["description"]
    if is_write:
        doc += " Write action, disabled unless FR_WRITES=on."
    if is_delete:
        doc += " Delete action, requires FR_ALLOW_DELETE=1."
    if is_charge:
        doc += " Can charge a card when doCharge=1, which requires FR_ALLOW_CHARGES=1 -- confirm with the user before sending doCharge=1."

    param_specs = endpoint["params"]
    param_names = {p["name"] for p in param_specs}

    async def _impl(**kwargs: Any) -> str:
        if is_write and not writes_enabled():
            raise ToolError(f"{tool_name}: writes are disabled (set FR_WRITES=on to enable).")
        if is_delete and not allow_delete():
            raise ToolError(f"{tool_name}: delete actions are disabled (set FR_ALLOW_DELETE=1 to enable).")
        if is_charge and str(kwargs.get("doCharge")) in ("1", "true", "True") and not allow_charges():
            raise ToolError(f"{tool_name}: real card charges are disabled (set FR_ALLOW_CHARGES=1 to enable).")
        params = {k: v for k, v in kwargs.items() if k in param_names and v is not None}
        try:
            data = await client.call(entity, action, params)
        except FieldRoutesError as exc:
            raise ToolError(str(exc)) from exc
        return json.dumps(data, default=str)

    parameters = [
        inspect.Parameter(
            p["name"],
            kind=inspect.Parameter.KEYWORD_ONLY,
            default=inspect.Parameter.empty if p.get("required") else None,
            annotation=(_JSON_TYPES.get(p.get("type", "string"), str) if p.get("required") else _annotation_for(p)),
        )
        for p in param_specs
    ]
    _impl.__signature__ = inspect.Signature(parameters)
    _impl.__annotations__ = {p.name: p.annotation for p in parameters}
    _impl.__name__ = tool_name
    _impl.__doc__ = doc

    mcp.add_tool(_impl, name=tool_name, description=doc)
    return tool_name
