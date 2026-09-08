"""Dedupe against FieldRoutes and push a scored lead candidate: `customer/create`
for a new lead, or a note + task for a re-touch (a known facility with a new
signal) or an upsell (the facility is already a real, active customer).

Every write goes through the same guards the curated MCP tools use --
`server._require_writes`, `server._require_customer_allowed`, the daily quota
built into `server.client()` -- by calling `fr_mcp.server`'s helpers directly
rather than re-implementing them, so this package inherits the repo's safety
model instead of maintaining a second copy of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError

from fr_mcp import server
from fr_mcp.client import FieldRoutesError

from .pipeline import LANE_EVENT, LANE_TERRITORY, LeadCandidate

DEFAULT_DAILY_CAP = 15
DEFAULT_TERRITORY_CAP = 5
DEFAULT_MAX_RUN_WRITES = 100
DEFAULT_RETOUCH_COOLDOWN_DAYS = 14


class ConfigError(RuntimeError):
    """A required LEADS_* setting is missing or invalid -- checked before any write
    (plan section 7's "fail-closed startup checks")."""


def _env_int(name: str, default: int | None = None) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} is not a valid integer") from exc


def source_id() -> int:
    v = _env_int("LEADS_SOURCE_ID")
    if v is None:
        raise ConfigError(
            'LEADS_SOURCE_ID is not set -- create a customer source (e.g. "Health Dept Inspections") in '
            "FieldRoutes Admin > Preferences > Customer Sources, then set LEADS_SOURCE_ID to its ID."
        )
    return v


def task_category_id() -> int:
    v = _env_int("LEADS_TASK_CATEGORY_ID") or server._default_task_category()
    if v is None:
        raise ConfigError(
            "LEADS_TASK_CATEGORY_ID (or FR_DEFAULT_TASK_CATEGORY_ID) is not set -- FieldRoutes requires a "
            'real office Task Category (lookups(kind="task_categories") lists the ones in use).'
        )
    return v


def note_type_id() -> int:
    v = _env_int("LEADS_NOTE_TYPE_ID") or server._default_note_type()
    if v is None:
        raise ConfigError("LEADS_NOTE_TYPE_ID (or FR_DEFAULT_NOTE_TYPE_ID) is not set.")
    return v


def assign_to() -> int | None:
    return _env_int("LEADS_ASSIGN_TO") or server._default_employee()


def daily_cap() -> int:
    return _env_int("LEADS_DAILY_CAP", DEFAULT_DAILY_CAP)


def territory_cap() -> int:
    return _env_int("LEADS_TERRITORY_CAP", DEFAULT_TERRITORY_CAP)


def max_run_writes() -> int:
    return _env_int("LEADS_MAX_RUN_WRITES", DEFAULT_MAX_RUN_WRITES)


def retouch_cooldown_days() -> int:
    return _env_int("LEADS_RETOUCH_COOLDOWN_DAYS", DEFAULT_RETOUCH_COOLDOWN_DAYS)


def check_config() -> None:
    """Raise ConfigError now, before the run does anything, rather than failing
    partway through after some leads already went out."""
    source_id()
    task_category_id()
    note_type_id()


# --- record shaping ----------------------------------------------------


def _split_owner_name(owner: str) -> tuple[str, str]:
    parts = owner.strip().split()
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    return "", owner.strip()


def build_customer_params(candidate: LeadCandidate) -> dict[str, Any]:
    header = candidate.header
    is_entity = bool(header and header.is_entity)
    fname, lname, spouse = "", candidate.name, None
    if header and header.owner and not is_entity:
        fname, lname = _split_owner_name(header.owner)
    elif header and header.owner and is_entity:
        spouse = f"Owner: {header.owner}"
    params: dict[str, Any] = {
        "companyName": candidate.name,
        "fname": fname,
        "lname": lname,
        "address": candidate.street,
        "city": candidate.city,
        "state": "CA",
        "zip": candidate.zip5,
        "county": candidate.county,
        "countryID": "US",
        "status": 0,
        "commercialAccount": 1,
        "sourceID": source_id(),
        "regionID": candidate.region_id,
        "customerLink": candidate.customer_link,
        "employeeID": server._default_employee(),
        "smsReminders": 0,
        "phoneReminders": 0,
        "emailReminders": 0,
    }
    if candidate.lat is not None and candidate.lng is not None:
        params["lat"] = candidate.lat
        params["lng"] = candidate.lng
    if candidate.best_phone:
        params["phone1"] = candidate.best_phone
    if candidate.email:
        params["email"] = candidate.email
    if spouse:
        params["spouse"] = spouse
    return params


