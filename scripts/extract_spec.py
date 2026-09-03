#!/usr/bin/env python3
"""Regenerate src/fr_mcp/fieldroutes_spec.json from FieldRoutes' public Swagger spec.

FieldRoutes publishes its full API surface as a JS file that assigns a JSON
literal to `window.swagger`:

    https://api.fieldroutes.com/resources/swagger.js

This script downloads it, strips the `window.swagger=...;` wrapper, and slims
it down to what the MCP server actually needs:

- One entry per `{entity}/{action}` endpoint (187 of them) with its HTTP
  method, summary, description, and parameter list (name/type/required/
  description/default). The spec also contains a legacy `/{entity}/[id]`
  path per entity that duplicates `{entity}/get`; those are dropped.
- One field list per entity (the plain-named definitions, e.g. `customer`,
  `appointment` -- as opposed to `customerSearchResponse` and friends, which
  are per-endpoint response wrappers and aren't useful here).

Run it whenever FieldRoutes changes their API:

    python scripts/extract_spec.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

SWAGGER_URL = "https://api.fieldroutes.com/resources/swagger.js"
OUT_PATH = Path(__file__).resolve().parent.parent / "src" / "fr_mcp" / "fieldroutes_spec.json"


def fetch_swagger(source: str | None) -> dict:
    if source:
        text = Path(source).read_text()
    else:
        with urllib.request.urlopen(SWAGGER_URL, timeout=30) as resp:
            text = resp.read().decode("utf-8")
    text = text.strip()
    prefix = "window.swagger="
    if not text.startswith(prefix):
        raise SystemExit(f"Unexpected swagger.js format: does not start with {prefix!r}")
    json_text = text[len(prefix):].strip()
    if json_text.endswith(";"):
        json_text = json_text[:-1]
    return json.loads(json_text)


def slim_param(p: dict) -> dict | None:
    """Return `None` for params too malformed to use as a tool argument.

    FieldRoutes' own swagger has at least one real defect: `compassCustomer/search`
    carries three params with `name: 0`, `name: 1`, `name: 2` (types `"o"`,
    `"c"`, `"d"` -- evidently a string that got exploded character-by-character
    somewhere in their spec generator, probably meant to be one param named
    `ocd`). Skip anything whose name isn't a usable Python identifier rather
    than let it crash tool registration; the entity is still reachable through
    the generic `call`/`search` tools with a raw param dict.
    """
    name = p.get("name")
    if not isinstance(name, str) or not name.isidentifier():
        return None
    out = {
        "name": p["name"],
        "type": p.get("type", "string"),
        "required": bool(p.get("required", False)),
        "description": p.get("description", ""),
    }
    if "default" in p:
        out["default"] = p["default"]
    return out


def build_endpoints(spec: dict) -> dict:
    endpoints = {}
    for path, methods in spec["paths"].items():
        # Drop the legacy `/{entity}/[id]` GET-by-id paths; they duplicate
        # `{entity}/get` and aren't part of the entity/action calling
        # convention the client and generic `call` tool use.
        if "[" in path:
            continue
        parts = path.strip("/").split("/")
        if len(parts) != 2:
            continue
        entity, action = parts
        for method, op in methods.items():
            key = f"{entity}/{action}"
            endpoints[key] = {
                "entity": entity,
                "action": action,
                "method": method.upper(),
                "summary": op.get("summary", action),
                "description": op.get("description", ""),
                "params": [
                    sp
                    for p in op.get("parameters", [])
                    if p.get("in") in ("query", "formData") and (sp := slim_param(p)) is not None
                ],
            }
    return endpoints


def build_get_id_params(endpoints: dict) -> dict:
    """The param name each entity's `get` takes for its bulk-ID list.

    Most endpoints follow `{entity}IDs` (e.g. `customerIDs` for `customer/get`),
    but a substantial minority don't -- `serviceType/get` takes `typeIDs`,
    `customerFlag/get` takes `customerIDs` (it has no ID of its own),
    `cancellationReason/get` takes `reasonIDs`, and so on. Every `get`
    endpoint has exactly one array-typed param; that's always the right one,
    confirmed against the live spec (see extract_spec.py's own checks).
    """
    out = {}
    for ep in endpoints.values():
        if ep["action"] != "get":
            continue
        array_params = [p["name"] for p in ep["params"] if p["type"] == "array"]
        if len(array_params) == 1:
            out[ep["entity"]] = array_params[0]
    return out


def build_entities(spec: dict, endpoints: dict) -> dict:
    entities = {}
    endpoint_entities = set()
    for path in spec["paths"]:
        if "[" in path:
            continue
        parts = path.strip("/").split("/")
        if len(parts) == 2:
            endpoint_entities.add(parts[0])

    defs = spec["definitions"]
    id_params = build_get_id_params(endpoints)
    for name in endpoint_entities:
        definition = defs.get(name)
        if not definition or definition.get("type") != "object":
            continue
        fields = {}
        for fname, fdef in definition.get("properties", {}).items():
            fields[fname] = {
                "type": fdef.get("type", "object"),
                "description": fdef.get("description", ""),
            }
        entry = {"fields": fields}
        get_id_param = id_params.get(name)
        if get_id_param and get_id_param != f"{name}IDs":
            entry["getIdParam"] = get_id_param
        entities[name] = entry
    return entities


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else None
    spec = fetch_swagger(source)

    endpoints = build_endpoints(spec)
    entities = build_entities(spec, endpoints)

    out = {
        "info": {
            "title": spec.get("info", {}).get("title", "FieldRoutes"),
            "source": SWAGGER_URL,
        },
        "endpoints": endpoints,
        "entities": entities,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {len(endpoints)} endpoints and {len(entities)} entities to {OUT_PATH}")


if __name__ == "__main__":
    main()
