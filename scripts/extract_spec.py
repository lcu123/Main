#!/usr/bin/env python3
"""Regenerate src/fr_mcp/fieldroutes_spec.json.

Where the spec comes from now
-----------------------------
Until late 2026 FieldRoutes published its whole API as a public Swagger file:

    https://api.fieldroutes.com/resources/swagger.js

That address now answers `301` to the help centre, and no public copy of the
Swagger exists any more. Two sources are left:

1. The help centre's API reference (the default here). It blocks search
   engines but publishes an index at `https://knowledgebase.fieldroutes.com/llms.txt`
   and a plain-text twin of every page (`<page>.md`). One page per action
   holds a parameter table; one page per entity holds a "Data Model" table.

2. The live Swagger, behind the office's own API key:
   `POST https://{subdomain}.fieldroutes.com/api/documentation/swagger`.
   This script never calls it. If you save its response to a file yourself,
   `--swagger FILE` rebuilds the spec from it (the old, exact code path).

Why the help-centre pages are merged, not trusted wholesale
-----------------------------------------------------------
The pages are prose written for people, and they are looser than the Swagger
they replaced. Measured against the last real Swagger (187 endpoints):

- 28 of the 56 `get` pages do not show the bulk-ID param as an array: 22
  write it in the singular (`noteID`, int, where the API takes `noteIDs`;
  one even "fixes" FieldRoutes' own misspelt `applciationMethodIDs`) and 6
  keep the name but type it `int`. Read literally, they would break the
  `getIdParam` derivation for half the entities.
- `update` pages are cut short ("all other parameters from Create are also
  accepted"): `customer/update` lists 1 of its 72 params.
- Search filters are typed `array<int>` where the Swagger said `integer`.

FieldRoutes' own migration note says the move was purely additive: no
endpoint, param or field was removed or changed. So this script keeps every
record that came from the real Swagger exactly as it is, and uses the pages
only to *add* what the old Swagger did not have: new endpoints, new params,
new entities, new fields. Everything added that way carries
`"source": "docs"`, so it can be told apart (and is rebuilt from the pages on
each run, so a later docs fix flows through). Where the pages disagree with a
Swagger record, the Swagger record wins and the disagreement goes in the
report.

Safeguards kept from the Swagger-only version
---------------------------------------------
- `slim_param` drops any param whose name is not a valid Python identifier.
- `getIdParam` is derived from the one array-typed param of each `get`. A
  `get` page that documents no array param leaves it underived (the server
  then falls back to `{entity}IDs`); those entities are listed in the report.
- The run aborts if any Swagger-derived `getIdParam` would change.

Usage
-----
    python scripts/extract_spec.py                    # help centre -> merge into the spec
    python scripts/extract_spec.py --save-pages DIR   # ...and keep the pages it fetched
    python scripts/extract_spec.py --pages-dir DIR    # same merge from saved pages, no network
    python scripts/extract_spec.py --swagger FILE     # rebuild from a saved Swagger 2.0 file
    python scripts/extract_spec.py --report FILE      # also write the full change report (JSON)
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

KB_INDEX_URL = "https://knowledgebase.fieldroutes.com/llms.txt"
KB_PAGE_RE = re.compile(r"https://knowledgebase\.fieldroutes\.com/apidocs/([A-Za-z0-9._-]+)\.md")
LEGACY_SWAGGER_URL = "https://api.fieldroutes.com/resources/swagger.js"
OUT_PATH = Path(__file__).resolve().parent.parent / "src" / "fr_mcp" / "fieldroutes_spec.json"

DOCS = "docs"  # value of the "source" tag on everything taken from the help centre

# `documentation/swagger` and `documentation/usage` are meta endpoints, not
# record endpoints: the first returns the entire Swagger, and neither is a
# `search`/`get*` action, so the server would count both as writes. They were
# never in the public Swagger either. Left out on purpose.
SKIP_ENTITIES = {"documentation"}


# =====================================================================
# Shared safeguards (used by both sources)
# =====================================================================


def slim_param(p: dict) -> dict | None:
    """Return `None` for params too malformed to use as a tool argument.

    FieldRoutes' own swagger has at least one real defect: `compassCustomer/search`
    carries three params with `name: 0`, `name: 1`, `name: 2` (types `"o"`,
    `"c"`, `"d"` -- evidently a string that got exploded character-by-character
    somewhere in their spec generator, probably meant to be one param named
    `ocd`). Skip anything whose name isn't a usable Python identifier rather
    than let it crash tool registration; the entity is still reachable through
    the generic `call`/`search` tools with a raw param dict.

    The help-centre pages get the same treatment: a table row whose first cell
    is a label, a dotted path or a `chemicals[0][amount]`-style key is dropped.
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