def build_note_text(candidate: LeadCandidate, *, retouch: bool = False) -> str:
    parts: list[str] = []
    parts.append(
        f"New inspection signal on an existing lead ({candidate.county} County public record)."
        if retouch
        else f"COMMERCIAL PEST LEAD (auto-import, {candidate.county} County public record)."
    )
    parts.append(f"Score {candidate.score.total} {candidate.score.tier.upper()}.")
    parts.append(f"Type: {', '.join(candidate.permits) or 'unknown'}.")
    signal = candidate.signal
    if signal:
        evidence = candidate.classification.quote or signal.violation_description or signal.result
        parts.append(f'Evidence: {signal.date.isoformat()} {signal.inspection_type or "inspection"}, {signal.result}: "{evidence}"')
        if candidate.classification.label not in ("unclassified", ""):
            parts.append(f"Pest: {candidate.classification.label}.")
        if candidate.classification.no_pco:
            parts.append("No pest-control provider/invoice found on site.")
    else:
        parts.append("No violation on file -- audit-offer prospect.")
    if header := candidate.header:
        if header.owner:
            owner_line = f"Owner: {header.owner}."
            if header.phone:
                owner_line += f" Phone: {header.phone} (from report)."
            parts.append(owner_line)
    if signal:
        parts.append(f"Report: {signal.report_url}")
        if signal.pkey:
            parts.append(f"pKey: {signal.pkey}.")
    parts.append("Public record; internal only -- do not cite county data to the prospect.")
    return " ".join(parts)[:900]


def _due_date(candidate: LeadCandidate, today: date) -> date:
    if candidate.lane == LANE_TERRITORY:
        return today + timedelta(days=5)
    return {"hot": today, "warm": today + timedelta(days=2)}.get(candidate.score.tier, today + timedelta(days=5))


def build_task_text(candidate: LeadCandidate, *, retouch: bool = False) -> str:
    addr = ", ".join(p for p in (candidate.street, candidate.city) if p)
    if candidate.lane == LANE_TERRITORY:
        miles = f"{candidate.distance_miles:.1f} mi" if candidate.distance_miles is not None else "distance unknown"
        return (
            f"Audit offer: {candidate.name}, {addr}. No violation on file, independent "
            f"{', '.join(candidate.permits) or 'food facility'}, {miles} from office. "
            "Offer the free Commercial Rodent Risk Audit."
        )
    pest = candidate.classification.label if candidate.classification.label != "unclassified" else "vermin (pest not specified)"
    signal_date = candidate.signal.date.isoformat() if candidate.signal else "unknown date"
    verb = "New inspection signal on existing lead" if retouch else "Call"
    return (
        f"{verb}: {candidate.name}, {addr}. {pest.capitalize()} violation {signal_date}, "
        f"score {candidate.score.total} ({candidate.score.tier.upper()}). "
        "Offer the free Commercial Rodent Risk Audit. Details in today's note."
    )


def _extract_id(entity: str, resp: dict[str, Any]) -> int | None:
    """Which key FieldRoutes uses for a newly created row's ID is unverified for
    note/task specifically (this repo's own notes: a note's *write* param is
    `contactID`, not `noteID`; a task's *read* field is `taskIDs`, not `taskID`)
    -- try every plausible spelling rather than assume one and crash the run
    over a field we only use for logging, not for correctness."""
    for key in (f"{entity}ID", f"{entity}IDs", "contactID", "id"):
        if key in resp:
            try:
                return int(resp[key])
            except (TypeError, ValueError):
                pass
    return None


# --- dedupe --------------------------------------------------------------


async def find_existing_customer(candidate: LeadCandidate) -> dict[str, Any] | None:
    """customerLink exact match, then address+zip, then phone (plan 5.2)."""
    rows = await server._search_rows("customer", {"customerLink": candidate.customer_link})
    if rows:
        return rows[0]
    if candidate.street and candidate.zip5:
        house_and_street = " ".join(candidate.street.split()[:2])
        rows = await server._search_rows(
            "customer", {"zip": candidate.zip5, "address": {"operator": "CONTAINS", "value": house_and_street}}
        )
        if rows:
            return rows[0]
    phone = candidate.best_phone
    if phone and len(phone) == 10:
        rows = await server._search_rows("customer", {"phone": phone})
        if rows:
            return rows[0]
    return None


