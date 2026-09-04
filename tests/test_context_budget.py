"""Context budget: the curated tool set's schema size stays bounded, and every
tool is well-formed enough for Claude to actually use correctly.

Measured directly rather than assumed: 30 curated tools currently serialize to
about 23k characters of JSON schema (name + description + inputSchema), which
at a rough 4-chars-per-token estimate is close to 5.8k tokens -- notably more
than earlier documentation guessed, because pydantic's schema derivation
wraps every optional `X | None = None` parameter in an `anyOf`/`title`
structure that costs more than a bare `"type": "..."` would. The ceiling here
has real headroom above that measured value; it exists to catch someone
turning FR_EXPOSE_ALL on by default or letting docstrings balloon, not to
pin the exact byte count.
"""

from __future__ import annotations

import json
import os

os.environ.setdefault("FR_SUBDOMAIN", "zest")
os.environ.setdefault("FR_AUTH_KEY", "key")
os.environ.setdefault("FR_AUTH_TOKEN", "token")

from fr_mcp import server  # noqa: E402

# Generous headroom above the ~23k chars / ~5.8k tokens measured for the
# current 30 curated tools -- catches a real regression, not day-to-day drift.
MAX_SCHEMA_CHARS = 35_000


async def test_curated_tool_count_is_31() -> None:
    tools = await server.mcp.list_tools()
    assert len(tools) == 31


async def test_curated_tool_schema_size_stays_within_budget() -> None:
    tools = await server.mcp.list_tools()
    total_chars = sum(
        len(json.dumps({"name": t.name, "description": t.description, "inputSchema": t.input_schema}))
        for t in tools
    )
    assert total_chars < MAX_SCHEMA_CHARS, (
        f"curated tool schemas are {total_chars} chars (~{total_chars // 4} tokens), "
        f"over the {MAX_SCHEMA_CHARS}-char budget -- a tool got a lot chattier, or "
        f"generated tools are leaking into the default (unexposed) set"
    )


async def test_every_curated_tool_has_a_nonempty_description() -> None:
    tools = await server.mcp.list_tools()
    empty = [t.name for t in tools if not (t.description or "").strip()]
    assert empty == [], f"tools with no docstring (Claude can't tell what they do): {empty}"


async def test_every_curated_tool_parameter_has_type_information() -> None:
    tools = await server.mcp.list_tools()
    untyped = []
    for t in tools:
        for pname, pschema in (t.input_schema.get("properties") or {}).items():
            if not any(k in pschema for k in ("type", "anyOf", "$ref", "enum")):
                untyped.append((t.name, pname))
    assert untyped == [], f"parameters with no usable type info in their schema: {untyped}"


async def test_generated_tools_are_off_by_default_so_they_never_hit_the_budget() -> None:
    # The 187-endpoint generated set (~70k tokens per CLAUDE.md) only exists
    # when FR_EXPOSE_ALL/FR_EXPOSE_ENTITIES is explicitly set -- confirming
    # that here is what makes the 35k-char ceiling above meaningful at all.
    assert server._generated_tools == []
