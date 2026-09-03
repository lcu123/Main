# CLAUDE.md — Zest FieldRoutes MCP

Context file for Claude Code / any agent working in this repo. Read fully before changing anything.

## Goal

A remote MCP server that gives Claude (claude.ai web/mobile, Cowork, Claude Desktop) read **and write** access to Zest Lawn & Pest's FieldRoutes account. Priorities, in order:

1. Answer office questions fast: which property is assigned to which route/tech, what's due for service, what a tech must know before a stop (Red Notes, flags, alerts), who owes money.
2. Take safe actions: notes, tasks, schedule/reschedule/cancel/complete appointments, hold a spot, update Red Notes, change subscription scheduling (frequency, billing frequency, seasonal window, custom date, region, preferred tech/day/time).
3. Reach every FieldRoutes endpoint without bloating context: 30 curated tools by default, all 187 endpoints via generic tools, optional per-endpoint generation.
4. Never surprise the owner: deletes and real card charges are blocked unless explicitly enabled; writes can be switched off per deployment, or restricted to a test customer while validating a new deployment.

## Non-goals

- Not a general FieldRoutes SDK. Tool outputs are trimmed to what an office user reads, not full records (use `get` for full records).
- Not multi-tenant. One FieldRoutes office per deployment; run a second Railway service for another office or a read-only variant.
- Not OAuth. Access control is a secret path segment (claude.ai's connector UI has no header field). Treat the URL as a password.
- No API field controls FieldRoutes' "Autoschedule" (Due Date Method) setting, or lets you define the dates of a Custom Schedule (only pick an existing one by ID). Both stay UI-only; don't invent params for them.

## How FieldRoutes' API actually works (do not "fix" these)

Built from the official Swagger spec (`src/fr_mcp/fieldroutes_spec.json`, extracted by `scripts/extract_spec.py` from `https://api.fieldroutes.com/resources/swagger.js`, which is itself the real spec, not a mock). Every earlier third-party MCP got this wrong:

- `POST https://{subdomain}.fieldroutes.com/api/{entity}/{action}` — `.pestroutes.com` also works (`FR_BASE_URL` override).
- Body is `application/x-www-form-urlencoded`, **not JSON**. `authenticationKey` and `authenticationToken` go in the body. **httpx 0.28 gotcha:** `AsyncClient.post(url, data=list_of_tuples)` silently degrades to a sync-only content stream and crashes an async client (`data=` is only treated as form-urlencoded when it's a `Mapping`). `client.py` pre-encodes with `urlencode()` and sends via `content=` plus an explicit `Content-Type` header instead. Don't switch this back to `data=`.
- Arrays are PHP-style repeated keys: `customerIDs[]=1&customerIDs[]=2`.
- Search filters are JSON strings: `balance={"operator":">","value":"0"}`. Operators: `> < >= <= = != IN BETWEEN LIKE STARTSWITH ENDSWITH CONTAINS`. A plain list in a search filter is sent as an `IN` object (`client.normalize_search_filters`).
- Responses are JSON; HTTP is 200 even on failure (`success: false` + `errorMessage`). **Verified live on `office/search` and `office/get`**, every response also carries an envelope the swagger doesn't document: `params` (an echo of the *entire request, authentication key and token included* — `client.call()` strips it before returning, never reintroduce it into tool output), `tokenUsage` (FieldRoutes' own `requestsReadToday`/`requestsWriteToday` — authoritative, shared across integrations, survives our restarts; `UsageCounter.sync()` adopts it over our estimate), `tokenLimits`, `requestAPIKeyType`, `requestAction`, `endpoint`, `ignoredParams` (a **list**, and it precedes the data key — so "first list in the body" can never be how records are found), `processingTime`, `count`. `search` returns the ID list under `{entity}IDs` (singular entity name; `idName`/`propertyName` both name that key) and, with `includeData=1`, the first 1000 resolved records under the **plural** entity name (`offices`, not `office`) named by `propertyNameData`, plus `{entity}IDsNoDataExported` for the rest, which come via `get` in chunks of 1000. `get` returns records under the plural name too, named by `propertyName`. `client.extract_records()` trusts those name hints first, then plural/singular, then a metadata-aware scan — don't shortcut it.
- **`get`'s bulk-ID param is *not* always `{entity}IDs`.** 24 of 56 entities deviate: `serviceType/get` takes `typeIDs`, `cancellationReason/get` takes `reasonIDs`, `customerFlag/get` takes `customerIDs` (it has no ID of its own — its "get" returns every flag row for those customers), `applicationMethod/get` takes the FieldRoutes-side-misspelled `applciationMethodIDs`, and 20 more. `extract_spec.py` derives the real one per entity (every `get` endpoint has exactly one array-typed param — that one) into `entities.{entity}.getIdParam`; `server._id_param(entity)` reads it, defaulting to `{entity}IDs` when unset. Always fetch through `_get_rows`/`_search_rows`, never hand-roll a `{entity}IDs` key.
- FieldRoutes' own swagger has real defects, not just gaps: `compassCustomer/search` has three params with `name: 0/1/2` (types `"o"`/`"c"`/`"d"`, evidently a string exploded character-by-character by their spec generator). `extract_spec.py`'s `slim_param` drops any param whose name isn't a valid Python identifier rather than crash tool registration. If a future re-extraction adds new junk like this, it'll be silently dropped the same way — check `describe_endpoint` output if an entity seems to be missing an obvious param.
- Rate limits: 60 req/min/office; 3,000 reads + 3,000 writes/day, **shared with every other integration touching the office** (FieldRoutes' own UI, Zapier, website forms). `client.py` has a sliding-window rate limiter at 55/min, and a `UsageCounter` that tracks this server's own reads/writes per local day and refuses new calls of a kind at 95% of `FR_DAILY_READ_LIMIT`/`FR_DAILY_WRITE_LIMIT` (default 3000 each) rather than running the office's shared quota to zero. This is a self-imposed estimate, not authoritative — FieldRoutes' server is.
- Field names that matter: customers use `fname`/`lname`/`phone1`; the customer's **Red Notes = `customer.notes`**, permission `editRedNotes`. `notes` is write-only on `customer/update` and does **not** appear in `customer/search` or `customer/get`'s response schema — confirmed against the real spec, not assumed. **Unverified**: whether Red Notes are readable another way — the FieldRoutes UI shows a Red Note as a record with an author, timestamp, and "Visible to Tech" flag, which looks like it could be a `note` entity row (`note/search` with the right `typeID`). Check `list_notes` output against the UI on the first live customer; if a Red Note shows up there with a distinguishing type, `customer_alerts`/`set_red_notes` should be revisited to read-before-write instead of blind-overwrite.
- **Verified live on `customer/get`**: every field value comes back as a JSON *string*, even numeric/ID ones (`"customerID": "10000"`, `"balance": "0.00"`) — `_int()`/`float()` handle this fine, no fix needed. More importantly, `customer.subscriptionIDs` and `customer.appointmentIDs` are **comma-separated strings** (`"appointmentIDs": "10002,10006,16404"`), not JSON arrays, despite the swagger's declared `type: array` — same pattern as `additionalTechs`. Passing that string straight into `_get_rows()` used to iterate it character-by-character (a real bug this caught: `"12,34"` silently became IDs 1, 2, 3, 4 instead of 12 and 34, which for a customer with a multi-digit or second subscription ID would fetch the wrong subscription). Always route a customer's own ID-list fields through `_id_list()`, never index them directly. Also: the live record carries several fields the swagger doesn't declare at all (`customerNumber`, `lotSize`, `subPropertyType`, `customerSubSourceID`/`customerSubSource`, `districtCode1`-`5`) — the spec's field list is a floor, not a ceiling; don't assume it's exhaustive when reading a live record.
- Scheduling notes = `specialScheduling` (on `customer`, read-only from the tools' perspective — no curated write for it yet). Alerts = `task` with `type=1` (`type=0` is a plain task); task `category`: Billing 1, Customer Care 10, Appt Status 15. Notes need `contactType` (Note Type ID from Admin → Preferences → Note Types; no API endpoint lists them) — note's own row ID in write params is **`contactID`, not `noteID`** (`note/update`, `note/delete`). Cancel uses `cancelReason` (string) + `cancelledBy` (employeeID). Appointment `status`: 0 pending / 1 completed / 2 no-show. Subscription `active`: 1 active / 0 frozen / -3 lead. Routes/spots expose `apiCanSchedule`. `appointment.additionalTechs`/`route.additionalTechs` are **comma-separated strings** of employeeIDs, not arrays (confirmed against the spec's own type declaration) — `_emp_list()` splits on `,`. `customer/get` takes `includeCustomerFlag=1` to inline the customer's flags (array under `customerFlag`); flags are also independently reachable via the `customerFlag` entity (`customerIDs`/`customerFlags` filters).

## Repo layout

```
src/fr_mcp/
  client.py             HTTP client: form encoding, auth, retries, rate limit, daily usage quota, search+get pagination
  server.py             MCPServer, 30 curated tools, generic tools, write guards + allowlist, HTTP app + entrypoint
  generated.py           Optional: one typed tool per endpoint, built from the spec at startup
  fieldroutes_spec.json Slimmed Swagger: 187 endpoints (params, required, descriptions) + entity field lists + getIdParam overrides
scripts/extract_spec.py  Regenerates fieldroutes_spec.json from FieldRoutes' public swagger.js
tests/test_wire_format.py  Fake FieldRoutes (httpx.MockTransport) verifying the wire format, pagination, quota, and allowlist
Dockerfile, railway.json   Railway deploy (healthcheck /healthz, installs from requirements.lock)
requirements.lock          Pinned runtime deps; regenerate deliberately, see README
.env.example               Every env var, commented
README.md                  Deploy steps + first-run checklist (user-facing)
```

## Tool inventory (server.py)

Generic: `list_endpoints`, `describe_endpoint`, `search`, `get`, `call`, `health_check`.
Reads: `find_customer`, `customer_360`, `customer_alerts`, `day_schedule`, `property_assignments`, `route_stops`, `crew_schedule`, `due_for_service`, `open_slots`, `ar_aging`, `lookups`, `subscription_details`, `list_notes`.
Writes: `add_note`, `update_note`, `set_red_notes`, `create_task`, `schedule_appointment`, `reschedule_appointment`, `update_appointment_notes`, `cancel_appointment`, `complete_appointment`, `reserve_slot`, `update_subscription`.

That's 30. Keep this count and the list in sync with `server.py` and README's tool table — the HTTP app test (`test_http_app_secret_path_healthz_and_bearer`) asserts the exact count and will fail the moment they drift.

Naming: curated tools are verbs/nouns an office person would say. Generated tools are `fr_{entity}_{action}`.

## Conventions

- Every tool returns a JSON string via `_j()`. Trim records with `_pick(row, KEYS)`; the `*_KEYS` lists at the top of `server.py` define what an office user sees. Add a key there rather than dumping whole records.
- Curated reads should resolve IDs to names (`_emp_names`, customer lookups) and put the **property address** on any row that has a customer. When you already have the customer record in hand (e.g. `customer_360` fetching its own appointments/subscriptions), pass it into `_shape_appointments`/`_shape_subscriptions`' `customers` map instead of letting them re-fetch — a redundant `customer/get` costs a real call against the shared daily quota.
- Dates: `YYYY-MM-DD`; datetimes `YYYY-MM-DD HH:MM:SS`, office local time (`TZ` env var — Railway defaults to UTC). Default windows are today + 6 days.
- `FR_OFFICE_ID` is auto-applied to searches when set and no office filter is given.
- Writes go through `_writes_enabled()`, then `_require_customer_allowed()` (the `FR_WRITE_CUSTOMER_IDS` allowlist — see below). Anything that could cancel, charge, freeze, or overwrite Red Notes must confirm with the user (say so in the docstring — Claude reads it).
- **Write allowlist** (`FR_WRITE_CUSTOMER_IDS`, comma-separated customer IDs): when set, a write is refused unless it resolves to one of those customers. Direct-customer-ID tools (`add_note`, `set_red_notes`, `create_task`, `schedule_appointment`) check immediately; record-based tools (`update_note`, `reschedule_appointment`, `update_appointment_notes`, `cancel_appointment`, `complete_appointment`, `update_subscription`) fetch the existing record first via `_write_target_customer(entity, id)` — which is a no-op returning `None` instantly when the allowlist isn't set, so this costs nothing once the deployment goes live and the variable is cleared. `reserve_slot` is the one deliberate exemption: a spot hold names no customer and isn't customer data. The generic `call` tool and every generated tool go through the same `_resolve_write_customer()`/`_require_customer_allowed()` pair — fail closed: an entity with no customer relationship at all (route, spot writes other than reserve, employee, service type, ...) resolves to `None` and is refused when the allowlist is active, not silently allowed through.
- Generic `call` and generated tools share guards: `*/delete` needs `FR_ALLOW_DELETE=1`; `payment/create` with `doCharge=1` needs `FR_ALLOW_CHARGES=1`.
- Errors: raise `FieldRoutesError`; the `_tool` wrapper (curated tools) / inline `try/except` (generic `call`, generated tools) converts it to `ToolError` so the message reaches Claude.
- Tool docstrings are the schema descriptions. One or two sentences; say when to use it and what it returns. Context cost matters: 30 curated tools ≈ 2.5k tokens, `FR_EXPOSE_ALL` ≈ 70k.
- mcp SDK is **2.x** (`mcp.server.mcpserver.MCPServer`, not `FastMCP`; `ToolError` is `mcp.server.mcpserver.exceptions.ToolError`). Transport is streamable-HTTP, stateless, JSON responses, DNS-rebinding protection off (Railway hostnames). `mcp.call_tool()` returns a `CallToolResult` with `.content[0].text`, not a subscriptable list — if you're writing a test against it directly, don't index it.
- Generated tools are built with a dynamically-constructed `inspect.Signature` (real `inspect.Parameter` objects with proper annotations) attached to a `**kwargs`-accepting async function, not a hand-written one per endpoint. This is required for the MCP SDK's Pydantic-based schema derivation (`Tool.from_function`) to produce an accurate per-tool JSON schema, `required` fields included, from spec data at startup.
- Never log the HTTP request path in the `http` transport — it contains `MCP_PATH_SECRET`. `main()` runs uvicorn with `access_log=False` and never prints the resolved secret path (`/<secret>/mcp` in the startup log, not the real value). Don't reintroduce either.

## Environment variables

| Var | Purpose |
| --- | --- |
| `FR_AUTH_KEY`, `FR_AUTH_TOKEN` | FieldRoutes Admin → API → Manage Keys |
| `FR_SUBDOMAIN` / `FR_BASE_URL` | Tenant host |
| `FR_OFFICE_ID` | Default office filter |
| `FR_DEFAULT_EMPLOYEE_ID` | Employee the API acts as (notes, tasks, appointments) |
| `FR_DEFAULT_NOTE_TYPE_ID` | Note Type for `add_note` |
| `FR_WRITES` | `on` (default) / `off` |
| `FR_ALLOW_DELETE`, `FR_ALLOW_CHARGES` | Unlock destructive actions |
| `FR_WRITE_CUSTOMER_IDS` | Comma-separated customer IDs writes are restricted to; unset = unrestricted. Use during first-run validation. |
| `FR_DAILY_READ_LIMIT`, `FR_DAILY_WRITE_LIMIT` | Self-imposed daily call budget (default 3000 each); refused at 95%. |
| `FR_EXPOSE_ENTITIES`, `FR_EXPOSE_ALL`, `FR_EXPOSE_VERBOSE` | Generated per-endpoint tools |
| `MCP_TRANSPORT` | `http` (Railway) / unset (stdio, Claude Desktop) |
| `PORT`, `MCP_PATH_SECRET`, `MCP_BEARER_TOKEN` | HTTP hosting + access control |
| `TZ` | Office local time zone |

## Dev loop

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                  # fake-API wire-format tests, must stay green (58 tests)
MCP_TRANSPORT=http FR_AUTH_KEY=x FR_AUTH_TOKEN=y FR_SUBDOMAIN=z MCP_PATH_SECRET=s fr-mcp
curl localhost:8080/healthz             # -> ok
```

To smoke-test tools without credentials, inject `FieldRoutesClient(transport=httpx.MockTransport(FakeFR().handler))` into `server._client` (see tests). `tests/test_wire_format.py`'s `FakeFR` seed data models the real deviations above (a `customerFlag` with no ID field, `additionalTechs` as a comma string, entities whose `get` id param isn't `{entity}IDs`) — extend the seed rather than special-casing around it if you add a test that needs new fixture data.

## Adding a tool

1. Check `describe_endpoint`/the spec for exact param names. Do not invent names — and don't assume `{entity}IDs` for a bulk-get id param; check `entities.{entity}.getIdParam` or just call `describe_endpoint`.
2. Write it next to similar tools in `server.py`, decorated `@_tool` (not `@mcp.tool()` directly — `_tool` wraps `FieldRoutesError` into `ToolError`), returning `_j(...)`.
3. Resolve IDs to names, include the property address, trim with `_pick`.
4. If it writes: call `_require_writes(tool)`, then `_require_customer_allowed(tool, customer_id)` — either directly (if the tool already takes `customer_id`) or via `await _write_target_customer(entity, record_id)` if it only takes an existing record's ID. If it can cancel/charge/delete/freeze/overwrite Red Notes, say "confirm with the user" in the docstring.
5. Add the tool to the README table, this file's inventory (and count), and `test_http_app_secret_path_healthz_and_bearer`'s expected tool count. Run `pytest`.

## Unverified against a live tenant

Everything is verified against the real published spec and a fake API built from it (not assumptions), but not against Zest's real account yet. The README's first-run checklist (health → lookups → find/360 → list_notes for the Red Note question above → schedule views → add_note → update_subscription read-back for the partial-update question → reserve+schedule+cancel → set_red_notes → allowlist-blocks-other-customers) is the order to validate, with `FR_WRITE_CUSTOMER_IDS` set to a test customer throughout. Likely surprises: `.fieldroutes.com` vs `.pestroutes.com`; API key lacking "can schedule" permission (breaks `schedule_appointment`/`open_slots`); `spot` search semantics for `reserved`; whether `subscription/update` blanks fields you don't pass or leaves them alone (untested — this is the one that could silently corrupt a subscription, verify first); whether scheduling/rescheduling/cancelling triggers a customer-facing SMS/email depending on office settings.

## Owner context

Zest Lawn & Pest (Sacramento): lawn fertilization/weed control, recurring pest, rodent exclusion, irrigation, mowing. Owner thinks in unit economics and route density; tools that surface density (lat/lng, distance to previous/next stop) are worth keeping. Office notes should stay to two sentences.