async def already_noted(customer_id: int, pkey: str) -> bool:
    if not pkey:
        return False
    notes = await server._search_rows("note", {"customerID": customer_id})
    return any(pkey in (n.get("notes") or "") for n in notes)


async def has_open_task(customer_id: int, added_by: int | None) -> bool:
    """"Open" means not yet resolved -- pending (0), in use (2), or urgent (3), the
    way server.TASK_STATUS defines it; a Hot lead's task is created with status 3, so
    filtering on status == 0 alone (an earlier version of this function did) would
    never find it and would stack a second task on every retouch."""
    filters: dict[str, Any] = {"customerID": customer_id}
    if added_by is not None:
        filters["addedBy"] = added_by
    rows = await server._search_rows("task", filters)
    return any(str(r.get("status")) not in ("1", "-1") for r in rows)


# --- writes ----------------------------------------------------------------


@dataclass
class PushResult:
    facility_id: str
    action: str
    customer_id: int | None = None
    note_id: int | None = None
    task_id: int | None = None
    detail: str = ""
    params: dict[str, Any] = field(default_factory=dict)


async def create_lead(candidate: LeadCandidate, *, today: date, dry_run: bool) -> PushResult:
    params = build_customer_params(candidate)
    if dry_run:
        return PushResult(candidate.facility_id, "would_create", params=params)
    server._require_writes("lead_push")
    # customer/create has no customerID yet to check against FR_WRITE_CUSTOMER_IDS --
    # fail closed the same way a curated tool would (server._resolve_write_customer
    # returns None for an unresolved write, and the allowlist then refuses it).
    server._require_customer_allowed("lead_push", None)
    resp = await server.client().call("customer", "create", params)
    customer_id = _extract_id("customer", resp)
    if customer_id is None:
        raise FieldRoutesError("customer/create: response carried no customerID", entity="customer", action="create")
    note_params = {
        "customerID": customer_id,
        "date": today.isoformat(),
        "contactType": note_type_id(),
        "notes": build_note_text(candidate),
        "showOnInvoice": 0,
        "showTech": 0,
        "showCustomer": 0,
        "employeeID": server._default_employee(),
    }
    note_resp = await server.client().call("note", "create", note_params)
    task_params: dict[str, Any] = {
        "type": 0,
        "customerID": customer_id,
        "task": build_task_text(candidate),
        "dueDate": _due_date(candidate, today).isoformat(),
        "status": 3 if candidate.score.tier == "hot" else 0,
        "assignedTo": assign_to(),
        "addedBy": server._default_employee(),
        "category": task_category_id(),
    }
    if candidate.best_phone:
        task_params["phone"] = candidate.best_phone
    task_resp = await server.client().call("task", "create", task_params)
    return PushResult(
        candidate.facility_id,
        "created",
        customer_id=customer_id,
        note_id=_extract_id("note", note_resp),
        task_id=_extract_id("task", task_resp),
    )


async def retouch(
    candidate: LeadCandidate, customer_id: int, *, today: date, dry_run: bool, is_real_customer: bool
) -> PushResult:
    action = "would_upsell" if (dry_run and is_real_customer) else "would_retouch" if dry_run else None
    if dry_run:
        return PushResult(candidate.facility_id, action, customer_id=customer_id)
    server._require_writes("lead_push")
    server._require_customer_allowed("lead_push", customer_id)
    note_params = {
        "customerID": customer_id,
        "date": today.isoformat(),
        "contactType": note_type_id(),
        "notes": build_note_text(candidate, retouch=True),
        "showOnInvoice": 0,
        "showTech": 0,
        "showCustomer": 0,
        "employeeID": server._default_employee(),
    }
    note_resp = await server.client().call("note", "create", note_params)
    task_id = None
    # Every task this pipeline creates has addedBy = the bot's own default employee
    # (see task_params below) -- that's the field to match on, not assign_to() (who
    # the task is *assigned to*, which is a different field and would never match).
    if not await has_open_task(customer_id, server._default_employee()):
        task_params: dict[str, Any] = {
            "type": 0,
            "customerID": customer_id,
            "task": build_task_text(candidate, retouch=True),
            "dueDate": _due_date(candidate, today).isoformat(),
            "status": 3 if candidate.score.tier == "hot" else 0,
            "assignedTo": assign_to(),
            "addedBy": server._default_employee(),
            "category": task_category_id(),
        }
        task_resp = await server.client().call("task", "create", task_params)
        task_id = _extract_id("task", task_resp)
    return PushResult(
        candidate.facility_id,
        "upsold" if is_real_customer else "retouched",
        customer_id=customer_id,
        note_id=_extract_id("note", note_resp),
        task_id=task_id,
    )


