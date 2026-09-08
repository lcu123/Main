"""Google Sheet destination: the system of record for prospects (plan section 5.1).

Unlike `fr_push.py` (FieldRoutes, used only at the "inspection booked" handoff),
this is where every scored candidate from every source (`arcgis.py`, `myhd.py`,
eventually the processor list) actually lands day to day. Two BDRs work the
Leads tab directly, so the contract that matters most here is column ownership:
`TOOL_COLUMNS` is everything this module may write, `REP_COLUMNS` is everything
it must never touch once a row exists (the one deliberate exception is `status`,
set to "new" on a row's first append -- plan 5.1's own "Update rules").

`SheetBackend` is a small interface (`ensure_tab`/`read_rows`/`append_rows`/
`update_row`) so `sync_leads`'s actual logic -- dedupe by key, honour DNC, flag
a known row's new signal without touching rep columns, batch every write until
the end of a run -- is unit-testable against `FakeSheetBackend` with no live
Google credentials, the same test-double pattern `tests/conftest.py`'s `FakeFR`
uses for FieldRoutes. `GspreadBackend` is the real implementation, built once
the owner's service-account key is wired up (see README).
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import regions
from .fr_push import ConfigError
from .pipeline import LeadCandidate

LEADS_TAB = "Leads"
SIGNALS_TAB = "Signals"
RUNS_TAB = "Runs"
DNC_TAB = "DNC"

DEFAULT_NEW_ROW_CAP = 40  # plan 13, decision 8

# Tool-owned columns (plan 5.1's table) -- sync_leads is the only writer of these.
TOOL_COLUMNS = [
    "key", "county", "lane", "tier", "score",
    "facility", "permit types", "address", "city", "zip", "region", "distance (mi)",
    "pest", "evidence quote", "signal date", "signal result", "report link",
    "prior vermin flags (24 mo)", "new-signal flag", "signal count",
    "owner name", "owner type",
    "phone", "phone source", "business phone", "phone flag",
    "email", "email source", "email confidence",
    "website", "business status", "first seen", "last updated",
]

# Rep-owned columns -- sync_leads never writes any of these on an update. `status`
# is the one field it sets, but only at append time (plan 5.1: "a new facility
# appends a row with status `new`"); every re-run of an existing row leaves it alone.
# The live sheet's header is authoritative for column *order* (the owner has moved
# columns around; GspreadBackend looks positions up by name on every write), and
# any column the reps add that isn't listed here is simply never written.
REP_COLUMNS = [
    "rep", "status", "last touch date", "next step date", "touch count", "notes",
    "followup", "inspection date", "outcome", "FieldRoutes customer ID",
]

LEADS_COLUMNS = TOOL_COLUMNS + REP_COLUMNS
SIGNALS_COLUMNS = ["key", "guid", "date", "result", "pest", "quote", "report url", "recorded at"]
RUNS_COLUMNS = ["run at", "rows added", "rows flagged", "skipped dnc", "skipped cap", "errors"]
DNC_COLUMNS = ["key", "phone", "email", "reason", "added at"]  # tool reads this tab, never writes it


# --- backend interface -------------------------------------------------------


class SheetBackend(ABC):
    @abstractmethod
    def ensure_tab(self, tab: str, columns: list[str]) -> None:
        """Create `tab` with this header row if it doesn't exist yet. Never
        rewrites an existing tab's header (a rep may have added a column)."""

    @abstractmethod
    def read_rows(self, tab: str) -> list[dict[str, str]]:
        """Every data row (header excluded), as {column_name: value}, in sheet
        order -- position `i` here is data-row-index `i` for `update_row`."""

    @abstractmethod
    def append_rows(self, tab: str, rows: list[dict[str, Any]]) -> None:
        """Append rows at the end of `tab`. Each dict may be partial; any column
        in the tab's header not present in a given row is written blank."""

    @abstractmethod
    def update_row(self, tab: str, row_index: int, values: dict[str, Any]) -> None:
        """Overwrite only the named columns in the data row at 0-based
        `row_index` (0 = the first row under the header)."""

    def update_rows(self, tab: str, updates: list[tuple[int, dict[str, Any]]]) -> None:
        """Apply many row updates at once. The default walks `update_row`, which
        is fine in memory; a backend talking to a rate-limited API is expected to
        override this with a single batched write (see GspreadBackend)."""
        for row_index, values in updates:
            self.update_row(tab, row_index, values)

    def ensure_columns(self, tab: str, columns: list[str]) -> list[str]:
        """Append any of `columns` the tab's header doesn't have yet, at the far
        right, and return the ones added. Writes are addressed by column *name*,
        so a tool-owned column the live sheet has never heard of is silently
        dropped -- which is what happens the first time a new field ships. Adding
        at the right rather than in TOOL_COLUMNS order deliberately leaves the
        owner's own column arrangement untouched."""
        return []


