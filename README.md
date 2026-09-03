# Zest FieldRoutes MCP

A remote [MCP](https://modelcontextprotocol.io) server that gives Claude (claude.ai web and mobile, Cowork, Claude Desktop) read **and write** access to one FieldRoutes office. Built for Zest Lawn & Pest; deploys to Railway in about fifteen minutes.

Ask Claude things like:

- "Who's on Carlos's route tomorrow and what does he need to know before each stop?"
- "Which properties in Folsom are due for aeration in the next two weeks?"
- "Move the Smiths' Thursday visit to the open slot on Friday afternoon."
- "Change subscription 2373 to every 45 days and put a note on the account."
- "Who owes us more than $200 and is over 60 days out?"

## Tools

Curated tools are named the way an office person talks. Every one returns trimmed JSON: names instead of IDs, the property address on anything with a customer, and only the fields an office user reads. The generic tools reach all 187 FieldRoutes endpoints when a curated tool doesn't fit.

| Tool | What it does |
| --- | --- |
| **Generic** | |
| `list_endpoints` | List FieldRoutes entities, or one entity's actions. |
| `describe_endpoint` | Exact params for `entity/action`, plus the entity's fields. |
| `search` | Run any `entity/search` with scalar, list (IN) or operator-object filters. |
| `get` | Full records for a list of IDs, chunked at 1000. |
| `call` | Call any endpoint directly. Same write guards as everything else. |
| `health_check` | Credentials, office, safety flags, and today's API quota usage. |
| **Reads** | |
| `find_customer` | By name, company, phone, email, street address or ID. |
| `customer_360` | Profile, property, flags, subscriptions, appointments, open tasks, latest notes, balance. |
| `customer_alerts` | What a tech must know before a stop: Red Notes, special scheduling, flags, alerts, balance. |
| `day_schedule` | Every appointment on a date, optionally one tech. |
| `property_assignments` | Which route and tech a property belongs to. |
| `route_stops` | Stops on one route in order, with distance to neighbours. |
| `crew_schedule` | Routes per day for a window, with tech and stop count. |
| `due_for_service` | Active subscriptions whose next service falls in a window. |
| `open_slots` | Schedulable spots in a window, with route density hints. |
| `ar_aging` | Who owes money, bucketed by age. |
| `lookups` | Employee, service type, region, office, reason and source IDs. |
| `subscription_details` | One subscription's full setup as the office sees it. |
| `list_notes` | All notes on an account, newest first. |
| **Writes** | |
| `add_note` | Add a note (tech-visible or customer-visible if asked). |
| `update_note` | Edit a note's text, type or visibility. |
| `set_red_notes` | Replace the account's Red Notes. Confirms first. |
| `create_task` | Create a task or an alert. |
| `schedule_appointment` | Book into a spot or onto a route. |
| `reschedule_appointment` | Move an appointment, or change its window, duration or tech. |
| `update_appointment_notes` | Set the visit notes the tech sees, and office-only notes. |
| `cancel_appointment` | Cancel. Confirms first. |
| `complete_appointment` | Mark completed or no-show. Generates the invoice, so confirms first. |
| `reserve_slot` | Hold a spot for a few minutes while confirming with the customer. |
| `update_subscription` | Job frequency, billing frequency, custom date, seasonal window, region, preferred tech/day/time, duration, call-ahead, sales reps, source, contract length, expiration, PO. |

Two things on the FieldRoutes subscription screen have no API: **Autoschedule** (Due Date Method) and defining the dates of a **Custom Schedule**. The API can only pick an existing custom schedule by ID. Both stay UI-only.

Optionally, set `FR_EXPOSE_ENTITIES` or `FR_EXPOSE_ALL` to add one typed tool per endpoint (`fr_customer_search`, `fr_appointment_cancel`, ...). That costs context on every message (all 187 is about 70k tokens), so leave it off unless you need it.

## Safety model

The MCP URL is the password. Anyone holding it has the access below, so treat it like one.

1. **Secret path.** The endpoint is `https://<host>/<MCP_PATH_SECRET>/mcp`. Everything else 404s. claude.ai's connector UI can't send headers, which is why the secret lives in the path.
2. **Bearer token** (`MCP_BEARER_TOKEN`), optional, for clients that can send headers.
3. **Least-privilege API key.** Make a dedicated FieldRoutes key for this server. Don't grant delete or payment processing.
4. **Kill switches.** `FR_WRITES=off` makes the deployment read-only. Deletes need `FR_ALLOW_DELETE=1`. Real card charges need `FR_ALLOW_CHARGES=1`. Both stay off.
5. **Write allowlist.** `FR_WRITE_CUSTOMER_IDS=10000` lets writes through only for those customers; the server resolves appointment, subscription, note and task IDs back to their customer and refuses anything it can't map. This is how you test full write capability against the live office without touching a real customer.
6. **Confirmation.** Tools that cancel, complete, freeze a subscription or overwrite Red Notes tell Claude to confirm with you first.
7. **Attribution.** Every write carries `FR_DEFAULT_EMPLOYEE_ID`, so bot activity is visible in FieldRoutes' changelog.
8. **Quota.** FieldRoutes allows 3,000 reads and 3,000 writes per office per day, shared with every other integration. The server counts its own calls, reports them in `health_check`, and refuses non-essential calls just short of the limit rather than letting FieldRoutes lock the office out. The counter resets at midnight in `TZ` and on restart.

The server never logs request paths (they'd contain the secret) or request bodies (they'd contain the API key).

## Deploy on Railway

1. **FieldRoutes:** Admin → API → Manage Keys. Create a key for this server. Note the office ID (visible in `health_check` later), your employee ID (or a dedicated "Claude" employee), and the Note Type ID you want for notes (Admin → Preferences → Note Types; there's no API for these). Create a test customer.
2. **Railway:** New Project → Deploy from GitHub repo → this repo. Railway reads `railway.json` and builds the Dockerfile.
3. **Variables:** paste `.env.example` into Railway → Variables and fill in. Minimum: `FR_AUTH_KEY`, `FR_AUTH_TOKEN`, `FR_SUBDOMAIN`, `MCP_PATH_SECRET` (generate with `python -c "import secrets; print(secrets.token_urlsafe(24))"`), `TZ`. For the first deploy set `FR_WRITE_CUSTOMER_IDS` to your test customer's ID.
4. **Settings:** Networking → Generate Domain. Turn **App Sleeping off**; a cold start makes the connector's first call time out.
5. Open `https://<domain>/healthz`. It should say `ok`.

### Connect

- **claude.ai / Cowork:** Settings → Connectors → Add custom connector. URL: `https://<domain>/<MCP_PATH_SECRET>/mcp`. No auth.
- **Claude Desktop:** same URL via `mcp-remote`, or run locally over stdio:

```json
{
  "mcpServers": {
    "fieldroutes": {
      "command": "/path/to/.venv/bin/fr-mcp",
      "env": { "FR_AUTH_KEY": "...", "FR_AUTH_TOKEN": "...", "FR_SUBDOMAIN": "zest" }
    }
  }
}
```

## First-run checklist

Nothing here has been verified against a live tenant yet; it was built from FieldRoutes' published spec and a fake API. Run these in order, in a Claude chat, with `FR_WRITE_CUSTOMER_IDS` set to the test customer.

1. `health_check`. Confirms credentials, shows the office ID and quota usage. Put the office ID in `FR_OFFICE_ID`.
2. `lookups`. Employees, service types, regions. Pick `FR_DEFAULT_EMPLOYEE_ID`.
3. `find_customer` for the test customer, then `customer_360` on it. Check the subscription block matches the FieldRoutes screen (frequency, billing frequency, next service, preferred tech).
4. `list_notes` on the test customer. **Does the Red Note appear here with a type?** If so, Red Notes are readable and `customer_alerts` can show them; tell the maintainer.
5. `day_schedule`, `crew_schedule`, `due_for_service`, `open_slots`. Compare a day against the FieldRoutes calendar.
6. `add_note` on the test customer. Check it shows up in FieldRoutes with the right type and author.
7. `update_subscription` on the test customer: change frequency, then read it back with `subscription_details`. **Did any field you didn't send change?** This is the partial-update check; if other fields blanked, stop and tell the maintainer.
8. `reserve_slot` then `schedule_appointment` on the test customer, far enough out that it doesn't land on a tech's day. Then `cancel_appointment`. If scheduling fails with a permission error, the API key needs "can schedule".
9. `set_red_notes` on the test customer, then look at the account in FieldRoutes.
10. Try a write on a real customer. It must be refused by the allowlist.

When all ten pass, remove `FR_WRITE_CUSTOMER_IDS` and the server is live.

## Environment variables

| Var | Purpose |
| --- | --- |
| `FR_AUTH_KEY`, `FR_AUTH_TOKEN` | FieldRoutes Admin → API → Manage Keys. |
| `FR_SUBDOMAIN` / `FR_BASE_URL` | Tenant host. `FR_BASE_URL` overrides, e.g. for `.pestroutes.com`. |
| `FR_OFFICE_ID` | Applied to every search unless the call scopes its own office. |
| `FR_DEFAULT_EMPLOYEE_ID` | Employee the API acts as for notes, tasks, cancels and completions. |
| `FR_DEFAULT_NOTE_TYPE_ID` | Note Type for `add_note`. |
| `FR_WRITES` | `on` (default) or `off`. |
| `FR_WRITE_CUSTOMER_IDS` | Comma-separated customer IDs writes are allowed for. Unset = all. |
| `FR_ALLOW_DELETE`, `FR_ALLOW_CHARGES` | Unlock deletes and real card charges. Leave at 0. |
| `FR_DAILY_READ_LIMIT`, `FR_DAILY_WRITE_LIMIT` | Quota the server holds itself to (default 3000 each). |
| `FR_EXPOSE_ENTITIES`, `FR_EXPOSE_ALL`, `FR_EXPOSE_VERBOSE` | Generated per-endpoint tools. |
| `MCP_TRANSPORT` | `http` for Railway; unset for stdio. |
| `PORT`, `MCP_PATH_SECRET`, `MCP_BEARER_TOKEN` | HTTP hosting and access control. |
| `TZ` | Office time zone, e.g. `America/Los_Angeles`. |

## Development

```
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest                                  # fake-API tests, no credentials needed
MCP_TRANSPORT=http FR_AUTH_KEY=x FR_AUTH_TOKEN=y FR_SUBDOMAIN=z MCP_PATH_SECRET=s fr-mcp
curl localhost:8080/healthz             # -> ok
```

`scripts/extract_spec.py` regenerates `src/fr_mcp/fieldroutes_spec.json` from FieldRoutes' public Swagger. Run it if they change their API. `CLAUDE.md` has the conventions for adding a tool.

The Docker build installs from `requirements.lock`, not `pyproject.toml`'s open ranges, so a rebuild months from now can't silently pick up a breaking dependency release. Regenerate it deliberately when you want to bump versions:

```
python -m venv /tmp/lockenv && /tmp/lockenv/bin/pip install -e . && /tmp/lockenv/bin/pip freeze > requirements.lock
```