def build_get_id_params(endpoints: dict) -> dict:
    """The param name each entity's `get` takes for its bulk-ID list.

    Most endpoints follow `{entity}IDs` (e.g. `customerIDs` for `customer/get`),
    but a substantial minority don't -- `serviceType/get` takes `typeIDs`,
    `customerFlag/get` takes `customerIDs` (it has no ID of its own),
    `cancellationReason/get` takes `reasonIDs`, and so on. Every `get`
    endpoint in the real Swagger has exactly one array-typed param; that's
    always the right one. A `get` that came from a help-centre page may have
    none (the page wrote the ID in the singular); it is left out here, and
    the server falls back to `{entity}IDs`.
    """
    out = {}
    for ep in endpoints.values():
        if ep["action"] != "get":
            continue
        array_params = [p["name"] for p in ep["params"] if p["type"] == "array"]
        if len(array_params) == 1:
            out[ep["entity"]] = array_params[0]
    return out


# =====================================================================
# Source 1: a Swagger 2.0 file (exact; needs the office's API key to obtain)
# =====================================================================


def load_swagger(path: str) -> dict:
    """Read a saved Swagger 2.0 document.

    Accepts the raw JSON that `POST /api/documentation/swagger` returns, or
    the legacy `window.swagger={...};` wrapper the public file used.
    """
    text = Path(path).read_text().strip()
    prefix = "window.swagger="
    if text.startswith(prefix):
        text = text[len(prefix):].strip()
        if text.endswith(";"):
            text = text[:-1]
    try:
        spec = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: not a Swagger JSON document ({exc})") from exc
    if not isinstance(spec, dict) or "paths" not in spec or "definitions" not in spec:
        raise SystemExit(f"{path}: no `paths`/`definitions` -- this is not a Swagger 2.0 document")
    return spec


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
        if entity in SKIP_ENTITIES:
            continue
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


def build_entities(spec: dict, endpoints: dict) -> dict:
    entities = {}
    endpoint_entities = {ep["entity"] for ep in endpoints.values()}
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


def spec_from_swagger(swagger: dict, source: str) -> dict:
    endpoints = build_endpoints(swagger)
    return {
        "info": {"title": swagger.get("info", {}).get("title", "FieldRoutes"), "source": source},
        "endpoints": endpoints,
        "entities": build_entities(swagger, endpoints),
    }


# =====================================================================
# Source 2: the help centre's API reference pages
# =====================================================================


def http_get(url: str, *, retries: int = 6) -> str:
    """GET a help-centre page. The site answers 429 to bursts; back off and retry."""
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=40) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == retries - 1:
                raise SystemExit(f"{url}: HTTP {exc.code}") from exc
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            wait = float(retry_after) if retry_after.isdigit() else 2.0 * (attempt + 1)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == retries - 1:
                raise SystemExit(f"{url}: {exc}") from exc
            wait = 2.0 * (attempt + 1)
        time.sleep(wait)
    raise AssertionError("unreachable")