async def _adopt_customer_link(candidate: LeadCandidate, existing: dict[str, Any], customer_id: int) -> None:
    """A hit found via the address/phone fallback (not customerLink itself) gets
    customerLink backfilled, but only when it's currently empty -- never overwrite
    a value someone else set, and never touch a manually-created customer's link
    unless it has none (plan 5.2)."""
    if existing.get("customerLink"):
        return
    server._require_writes("lead_push")
    server._require_customer_allowed("lead_push", customer_id)
    await server.client().call("customer", "update", {"customerID": customer_id, "customerLink": candidate.customer_link})


async def push_candidate(candidate: LeadCandidate, *, today: date, dry_run: bool) -> PushResult:
    existing = await find_existing_customer(candidate)
    if existing is None:
        return await create_lead(candidate, today=today, dry_run=dry_run)
    customer_id = int(existing["customerID"])
    is_real_customer = str(existing.get("status")) == "1"
    if not dry_run:
        await _adopt_customer_link(candidate, existing, customer_id)
    if candidate.lane == LANE_TERRITORY:
        # A territory-lane lead has no new evidence to add once it exists -- one
        # create is the whole lifecycle; a daily re-run should see it and stop.
        return PushResult(candidate.facility_id, "skipped_already_lead", customer_id=customer_id)
    if candidate.signal and await already_noted(customer_id, candidate.signal.pkey):
        return PushResult(candidate.facility_id, "skipped_duplicate_signal", customer_id=customer_id)
    return await retouch(candidate, customer_id, today=today, dry_run=dry_run, is_real_customer=is_real_customer)


# --- run orchestration -----------------------------------------------------

CREATED_ACTIONS = {"created", "would_create"}
RETOUCHED_ACTIONS = {"retouched", "would_retouch", "upsold", "would_upsell"}
SKIPPED_ACTIONS = {"skipped_duplicate_signal", "skipped_already_lead"}
WRITES_PER_CREATE = 3  # customer + note + task
WRITES_PER_RETOUCH = 2  # note (+ task, usually)


@dataclass
class RunSummary:
    results: list[PushResult]
    created: int = 0
    retouched: int = 0
    skipped_cap: int = 0
    skipped_duplicate: int = 0
    errors: list[str] = field(default_factory=list)


async def push_run(candidates: list[LeadCandidate], *, today: date, dry_run: bool) -> RunSummary:
    """Push every pushable candidate, in the order given (callers should pass
    `pipeline.rank()`'s output), respecting the event/territory daily caps and
    the total-writes ceiling. Never raises for a single candidate's failure --
    that candidate is recorded as an error and the run continues, so one bad
    record can't stop the rest of the morning's leads."""
    if not dry_run:
        check_config()
    cap, terr_cap, write_ceiling = daily_cap(), territory_cap(), max_run_writes()
    summary = RunSummary(results=[])
    event_created = territory_created = writes_used = 0
    for c in candidates:
        if not c.pushable:
            continue
        if writes_used >= write_ceiling:
            summary.skipped_cap += 1
            summary.results.append(PushResult(c.facility_id, "skipped_write_ceiling"))
            continue
        if c.lane == LANE_EVENT and event_created >= cap:
            summary.skipped_cap += 1
            summary.results.append(PushResult(c.facility_id, "skipped_cap"))
            continue
        if c.lane == LANE_TERRITORY and territory_created >= terr_cap:
            summary.skipped_cap += 1
            summary.results.append(PushResult(c.facility_id, "skipped_cap"))
            continue
        try:
            result = await push_candidate(c, today=today, dry_run=dry_run)
        except (FieldRoutesError, ConfigError, ToolError) as exc:
            # ToolError is what _require_writes/_require_customer_allowed raise (the same
            # guards the curated MCP tools use) -- an allowlist refusal on one candidate
            # must not crash the rest of the run.
            summary.errors.append(f"{c.facility_id}: {exc}")
            continue
        summary.results.append(result)
        if result.action in CREATED_ACTIONS:
            summary.created += 1
            writes_used += WRITES_PER_CREATE
            if c.lane == LANE_EVENT:
                event_created += 1
            elif c.lane == LANE_TERRITORY:
                territory_created += 1
        elif result.action in RETOUCHED_ACTIONS:
            summary.retouched += 1
            writes_used += WRITES_PER_RETOUCH
        elif result.action in SKIPPED_ACTIONS:
            summary.skipped_duplicate += 1
    return summary