class FakeSheetBackend(SheetBackend):
    """In-memory stand-in for a real spreadsheet -- mirrors gspread's shape
    closely enough (header-keyed rows, append-only growth, partial-column
    updates) that `sync_leads` doesn't need to know which one it's talking to."""

    def __init__(self) -> None:
        self.columns: dict[str, list[str]] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {}

    def ensure_tab(self, tab: str, columns: list[str]) -> None:
        if tab not in self.columns:
            self.columns[tab] = list(columns)
            self.rows[tab] = []

    def read_rows(self, tab: str) -> list[dict[str, str]]:
        return [dict(r) for r in self.rows.get(tab, [])]

    def append_rows(self, tab: str, rows: list[dict[str, Any]]) -> None:
        columns = self.columns[tab]
        for r in rows:
            self.rows[tab].append({c: r.get(c, "") for c in columns})

    def update_row(self, tab: str, row_index: int, values: dict[str, Any]) -> None:
        self.rows[tab][row_index].update(values)

    def ensure_columns(self, tab: str, columns: list[str]) -> list[str]:
        header = self.columns.setdefault(tab, [])
        added = [c for c in columns if c not in header]
        header.extend(added)
        for row in self.rows.get(tab, []):
            for c in added:
                row.setdefault(c, "")
        return added


class GspreadBackend(SheetBackend):
    """Wraps a `gspread.Spreadsheet`. Column position is looked up from each
    tab's own header row (never assumed), so a rep manually reordering rep-
    owned columns doesn't misalign a tool-owned write."""

    def __init__(self, spreadsheet: Any) -> None:
        self._ss = spreadsheet
        self._ws_cache: dict[str, Any] = {}

    def _worksheet(self, tab: str) -> Any:
        import gspread

        if tab not in self._ws_cache:
            try:
                self._ws_cache[tab] = self._ss.worksheet(tab)
            except gspread.WorksheetNotFound:
                self._ws_cache[tab] = None
        return self._ws_cache[tab]

    def header(self, tab: str) -> list[str]:
        ws = self._worksheet(tab)
        return ws.row_values(1) if ws is not None else []

    def ensure_tab(self, tab: str, columns: list[str]) -> None:
        ws = self._worksheet(tab)
        if ws is None:
            ws = self._ss.add_worksheet(title=tab, rows=1000, cols=max(len(columns), 10))
            ws.update([columns], "A1")
            self._ws_cache[tab] = ws

    def ensure_columns(self, tab: str, columns: list[str]) -> list[str]:
        ws = self._worksheet(tab)
        if ws is None:
            return []
        header = ws.row_values(1)
        added = [c for c in columns if c not in header]
        if not added:
            return []
        if ws.col_count < len(header) + len(added):
            ws.add_cols(len(header) + len(added) - ws.col_count)
        import gspread.utils

        first = gspread.utils.rowcol_to_a1(1, len(header) + 1)
        last = gspread.utils.rowcol_to_a1(1, len(header) + len(added))
        ws.update([added], f"{first}:{last}")
        return added

    def read_rows(self, tab: str) -> list[dict[str, str]]:
        """Built from raw values rather than gspread's `get_all_records()`, which
        raises outright on a duplicated header -- and a duplicate is something a rep
        can create by copying a column, which must never take the morning run down
        (verified live 2026-09-08: a duplicated "report link"/"notes" pair crashed a
        whole run before it wrote anything). First occurrence of a name wins, which
        is the same rule `update_row`'s `header.index()` follows."""
        ws = self._worksheet(tab)
        if ws is None:
            return []
        values = ws.get_all_values()
        if not values:
            return []
        header = values[0]
        rows = []
        for raw in values[1:]:
            row: dict[str, str] = {}
            for name, value in zip(header, raw):
                row.setdefault(name, value)
            rows.append(row)
        return rows

    def append_rows(self, tab: str, rows: list[dict[str, Any]]) -> None:
        ws = self._worksheet(tab)
        if ws is None or not rows:
            return
        header = ws.row_values(1)
        values = [[r.get(c, "") for c in header] for r in rows]
        ws.append_rows(values, value_input_option="USER_ENTERED")

    def update_row(self, tab: str, row_index: int, values: dict[str, Any]) -> None:
        self.update_rows(tab, [(row_index, values)])

    def update_rows(self, tab: str, updates: list[tuple[int, dict[str, Any]]]) -> None:
        """One `batch_update` for every cell in every row. Sheets allows 60 write
        requests per minute per user, and a cell-at-a-time loop blew straight
        through that backfilling 40 rows (verified live 2026-09-08: HTTP 429 partway
        through, leaving the sheet half-written)."""
        ws = self._worksheet(tab)
        if ws is None or not updates:
            return
        import gspread.utils

        header = ws.row_values(1)
        body = []
        for row_index, values in updates:
            sheet_row = row_index + 2  # +1 for the header row, +1 for 1-based indexing
            for col_name, val in values.items():
                if col_name not in header:
                    continue
                cell = gspread.utils.rowcol_to_a1(sheet_row, header.index(col_name) + 1)
                body.append({"range": cell, "values": [[val]]})
        if body:
            ws.batch_update(body, value_input_option="USER_ENTERED")