def fetch_pages(*, save_dir: Path | None = None, delay: float = 0.3) -> dict[str, str]:
    """Download every `/apidocs/*.md` page the index lists. Returns {slug: text}."""
    index = http_get(KB_INDEX_URL)
    slugs = sorted(set(KB_PAGE_RE.findall(index)))
    if not slugs:
        raise SystemExit(f"{KB_INDEX_URL}: no /apidocs/ pages listed -- the help centre's layout changed")
    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
    pages = {}
    for n, slug in enumerate(slugs, 1):
        pages[slug] = http_get(f"https://knowledgebase.fieldroutes.com/apidocs/{slug}.md")
        if save_dir:
            (save_dir / f"{slug}.md").write_text(pages[slug])
        if n % 50 == 0:
            print(f"  fetched {n}/{len(slugs)} pages", file=sys.stderr)
        time.sleep(delay)  # one at a time, politely: the site rate-limits
    return pages


def read_pages(pages_dir: Path) -> dict[str, str]:
    pages = {p.stem: p.read_text() for p in sorted(pages_dir.glob("*.md"))}
    if not pages:
        raise SystemExit(f"{pages_dir}: no .md pages found")
    return pages


_FRONT_MATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
_CODE_FENCE_RE = re.compile(r"^```.*?^```[ \t]*$", re.M | re.S)
# An action is introduced by its HTTP method on one line and the path, indented, on the next.
_ENDPOINT_RE = re.compile(r"^(GET|POST|PUT|PATCH|DELETE)[ \t]*\n[ \t]+/api/([A-Za-z0-9_]+)/([A-Za-z0-9_]+)[ \t]*$", re.M)
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$", re.M)
_API_PATH_RE = re.compile(r"/api/([A-Za-z0-9_]+)/[A-Za-z0-9_]+")
_DATA_MODEL_RE = re.compile(r"^##[ \t]+(?:\d+\.[ \t]*)?Data Model\b.*$", re.M)
_NUMBERING_RE = re.compile(r"^\d+(?:\.\d+)*\.?[ \t]+")
_ABBREVIATED_RE = re.compile(r"\bparameters from\b", re.I)
# A Data Model sub-table that describes the entries of a nested array, not the record itself.
_NESTED_RE = re.compile(r"nested|sub-?object|line items?|each (?:entry|item)\b|each `[^`]+` entry", re.I)
_FIELD_RANGE_RE = re.compile(r"^`([A-Za-z_]+?)(\d+)`\s*[–—-]\s*`([A-Za-z_]+?)(\d+)`$")


def clean_page(text: str) -> str:
    """Normalise a page: no front matter, plain spaces (the pages indent with NBSP), no CRs."""
    text = text.replace("\r\n", "\n").replace("\xa0", " ")
    return _FRONT_MATTER_RE.sub("", text, count=1)


def strip_code(text: str) -> str:
    """Remove fenced code blocks, so an example request can't pass for an endpoint or a table."""
    return _CODE_FENCE_RE.sub("", text)


