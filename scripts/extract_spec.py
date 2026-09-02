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


def slim_param(p: dict) -> dict:
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
                "params": [slim_param(p) for p in op.get("parameters", []) if p.get("in") in ("query", "formData")],
            }
    return endpoints


def build_entities(spec: dict) -> dict:
    entities = {}
    endpoint_entities = set()
    for path in spec["paths"]:
        if "[" in path:
            continue
        parts = path.strip("/").split("/")
        if len(parts) == 2:
            endpoint_entities.add(parts[0])

    defs = spec["definitions"]
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
        entities[name] = {"fields": fields}
    return entities


def main() -> None:
    source = sys.argv[1] if len(sys.argv) > 1 else None
    spec = fetch_swagger(source)

    endpoints = build_endpoints(spec)
    entities = build_entities(spec)

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