def open_backend() -> GspreadBackend:
    """LEADS_SHEET_ID plus a service-account credential, either the key file's
    JSON pasted inline (GOOGLE_SERVICE_ACCOUNT_JSON -- Railway's variables UI has
    no separate secret-file mechanism) or a path to it on disk
    (GOOGLE_SERVICE_ACCOUNT_JSON_PATH, for local/dev use)."""
    import gspread

    sheet_id = os.environ.get("LEADS_SHEET_ID", "").strip()
    if not sheet_id:
        raise ConfigError("LEADS_SHEET_ID is not set.")
    inline = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "").strip()
    if inline:
        try:
            info = json.loads(inline)
        except ValueError as exc:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc
        client = gspread.service_account_from_dict(info)
    elif path:
        client = gspread.service_account(filename=path)
    else:
        raise ConfigError("Set GOOGLE_SERVICE_ACCOUNT_JSON (the key file's JSON, inline) or GOOGLE_SERVICE_ACCOUNT_JSON_PATH.")
    spreadsheet = client.open_by_key(sheet_id)
    return GspreadBackend(spreadsheet)


# --- row shaping ---------------------------------------------------------


def _phone_flag(candidate: LeadCandidate) -> str:
    """Warn a rep off a county number that almost certainly is not the business.
    Audited live: out-of-area county numbers were wrong in every case checked."""
    phone = candidate.best_phone
    if not phone or regions.is_local_number(phone):
        return ""
    return "out-of-area number -- use business phone" if candidate.business_phone else "out-of-area number -- verify"


def _owner_type(header: Any) -> str:
    if not header or not header.owner:
        return ""
    return "entity" if header.is_entity else "person"


def _leads_row(candidate: LeadCandidate, *, today: date) -> dict[str, Any]:
    header = candidate.header
    classification = candidate.classification
    return {
        "key": candidate.customer_link,
        "county": candidate.county,
        "lane": candidate.lane,
        "tier": candidate.score.tier,
        "score": candidate.score.total,
        "facility": candidate.name,
        "permit types": ", ".join(candidate.permits),
        "address": candidate.street,
        "city": candidate.city,
        "zip": candidate.zip5,
        "region": candidate.region_name or "",
        "distance (mi)": round(candidate.distance_miles, 1) if candidate.distance_miles is not None else "",
        "pest": classification.label if classification and classification.label != "unclassified" else "",
        "evidence quote": classification.quote if classification else "",
        "signal date": candidate.signal.date.isoformat() if candidate.signal else "",
        "signal result": candidate.signal.result if candidate.signal else "",
        "report link": candidate.signal.report_url if candidate.signal else "",
        # Not yet plumbed through LeadCandidate (arcgis.Facility.vermin_count_24mo
        # and myhd's per-row history both stop short of this) -- left blank rather
        # than guessed; a rep can see the count in the Signals tab meanwhile.
        "prior vermin flags (24 mo)": "",
        "new-signal flag": "yes" if candidate.signal else "",
        "signal count": 1 if candidate.signal else 0,
        "owner name": header.owner if header and header.owner else "",
        "owner type": _owner_type(header),
        "phone": candidate.best_phone or "",
        "phone source": candidate.best_phone_source or "",
        "business phone": candidate.business_phone or "",
        "phone flag": _phone_flag(candidate),
        "email": candidate.email or "",
        "email source": candidate.email_source or "",
        "email confidence": "high" if candidate.email_source == "yolo_pdf" else "",
        "website": candidate.website or "",
        "business status": candidate.business_status or "",
        "first seen": today.isoformat(),
        "last updated": today.isoformat(),
        "status": "new",
    }