def clean_text(cell: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", cell)).strip()


def _split_row(line: str) -> list[str]:
    line = line.strip()
    line = line[1:] if line.startswith("|") else line
    line = line[:-1] if line.endswith("|") and not line.endswith("\\|") else line
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def iter_tables(text: str):
    """Yield `(header, rows, notes, start)` per Markdown table.

    `rows` are the rows as wide as the header. `notes` are the one-cell rows
    the pages use as group labels ("Billing Address") or to cut a list short
    ("All other parameters from Create are also accepted").
    """
    lines = text.split("\n")
    offset = 0
    offsets = []
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1
    i = 0
    while i < len(lines) - 1:
        if lines[i].startswith("|") and re.match(r"^\|\s*:?-{3,}", lines[i + 1]):
            header = _split_row(lines[i])
            rows, notes = [], []
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                row = _split_row(lines[j])
                if len(row) == len(header):
                    rows.append(row)
                elif len(row) == 1:
                    notes.append(clean_text(row[0]))
                j += 1
            yield header, rows, notes, offsets[i]
            i = j
        else:
            i += 1


def normalize_type(doc_type: str) -> str:
    """Map the pages' type words onto the Swagger words the spec already uses.

    Follows the pages' own "Type Conversion Map" (int -> integer; date,
    datetime, time -> string; float, double, decimal -> number). Anything
    that can be a list (`array<int>`, `int[]`, `int|array`, ...) is `array`:
    FieldRoutes takes a one-item list wherever it takes a scalar.
    """
    t = doc_type.strip().strip("`").lower()
    if "array" in t or "[]" in t:
        return "array"
    return {
        "int": "integer",
        "integer": "integer",
        "bool": "boolean",
        "boolean": "boolean",
        "float": "number",
        "double": "number",
        "decimal": "number",
        "number": "number",
        "object": "object",
        "file": "file",
    }.get(t, "string")


def _column(header: list[str], *names: str) -> int | None:
    for i, cell in enumerate(header):
        if cell.rstrip("?").strip().lower() in names:
            return i
    return None


def _last_heading_before(text: str, pos: int) -> str:
    heading = ""
    for m in _HEADING_RE.finditer(text, 0, pos):
        heading = m.group(2)
    return _NUMBERING_RE.sub("", clean_text(heading))


def parse_endpoints(slug: str, text: str) -> list[dict]:
    """Every action a page documents, with its request-parameter table(s)."""
    body = strip_code(clean_page(text))
    marks = list(_ENDPOINT_RE.finditer(body))
    found = []
    for k, m in enumerate(marks):
        method, entity, action = m.group(1), m.group(2), m.group(3)
        section = body[m.end(): marks[k + 1].start() if k + 1 < len(marks) else len(body)]
        # Request params sit before the "Response" heading; tables after it describe output.
        response = re.search(r"^#{2,6}[ \t]+(?:\d[\d.]*[ \t]+)?Response\b", section, re.M)
        request = section[: response.start()] if response else section

        intro_end = min(
            (x.start() for x in (_HEADING_RE.search(request), re.search(r"^\|", request, re.M)) if x),
            default=len(request),
        )
        paragraphs = [clean_text(p) for p in re.split(r"\n[ \t]*\n", request[:intro_end])]
        description = "\n\n".join(p for p in paragraphs if p)

        params, dropped, notes = [], [], []
        seen = set()
        for header, rows, table_notes, _ in iter_tables(request):
            name_i, type_i, desc_i = _column(header, "parameter"), _column(header, "type"), _column(header, "description")
            if name_i != 0:
                continue
            req_i, default_i = _column(header, "required"), _column(header, "default")
            notes.extend(table_notes)
            for row in rows:
                raw: dict[str, Any] = {
                    "name": row[0].strip().strip("`"),
                    "type": normalize_type(row[type_i]) if type_i is not None else "string",
                    "required": req_i is not None and row[req_i].strip().lower() in ("yes", "required", "true"),
                    "description": clean_text(row[desc_i]) if desc_i is not None else "",
                }
                if default_i is not None and (default := row[default_i].strip().strip("`")) not in ("", "-", "--", "—"):
                    raw["default"] = default
                param = slim_param(raw)
                if param is None:
                    dropped.append(raw["name"])
                elif param["name"] not in seen:
                    seen.add(param["name"])
                    params.append(param)
        found.append(
            {
                "entity": entity,
                "action": action,
                "method": method,
                "summary": _last_heading_before(body, m.start()) or action,
                "description": description,
                "params": params,
                "page": slug,
                "pageEndpoints": len(marks),
                "dropped": dropped,
                "abbreviated": [n for n in notes if _ABBREVIATED_RE.search(n)],
            }
        )
    return found


def parse_entity_fields(slug: str, text: str) -> tuple[str, dict, list[str]] | None:
    """`(entity, fields, skipped sub-tables)` from a page's "Data Model" section, or None.

    Field tables directly under the heading are the record. A sub-section's
    first field table counts too when the sub-section is a numbered part of
    the model ("3.2 Service Address Fields") or lists conditional/optional
    fields -- unless it describes the entries of a nested array, which are
    not fields of the record itself. Skipped sub-tables are returned so the
    report can show them.
    """
    page = clean_page(text)
    mentions = Counter(_API_PATH_RE.findall(page))  # the page's own examples name the entity
    body = strip_code(page)
    start = _DATA_MODEL_RE.search(body)
    if not start or not mentions:
        return None
    entity = mentions.most_common(1)[0][0]
    rest = body[start.end():]
    end = re.search(r"^##[ \t]", rest, re.M)
    section = rest[: end.start()] if end else rest

    parts = re.split(r"^#{3,6}[ \t]+(.*)$", section, flags=re.M)
    chunks = [(None, parts[0])] + [(clean_text(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]
    fields: dict[str, dict] = {}
    skipped = []
    for heading, chunk in chunks:
        tables = [t for t in iter_tables(chunk) if _column(t[0], "field") == 0 and _column(t[0], "type") is not None]
        if not tables:
            continue
        if heading is None:
            wanted = tables
        else:
            intro = chunk[: tables[0][3]]
            is_part = bool(_NUMBERING_RE.match(heading)) or bool(re.search(r"conditional|optional", heading, re.I))
            if not is_part or _NESTED_RE.search(heading) or _NESTED_RE.search(intro):
                skipped.append(f"{slug}: {heading}")
                continue
            wanted = tables[:1]
        for header, rows, _, _ in wanted:
            type_i, desc_i = _column(header, "type"), _column(header, "description")
            for row in rows:
                cell = row[0].strip()
                span = _FIELD_RANGE_RE.match(cell)
                if span and span.group(1) == span.group(3):  # `districtCode1` – `districtCode5`
                    names = [f"{span.group(1)}{n}" for n in range(int(span.group(2)), int(span.group(4)) + 1)]
                else:
                    names = [cell.strip("`")] if re.fullmatch(r"`[A-Za-z_][A-Za-z0-9_]*`", cell) else []
                for name in names:
                    fields.setdefault(
                        name,
                        {
                            "type": normalize_type(row[type_i]),
                            "description": clean_text(row[desc_i]) if desc_i is not None else "",
                        },
                    )
    return entity, fields, skipped


def parse_pages(pages: dict[str, str]) -> tuple[dict, dict, dict]:
    """`(endpoints, entity fields, notes)` as the help-centre pages document them."""
    candidates: dict[str, list[dict]] = {}
    for slug in sorted(pages):
        for ep in parse_endpoints(slug, pages[slug]):
            if ep["entity"] not in SKIP_ENTITIES:
                candidates.setdefault(f"{ep['entity']}/{ep['action']}", []).append(ep)

    endpoints, duplicates, dropped, abbreviated = {}, {}, {}, {}
    for key, options in sorted(candidates.items()):
        # An action's own page beats a how-to guide that walks through several actions.
        options.sort(key=lambda ep: (ep["pageEndpoints"], ep["page"]))
        best = options[0]
        if len(options) > 1:
            duplicates[key] = [ep["page"] for ep in options]
        if best["dropped"]:
            dropped[key] = best["dropped"]
        if best["abbreviated"]:
            abbreviated[key] = best["abbreviated"]
        endpoints[key] = {k: best[k] for k in ("entity", "action", "method", "summary", "description", "params", "page")}

    fields: dict[str, dict] = {}
    skipped_tables = []
    for slug in sorted(pages):
        parsed = parse_entity_fields(slug, pages[slug])
        if not parsed:
            continue
        entity, entity_fields, skipped = parsed
        skipped_tables.extend(skipped)
        if entity in SKIP_ENTITIES or not entity_fields:
            continue
        if entity in fields:
            raise SystemExit(f"two Data Model pages both describe {entity!r} (second: {slug}) -- check the parser")
        fields[entity] = entity_fields
    notes = {
        "duplicatePages": duplicates,
        "droppedParams": dropped,
        "abbreviatedParamLists": abbreviated,
        "skippedFieldTables": skipped_tables,
    }
    return endpoints, fields, notes


# =====================================================================
# Merge: Swagger records stay as they are; the pages only add
# =====================================================================


def _is_docs(record: dict) -> bool:
    return record.get("source") == DOCS


def swagger_layer(spec: dict) -> dict:
    """The spec minus everything a previous run took from the help centre."""
    endpoints = {}
    for key, ep in spec["endpoints"].items():
        if _is_docs(ep):
            continue
        endpoints[key] = {**ep, "params": [p for p in ep["params"] if not _is_docs(p)]}
    entities = {}
    for name, entity in spec["entities"].items():
        if _is_docs(entity):
            continue
        kept = {k: v for k, v in entity.items() if k != "getIdParam"}
        kept["fields"] = {f: d for f, d in entity["fields"].items() if not _is_docs(d)}
        entities[name] = kept
    return {"info": spec.get("info", {}), "endpoints": endpoints, "entities": entities}


def _same_type(a: str, b: str) -> bool:
    alias = {"int": "integer", "bool": "boolean", "": "string"}
    return alias.get(a, a) == alias.get(b, b)


def merge(previous: dict, doc_endpoints: dict, doc_fields: dict) -> tuple[dict, dict]:
    """Lay the help-centre pages over the Swagger-derived part of `previous`.

    Returns `(spec, findings)`; `findings` is what the pages say that was
    *not* applied, or could not be derived.
    """
    base = swagger_layer(previous)
    base_id_params = build_get_id_params(base["endpoints"])
    endpoints: dict[str, dict] = {}
    disagree: dict[str, list] = {}
    omitted: dict[str, list] = {}
    selector_variants: dict[str, list] = {}
    made_optional: dict[str, list] = {}

    for key, ep in base["endpoints"].items():
        doc = doc_endpoints.get(key)
        if not doc:
            endpoints[key] = ep
            continue
        params = list(ep["params"])
        known = {p["name"]: p for p in params}
        documented = {p["name"] for p in doc["params"]}
        if missing := [name for name in known if name not in documented]:
            omitted[key] = missing
        for p in doc["params"]:
            if (mine := known.get(p["name"])) is not None:
                if not _same_type(mine["type"], p["type"]) or mine["required"] != p["required"]:
                    disagree.setdefault(key, []).append(
                        {
                            "param": p["name"],
                            "spec": {"type": mine["type"], "required": mine["required"]},
                            "docs": {"type": p["type"], "required": p["required"]},
                        }
                    )
                continue
            if ep["action"] == "get" and (p["required"] or p["type"] == "array"):
                # The real `get` already has its bulk-ID param. A second required or
                # array-typed one on the page is that same selector respelt (`noteID`
                # for `noteIDs`) or an alias; adding it would break the one-array rule.
                selector_variants.setdefault(key, []).append(p["name"])
                continue
            if p["required"]:
                # FieldRoutes says nothing breaking changed, so an endpoint that
                # already existed cannot have gained a required param.
                made_optional.setdefault(key, []).append(p["name"])
            params.append({**p, "required": False, "source": DOCS})
        endpoints[key] = {**ep, "params": params}

    for key, doc in doc_endpoints.items():
        if key in endpoints:
            continue
        endpoints[key] = {
            "entity": doc["entity"],
            "action": doc["action"],
            "method": doc["method"],
            "summary": doc["summary"],
            "description": doc["description"],
            "params": doc["params"],
            "source": DOCS,
        }

    entities: dict[str, dict] = {}
    for name, entity in base["entities"].items():
        fields = dict(entity["fields"])
        for fname, fdef in doc_fields.get(name, {}).items():
            if fname not in fields:
                fields[fname] = {**fdef, "source": DOCS}
        entities[name] = {**entity, "fields": fields}
    for name in sorted({ep["entity"] for ep in endpoints.values()} - set(entities)):
        fields = {f: {**d, "source": DOCS} for f, d in doc_fields.get(name, {}).items()}
        entities[name] = {"fields": fields, "source": DOCS}

    id_params = build_get_id_params(endpoints)
    moved = {e: (base_id_params.get(e), id_params.get(e)) for e in base["entities"] if base_id_params.get(e) != id_params.get(e)}
    if moved:
        raise SystemExit(f"refusing to write: the merge would change Swagger-derived getIdParam values: {moved}")
    for name, entity in entities.items():
        if (id_param := id_params.get(name)) and id_param != f"{name}IDs":
            entity["getIdParam"] = id_param
    underived = sorted(ep["entity"] for ep in endpoints.values() if ep["action"] == "get" and ep["entity"] not in id_params)

    spec = {
        "info": {
            "title": base["info"].get("title", "FieldRoutes"),
            "source": KB_INDEX_URL,
            "baseline": f"{LEGACY_SWAGGER_URL} (retired); records without \"source\": \"docs\" come from it",
        },
        "endpoints": endpoints,
        "entities": entities,
    }
    findings = {
        "specEndpointsNotInDocs": sorted(set(base["endpoints"]) - set(doc_endpoints)),
        "docsDisagree": disagree,
        "docsOmitParams": omitted,
        "getSelectorVariantsNotAdded": selector_variants,
        "docsRequiredAddedAsOptional": made_optional,
        "getIdParamNotDerivable": underived,
    }
    return spec, findings


# =====================================================================
# Report
# =====================================================================


def diff_specs(old: dict, new: dict) -> dict:
    """What changed between two spec files, by name and type."""
    old_eps, new_eps = old.get("endpoints", {}), new["endpoints"]
    old_ents, new_ents = old.get("entities", {}), new["entities"]
    params_added, params_removed, params_retyped = {}, {}, {}
    for key in sorted(set(old_eps) & set(new_eps)):
        before = {p["name"]: p for p in old_eps[key]["params"]}
        after = {p["name"]: p for p in new_eps[key]["params"]}
        if added := [n for n in after if n not in before]:
            params_added[key] = added
        if removed := [n for n in before if n not in after]:
            params_removed[key] = removed
        if retyped := {
            n: [before[n]["type"], after[n]["type"]] for n in before if n in after and before[n]["type"] != after[n]["type"]
        }:
            params_retyped[key] = retyped
    fields_added, fields_removed = {}, {}
    for name in sorted(set(old_ents) & set(new_ents)):
        before, after = old_ents[name]["fields"], new_ents[name]["fields"]
        if added := [f for f in after if f not in before]:
            fields_added[name] = added
        if removed := [f for f in before if f not in after]:
            fields_removed[name] = removed
    id_params = {
        name: [old_ents.get(name, {}).get("getIdParam"), new_ents.get(name, {}).get("getIdParam")]
        for name in sorted(set(old_ents) | set(new_ents))
        if old_ents.get(name, {}).get("getIdParam") != new_ents.get(name, {}).get("getIdParam")
    }
    return {
        "endpoints": {"before": len(old_eps), "after": len(new_eps)},
        "entities": {"before": len(old_ents), "after": len(new_ents)},
        "endpointsAdded": sorted(set(new_eps) - set(old_eps)),
        "endpointsRemoved": sorted(set(old_eps) - set(new_eps)),
        "entitiesAdded": sorted(set(new_ents) - set(old_ents)),
        "entitiesRemoved": sorted(set(old_ents) - set(new_ents)),
        "paramsAdded": params_added,
        "paramsRemoved": params_removed,
        "paramsRetyped": params_retyped,
        "fieldsAdded": fields_added,
        "fieldsRemoved": fields_removed,
        "getIdParamChanged": id_params,
    }


def _count(mapping: dict) -> int:
    return sum(len(v) for v in mapping.values())


def print_summary(report: dict) -> None:
    d = report["diff"]
    print(f"endpoints {d['endpoints']['before']} -> {d['endpoints']['after']}, entities {d['entities']['before']} -> {d['entities']['after']}")
    print(f"  endpoints added: {len(d['endpointsAdded'])}, removed: {len(d['endpointsRemoved'])}")
    print(f"  entities added: {len(d['entitiesAdded'])}, removed: {len(d['entitiesRemoved'])}")
    print(f"  params added: {_count(d['paramsAdded'])}, removed: {_count(d['paramsRemoved'])}, retyped: {_count(d['paramsRetyped'])}")
    print(f"  fields added: {_count(d['fieldsAdded'])}, removed: {_count(d['fieldsRemoved'])}")
    f = report.get("findings")
    if not f:
        return
    print("help-centre pages vs the Swagger-derived records (reported, not applied):")
    print(f"  type/required disagreements: {_count(f['docsDisagree'])} on {len(f['docsDisagree'])} endpoints")
    print(f"  params the pages leave out: {_count(f['docsOmitParams'])} on {len(f['docsOmitParams'])} endpoints")
    print(f"  `get` ID respellings/aliases not added: {_count(f['getSelectorVariantsNotAdded'])}")
    print(f"  new params the pages call required, added as optional: {_count(f['docsRequiredAddedAsOptional'])}")
    if f["specEndpointsNotInDocs"]:
        print(f"  WARNING: in the spec but no longer documented: {f['specEndpointsNotInDocs']}")
    if f["getIdParamNotDerivable"]:
        print(f"  WARNING: no array param on `get`, so getIdParam falls back to {{entity}}IDs (unverified): {f['getIdParamNotDerivable']}")
    n = report["parseNotes"]
    if n["abbreviatedParamLists"]:
        print(f"  note: param list cut short on the page for: {sorted(n['abbreviatedParamLists'])}")
    if n["droppedParams"]:
        print(f"  note: non-identifier param names dropped: {n['droppedParams']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--swagger", metavar="FILE", help="rebuild from a saved Swagger 2.0 document instead of the help centre")
    parser.add_argument("--pages-dir", metavar="DIR", type=Path, help="read help-centre pages from DIR instead of the network")
    parser.add_argument("--save-pages", metavar="DIR", type=Path, help="keep the downloaded help-centre pages in DIR")
    parser.add_argument("--out", metavar="FILE", type=Path, default=OUT_PATH, help="spec file to merge into and write")
    parser.add_argument("--report", metavar="FILE", type=Path, help="write the full change report as JSON")
    args = parser.parse_args()

    previous = json.loads(args.out.read_text()) if args.out.exists() else None
    report: dict[str, Any] = {}
    if args.swagger:
        spec = spec_from_swagger(load_swagger(args.swagger), f"swagger file: {Path(args.swagger).name}")
    else:
        if previous is None:
            raise SystemExit(
                f"{args.out} does not exist. The help-centre pages can only add to a Swagger-derived spec; "
                "restore the file from git, or rebuild it with --swagger FILE."
            )
        pages = read_pages(args.pages_dir) if args.pages_dir else fetch_pages(save_dir=args.save_pages)
        doc_endpoints, doc_fields, report["parseNotes"] = parse_pages(pages)
        print(f"parsed {len(pages)} pages: {len(doc_endpoints)} endpoints, {len(doc_fields)} entities")
        spec, report["findings"] = merge(previous, doc_endpoints, doc_fields)
    report["diff"] = diff_specs(previous or {}, spec)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(spec, indent=1, sort_keys=True) + "\n")
    print(f"Wrote {len(spec['endpoints'])} endpoints and {len(spec['entities'])} entities to {args.out}")
    print_summary(report)
    if args.report:
        args.report.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
        print(f"Full report: {args.report}")


if __name__ == "__main__":
    main()
