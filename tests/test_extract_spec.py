"""scripts/extract_spec.py: parsing the help centre's API pages and merging them
over the Swagger-derived spec.

No network. The page fixtures below copy the real pages' quirks, each of which
broke a first attempt at parsing them: the path line is indented with
non-breaking spaces, type cells escape their pipes (`int\\|array`), tables carry
one-cell label rows, `update` pages cut their list short, and `get` pages write
the bulk-ID param in the singular.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("extract_spec", ROOT / "scripts" / "extract_spec.py")
extract_spec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(extract_spec)

NBSP = "\xa0"


def _action_page(title: str, path: str, table: str, intro: str = "Does the thing.") -> str:
    return (
        f'---\ntitle: "{title}"\nslug: "x"\n---\n\n> ## Documentation Index\n> Fetch the index.\n\n'
        f"# {title}\n\n## 4. {title}\n\nPOST\n {NBSP} {NBSP}{path}\n\n{intro}\n\n"
        f"### Request Parameters\n\n{table}\n\n### Response\n\n"
        "```\nPOST\n    /api/fake/fromCodeBlock\n| Parameter | Type | Required | Description |\n```\n\n"
        "| Parameter | Type | Required | Description |\n| --- | --- | --- | --- |\n| `notAParam` | int | No | A response table. |\n"
    )


WIDGET_SEARCH = _action_page(
    "Search / List",
    "/api/widget/search",
    "| Parameter | Type | Required? | Default | Description |\n"
    "| --- | --- | --- | --- | --- |\n"
    "| Core Parameters |\n"
    "| `widgetIDs` | array<int> | No | -- | Filter to IDs. |\n"
    "| `officeID` | int | Yes | `1` | Office &gt; 0. |\n"
    "| `status` | int\\|array | Cond. | -- | One or many. |\n"
    "| `price` | decimal | optional | -- | Price. |\n"
    "| `includeData` | bool | No | `0` | Full rows. |\n"
    "| `chemicals[0][amount]` | int | No | -- | Not an identifier. |\n"
    "| All other parameters from **Create** are also accepted. |\n",
    intro="Returns widget IDs.\n\n**Tip**\n    Use `includeData`.",
)
WIDGET_GET_SINGULAR = _action_page(
    "Get", "/api/widget/get", "| Parameter | Type | Required | Description |\n| --- | --- | --- | --- |\n| `widgetID` | int | Yes | The ID (or IDs). |\n| `dataLink` | bool | No | Extra. |"
)
GADGET_GET = _action_page(
    "Get", "/api/gadget/get", "| Parameter | Type | Required | Description |\n| --- | --- | --- | --- |\n| `partIDs` | int\\|array | Yes | IDs. |"
)
SPROCKET_GET = _action_page(
    "Get", "/api/sprocket/get", "| Parameter | Type | Required | Description |\n| --- | --- | --- | --- |\n| `sprocketID` | int | Yes | The ID. |"
)
GUIDE = (
    "# How to\n\n### Step 1\n\nPOST\n    /api/widget/search\n\nGuide text.\n\n"
    "| Parameter | Type | Description |\n| --- | --- | --- |\n| `guideOnly` | int | From the guide. |\n\n"
    "### Step 2\n\nPOST\n    /api/documentation/swagger\n\nMeta.\n"
)
WIDGET_ENTITY = (
    "# Widgets\n\n```\nPOST /api/widget/search HTTP/1.1\n```\n\n```\nPOST /api/widget/get HTTP/1.1\n```\n\n"
    "## 3. Data Model\n\n"
    "| Field | Type | Description |\n| --- | --- | --- |\n"
    "| `widgetID` | int | Key. |\n| `name` | string | From the docs. |\n| `added` | datetime | When. |\n"
    "| `tax1` – `tax3` | decimal | Rates. |\n| `a.b` | int | Dotted, skipped. |\n\n"
    "### Conditional / Optional Fields\n\n"
    "| Field | Type | Requires Parameter | Description |\n| --- | --- | --- | --- |\n| `extras` | array<object> | `includeExtras=1` | Extras. |\n\n"
    "Each `extras` entry has the following shape:\n\n"
    "| Field | Type | Description |\n| --- | --- | --- |\n| `secondTableInSection` | int | Skipped. |\n\n"
    "### Part (nested object)\n\n| Field | Type | Description |\n| --- | --- | --- |\n| `partID` | int | Nested. |\n\n"
    "### Sub-Source Fields\n\nEach entry in the `subs` array has:\n\n| Field | Type | Description |\n| --- | --- | --- |\n| `subID` | int | Nested. |\n\n"
    "## 8. Error Handling\n\n| Field | Type | Description |\n| --- | --- | --- |\n| `notAField` | int | Past the section. |\n"
)
PAGES = {
    "widgets": WIDGET_ENTITY,
    "widgets-search-list": WIDGET_SEARCH,
    "widgets-get": WIDGET_GET_SINGULAR,
    "gadgets-get": GADGET_GET,
    "sprockets-get": SPROCKET_GET,
    "a-guide": GUIDE,
}


def _swagger_spec() -> dict:
    """A spec as the Swagger path writes it: widget with a real bulk-ID `get`."""
    return {
        "info": {"title": "FieldRoutes", "source": "old"},
        "endpoints": {
            "widget/search": {
                "entity": "widget", "action": "search", "method": "POST", "summary": "search", "description": "Old text.",
                "params": [
                    {"name": "widgetIDs", "type": "integer", "required": False, "description": ""},
                    {"name": "legacyOnly", "type": "string", "required": False, "description": "Not on the page."},
                ],
            },
            "widget/get": {
                "entity": "widget", "action": "get", "method": "POST", "summary": "getBulk", "description": "",
                "params": [{"name": "widgetIDs", "type": "array", "required": False, "description": ""}],
            },
        },
        "entities": {"widget": {"fields": {"widgetID": {"type": "integer", "description": "Swagger text"}}}},
    }


# --- parsing ---------------------------------------------------------------


def test_normalize_type_follows_the_pages_own_conversion_map() -> None:
    n = extract_spec.normalize_type
    assert [n(t) for t in ("int", "bool", "decimal", "double", "float", "number")] == ["integer", "boolean", "number", "number", "number", "number"]
    assert [n(t) for t in ("date", "datetime", "time", "string", "enum", "JSON", "text (JSON)")] == ["string"] * 7
    assert {n(t) for t in ("array<int>", "int[]", "int|array", "int | int[]", "int / array", "object[]", "array")} == {"array"}
    assert n("object") == "object" and n("file") == "file"


def test_parse_endpoints_reads_the_request_table_and_nothing_else() -> None:
    (ep,) = extract_spec.parse_endpoints("widgets-search-list", WIDGET_SEARCH)
    assert (ep["entity"], ep["action"], ep["method"]) == ("widget", "search", "POST")
    assert ep["summary"] == "Search / List"  # nearest heading, numbering stripped
    assert ep["description"] == "Returns widget IDs.\n\n**Tip** Use `includeData`."
    by_name = {p["name"]: p for p in ep["params"]}
    # No response-table row, no code-block row, no label row, no non-identifier name.
    assert list(by_name) == ["widgetIDs", "officeID", "status", "price", "includeData"]
    assert ep["dropped"] == ["chemicals[0][amount]"]
    assert [p["type"] for p in ep["params"]] == ["array", "integer", "array", "number", "boolean"]
    # Only "Yes"/"required" is required; "Cond." and "optional" are not.
    assert [p["name"] for p in ep["params"] if p["required"]] == ["officeID"]
    assert by_name["officeID"]["default"] == "1" and "default" not in by_name["widgetIDs"]
    assert by_name["officeID"]["description"] == "Office > 0."  # HTML entity unescaped
    assert ep["abbreviated"] == ["All other parameters from **Create** are also accepted."]


def test_parse_entity_fields_keeps_record_fields_and_skips_nested_shapes() -> None:
    entity, fields, skipped = extract_spec.parse_entity_fields("widgets", WIDGET_ENTITY)
    assert entity == "widget"  # from the page's own example requests
    assert list(fields) == ["widgetID", "name", "added", "tax1", "tax2", "tax3", "extras"]
    assert fields["added"]["type"] == "string" and fields["tax2"]["type"] == "number" and fields["extras"]["type"] == "array"
    assert skipped == ["widgets: Part (nested object)", "widgets: Sub-Source Fields"]
    assert extract_spec.parse_entity_fields("widgets-get", WIDGET_GET_SINGULAR) is None  # no Data Model section


def test_parse_pages_prefers_an_actions_own_page_and_skips_meta_endpoints() -> None:
    endpoints, fields, notes = extract_spec.parse_pages(PAGES)
    assert set(endpoints) == {"widget/search", "widget/get", "gadget/get", "sprocket/get"}  # no documentation/*
    assert endpoints["widget/search"]["page"] == "widgets-search-list"
    assert "guideOnly" not in {p["name"] for p in endpoints["widget/search"]["params"]}
    assert notes["duplicatePages"] == {"widget/search": ["widgets-search-list", "a-guide"]}
    assert notes["abbreviatedParamLists"] == {"widget/search": ["All other parameters from **Create** are also accepted."]}
    assert set(fields) == {"widget"}


# --- merge -----------------------------------------------------------------


def test_merge_leaves_swagger_records_alone_and_only_adds() -> None:
    previous = _swagger_spec()
    spec, findings = extract_spec.merge(previous, *extract_spec.parse_pages(PAGES)[:2])
    before, after = previous["endpoints"]["widget/search"], spec["endpoints"]["widget/search"]
    assert after["params"][: len(before["params"])] == before["params"]  # same records, same order
    assert after["description"] == "Old text." and "source" not in after
    added = after["params"][len(before["params"]):]
    assert [p["name"] for p in added] == ["officeID", "status", "price", "includeData"]
    assert all(p["source"] == "docs" and p["required"] is False for p in added)
    # The page calls officeID required and types widgetIDs as an array; neither is applied.
    assert findings["docsRequiredAddedAsOptional"] == {"widget/search": ["officeID"]}
    assert findings["docsDisagree"]["widget/search"] == [
        {"param": "widgetIDs", "spec": {"type": "integer", "required": False}, "docs": {"type": "array", "required": False}}
    ]
    assert findings["docsOmitParams"] == {"widget/search": ["legacyOnly"], "widget/get": ["widgetIDs"]}
    # Fields: the Swagger one keeps its type and text; new ones are tagged.
    fields = spec["entities"]["widget"]["fields"]
    assert fields["widgetID"] == {"type": "integer", "description": "Swagger text"}
    assert fields["name"] == {"type": "string", "description": "From the docs.", "source": "docs"}


def test_merge_keeps_the_one_array_param_rule_for_get() -> None:
    spec, findings = extract_spec.merge(_swagger_spec(), *extract_spec.parse_pages(PAGES)[:2])
    # The page's singular `widgetID` is the real `widgetIDs` respelt: not added. `dataLink` is.
    assert [p["name"] for p in spec["endpoints"]["widget/get"]["params"]] == ["widgetIDs", "dataLink"]
    assert findings["getSelectorVariantsNotAdded"] == {"widget/get": ["widgetID"]}
    assert "getIdParam" not in spec["entities"]["widget"]  # widgetIDs is the convention
    # A docs-only `get` with one array param yields a getIdParam; one with none is reported.
    assert spec["endpoints"]["gadget/get"]["source"] == "docs"
    assert spec["entities"]["gadget"] == {"fields": {}, "source": "docs", "getIdParam": "partIDs"}
    assert spec["endpoints"]["sprocket/get"]["params"][0]["name"] == "sprocketID"  # as documented, not invented
    assert "getIdParam" not in spec["entities"]["sprocket"]
    assert findings["getIdParamNotDerivable"] == ["sprocket"]
    assert findings["specEndpointsNotInDocs"] == []


def test_merge_never_adds_a_second_selector_to_a_swagger_get() -> None:
    doc_endpoints, doc_fields, _ = extract_spec.parse_pages(PAGES)
    doc_endpoints["widget/get"]["params"] = [
        {"name": "widgetIDs", "type": "integer", "required": True, "description": "Same name, looser type."},
        {"name": "gizmoIDs", "type": "array", "required": False, "description": "An alias."},
        {"name": "widgetID", "type": "integer", "required": True, "description": "The singular."},
    ]
    spec, findings = extract_spec.merge(_swagger_spec(), doc_endpoints, doc_fields)
    assert spec["endpoints"]["widget/get"]["params"] == _swagger_spec()["endpoints"]["widget/get"]["params"]
    assert findings["getSelectorVariantsNotAdded"] == {"widget/get": ["gizmoIDs", "widgetID"]}
    assert findings["docsDisagree"]["widget/get"] == [
        {"param": "widgetIDs", "spec": {"type": "array", "required": False}, "docs": {"type": "integer", "required": True}}
    ]


def test_merge_is_idempotent_and_rebuilds_the_docs_layer_each_run() -> None:
    doc_endpoints, doc_fields, _ = extract_spec.parse_pages(PAGES)
    first, _ = extract_spec.merge(_swagger_spec(), doc_endpoints, doc_fields)
    second, _ = extract_spec.merge(first, doc_endpoints, doc_fields)
    assert second == first
    # Stripping the docs layer gives back exactly the Swagger records.
    assert extract_spec.swagger_layer(first)["endpoints"] == _swagger_spec()["endpoints"]
    assert extract_spec.swagger_layer(first)["entities"] == _swagger_spec()["entities"]
    # When the pages drop something they once documented, it leaves the spec too.
    fewer = {k: v for k, v in doc_endpoints.items() if k != "gadget/get"}
    fewer["widget/search"] = {**fewer["widget/search"], "params": fewer["widget/search"]["params"][:2]}
    third, _ = extract_spec.merge(first, fewer, {})
    assert "gadget/get" not in third["endpoints"] and "gadget" not in third["entities"]
    assert [p["name"] for p in third["endpoints"]["widget/search"]["params"]] == ["widgetIDs", "legacyOnly", "officeID"]
    assert list(third["entities"]["widget"]["fields"]) == ["widgetID"]
    diff = extract_spec.diff_specs(first, third)
    assert diff["endpointsRemoved"] == ["gadget/get"] and diff["entitiesRemoved"] == ["gadget"]
    assert diff["paramsRemoved"]["widget/search"] == ["status", "price", "includeData"]


# --- the Swagger path, kept for a saved /api/documentation/swagger response --


SWAGGER = {
    "info": {"title": "FieldRoutes"},
    "paths": {
        "/widget/search": {"post": {"summary": "search", "parameters": [
            {"name": "widgetIDs", "in": "query", "type": "integer"},
            {"name": 0, "in": "query", "type": "o"},
            {"name": "body", "in": "body", "type": "object"},
        ]}},
        "/widget/get": {"post": {"summary": "getBulk", "parameters": [{"name": "itemIDs", "in": "query", "type": "array"}]}},
        "/widget/[id]": {"get": {"summary": "legacy"}},
        "/documentation/swagger": {"post": {"summary": "meta"}},
    },
    "definitions": {"widget": {"type": "object", "properties": {"widgetID": {"type": "integer", "description": "Key"}}}},
}


@pytest.mark.parametrize("wrap", ["{}", "window.swagger={};"])
def test_swagger_file_rebuild_keeps_its_safeguards(tmp_path: Path, wrap: str) -> None:
    path = tmp_path / "swagger.json"
    path.write_text(wrap.replace("{}", json.dumps(SWAGGER)))
    spec = extract_spec.spec_from_swagger(extract_spec.load_swagger(str(path)), "test")
    assert set(spec["endpoints"]) == {"widget/search", "widget/get"}  # no [id] path, no meta endpoint
    assert [p["name"] for p in spec["endpoints"]["widget/search"]["params"]] == ["widgetIDs"]  # name 0 dropped
    assert spec["entities"]["widget"] == {"fields": {"widgetID": {"type": "integer", "description": "Key"}}, "getIdParam": "itemIDs"}


def test_load_swagger_rejects_a_file_that_is_not_a_swagger_document(tmp_path: Path) -> None:
    path = tmp_path / "error.json"
    path.write_text('{"success": false, "errorMessage": "Invalid authorization keys!"}')
    with pytest.raises(SystemExit, match="not a Swagger 2.0 document"):
        extract_spec.load_swagger(str(path))


# --- the shipped spec --------------------------------------------------------


def test_shipped_spec_still_holds_the_whole_swagger_layer() -> None:
    # The last real Swagger gave 187 endpoints, 56 entities, 1489 params and 1182
    # fields. Everything else in the file is tagged "source": "docs"; strip that
    # and those four numbers must come back, or a regeneration lost real records.
    shipped = json.loads((ROOT / "src" / "fr_mcp" / "fieldroutes_spec.json").read_text())
    layer = extract_spec.swagger_layer(shipped)
    assert len(layer["endpoints"]) == 187 and len(layer["entities"]) == 56
    assert sum(len(ep["params"]) for ep in layer["endpoints"].values()) == 1489
    assert sum(len(e["fields"]) for e in layer["entities"].values()) == 1182
    assert len(extract_spec.build_get_id_params(layer["endpoints"])) == 56