def _evidence_update(candidate: LeadCandidate, *, existing_signal_count: str, today: date) -> dict[str, Any]:
    assert candidate.signal is not None
    try:
        count = int(str(existing_signal_count).strip() or 0)
    except ValueError:
        count = 0
    classification = candidate.classification
    return {
        "tier": candidate.score.tier,
        "score": candidate.score.total,
        "pest": classification.label if classification and classification.label != "unclassified" else "",
        "evidence quote": classification.quote if classification else "",
        "signal date": candidate.signal.date.isoformat(),
        "signal result": candidate.signal.result,
        "report link": candidate.signal.report_url,
        "new-signal flag": "yes",
        "signal count": count + 1,
        "last updated": today.isoformat(),
    }


CONTACT_COLUMNS = (
    "owner name", "owner type", "phone", "phone source", "business phone", "phone flag",
    "email", "email source", "email confidence", "website", "business status",
)


def _blank(value: Any) -> bool:
    return not str(value if value is not None else "").strip()


def _contact_update(candidate: LeadCandidate, existing: dict[str, Any], *, today: date) -> dict[str, Any]:
    """Fill contact columns that are still blank on a known row from what this
    run's candidate carries (a report fetched this time that wasn't last time,
    a Yolo email). Never overwrites a value already there -- a rep may have
    typed a better number by hand into these columns, and the tool losing it
    would be worse than the tool never filling it."""
    fresh = _leads_row(candidate, today=today)
    update = {c: fresh[c] for c in CONTACT_COLUMNS if _blank(existing.get(c)) and not _blank(fresh[c])}
    if update:
        # Reachability is part of the score, so a newly found phone moves it.
        update["tier"] = candidate.score.tier
        update["score"] = candidate.score.total
        update["last updated"] = today.isoformat()
    return update


def _signal_row(candidate: LeadCandidate, *, today: date) -> dict[str, Any]:
    s = candidate.signal
    assert s is not None
    classification = candidate.classification
    return {
        "key": candidate.customer_link,
        "guid": s.pkey,
        "date": s.date.isoformat(),
        "result": s.result,
        "pest": classification.label if classification else "",
        "quote": classification.quote if classification else "",
        "report url": s.report_url,
        "recorded at": today.isoformat(),
    }


def _run_row(result: "SyncResult", *, today: date) -> dict[str, Any]:
    return {
        "run at": today.isoformat(),
        "rows added": result.added,
        "rows flagged": result.flagged,
        "skipped dnc": result.skipped_dnc,
        "skipped cap": result.skipped_cap,
        "errors": "; ".join(result.errors),
    }


def existing_keys(backend: SheetBackend) -> set[str]:
    """Every key already present as a Leads row."""
    return {(r.get("key") or "").strip() for r in backend.read_rows(LEADS_TAB) if (r.get("key") or "").strip()}


def keys_with_business_phone(backend: SheetBackend) -> set[str]:
    """Keys already carrying a Places-sourced business number. Enrichment is the
    only billed step in the pipeline, so a row that has one must not be looked up
    again on tomorrow's run."""
    return {
        (r.get("key") or "").strip()
        for r in backend.read_rows(LEADS_TAB)
        if (r.get("key") or "").strip() and str(r.get("business phone") or "").strip()
    }


def _duplicate_tool_columns(backend: SheetBackend, tab: str) -> set[str]:
    """Tool-owned column names appearing more than once in `tab`'s header. Only
    meaningful against a real spreadsheet, so a backend that can't report its
    header (the in-memory fake, whose columns are a list the tool itself set)
    reports none."""
    header = getattr(backend, "header", None)
    if not callable(header):
        return set()
    names = header(tab)
    return {n for n in TOOL_COLUMNS if names.count(n) > 1}


def _dnc_sets(rows: list[dict[str, str]]) -> tuple[set[str], set[str], set[str]]:
    keys = {r.get("key", "").strip() for r in rows if r.get("key", "").strip()}
    phones = {r.get("phone", "").strip() for r in rows if r.get("phone", "").strip()}
    emails = {r.get("email", "").strip().lower() for r in rows if r.get("email", "").strip()}
    return keys, phones, emails


def _is_dnc(candidate: LeadCandidate, dnc_keys: set[str], dnc_phones: set[str], dnc_emails: set[str]) -> bool:
    if candidate.customer_link in dnc_keys:
        return True
    if candidate.best_phone and candidate.best_phone in dnc_phones:
        return True
    if candidate.email and candidate.email.strip().lower() in dnc_emails:
        return True
    return False


# --- sync ------------------------------------------------------------------


@dataclass
class SyncResult:
    added: int = 0
    flagged: int = 0
    enriched: int = 0  # known rows that gained contact info (owner/phone/email) this run
    skipped_dnc: int = 0
    skipped_cap: int = 0
    skipped_duplicate: int = 0
    skipped_no_change: int = 0
    errors: list[str] = field(default_factory=list)


def sync_leads(
    backend: SheetBackend,
    candidates: list[LeadCandidate],
    *,
    today: date,
    new_row_cap: int = DEFAULT_NEW_ROW_CAP,
    dry_run: bool = False,
) -> SyncResult:
    """Append new facilities, flag known ones with a genuinely new signal, skip
    DNC and duplicates, and write nothing at all until the very end -- a crash
    partway through this function leaves the sheet exactly as it was (plan 5.1).

    Idempotency: a facility already in the Leads tab with no signal (a territory-
    lane row, or an event-lane row whose signal was already recorded in the
    Signals tab) is a no-op, the same "create once, then leave alone" contract
    `fr_push.push_candidate`'s territory-lane branch uses for FieldRoutes.
    """
    backend.ensure_tab(LEADS_TAB, LEADS_COLUMNS)
    backend.ensure_columns(LEADS_TAB, TOOL_COLUMNS)
    backend.ensure_tab(SIGNALS_TAB, SIGNALS_COLUMNS)
    backend.ensure_tab(RUNS_TAB, RUNS_COLUMNS)
    backend.ensure_tab(DNC_TAB, DNC_COLUMNS)

    leads_rows = backend.read_rows(LEADS_TAB)
    signals_rows = backend.read_rows(SIGNALS_TAB)
    dnc_rows = backend.read_rows(DNC_TAB)

    result = SyncResult()
    # A duplicated tool-owned header is survivable but not silent: an append fills
    # every copy, an update only ever touches the first, so the later copies go
    # stale and the sheet quietly disagrees with itself.
    for name in sorted(_duplicate_tool_columns(backend, LEADS_TAB)):
        result.errors.append(
            f'the Leads tab has more than one "{name}" column -- only the leftmost is kept up to date; delete the extras'
        )

    key_to_index = {r.get("key", "").strip(): i for i, r in enumerate(leads_rows) if r.get("key", "").strip()}
    seen_signals = {(r.get("key", ""), r.get("guid", "")) for r in signals_rows}
    dnc_keys, dnc_phones, dnc_emails = _dnc_sets(dnc_rows)

    pending_appends: list[dict[str, Any]] = []
    pending_updates: list[tuple[int, dict[str, Any]]] = []
    pending_signals: list[dict[str, Any]] = []
    new_rows_this_run = 0

    for c in candidates:
        if not c.pushable:
            continue
        if _is_dnc(c, dnc_keys, dnc_phones, dnc_emails):
            result.skipped_dnc += 1
            continue
        existing_index = key_to_index.get(c.customer_link)
        if existing_index is None:
            if new_rows_this_run >= new_row_cap:
                result.skipped_cap += 1
                continue
            pending_appends.append(_leads_row(c, today=today))
            if c.signal:
                pending_signals.append(_signal_row(c, today=today))
            new_rows_this_run += 1
            result.added += 1
            continue
        existing_row = leads_rows[existing_index]
        update = _contact_update(c, existing_row, today=today)
        if update:
            result.enriched += 1
        if not c.signal:
            if update:
                pending_updates.append((existing_index, update))
            else:
                result.skipped_no_change += 1
            continue
        if (c.customer_link, c.signal.pkey) in seen_signals:
            if update:
                pending_updates.append((existing_index, update))
            else:
                result.skipped_duplicate += 1
            continue
        update.update(_evidence_update(c, existing_signal_count=existing_row.get("signal count", "0"), today=today))
        pending_updates.append((existing_index, update))
        pending_signals.append(_signal_row(c, today=today))
        result.flagged += 1

    if not dry_run:
        if pending_appends:
            backend.append_rows(LEADS_TAB, pending_appends)
        if pending_updates:
            backend.update_rows(LEADS_TAB, pending_updates)
        if pending_signals:
            backend.append_rows(SIGNALS_TAB, pending_signals)
        backend.append_rows(RUNS_TAB, [_run_row(result, today=today)])

    return result
