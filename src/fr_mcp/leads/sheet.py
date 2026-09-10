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

import gspread
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Sequence

from . import institutions, regions
from .apartments import Complex, Manager
from .food import FoodFacility
from .fr_push import ConfigError
from .pipeline import LeadCandidate

LEADS_TAB = "Leads"
SIGNALS_TAB = "Signals"
RUNS_TAB = "Runs"
DNC_TAB = "DNC"

_DEFAULT_NEW_ROW_CAP = 40  # plan 13, decision 8
_DEFAULT_FOOD_ROW_CAP = 300  # the whole in-range universe is under this, so one pull covers it


def _cap(env_var: str, default: int) -> int:
    """A row cap, overridable per deployment. These were constants until 2026-09-09
    while the README already documented LEADS_SHEET_NEW_ROW_CAP as settable -- so
    setting it on Railway did exactly nothing, which is worse than not offering it."""
    raw = os.environ.get(env_var, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        return default
    return value if value >= 0 else default


DEFAULT_NEW_ROW_CAP = _cap("LEADS_SHEET_NEW_ROW_CAP", _DEFAULT_NEW_ROW_CAP)

# Tool-owned columns (plan 5.1's table) -- sync_leads is the only writer of these.
TOOL_COLUMNS = [
    "key", "county", "lane", "tier", "score",
    "facility", "permit types", "address", "city", "zip", "region", "distance (mi)",
    "pest", "evidence quote", "signal date", "signal result", "report link",
    "prior vermin flags (24 mo)", "new-signal flag", "signal count",
    "owner name", "owner type",
    "phone", "phone source", "business phone", "phone flag",
    "email", "email source", "email confidence",
    "website", "business status", "places checked", "facility class",
    "first seen", "last updated",
]

# Rep-owned columns -- sync_leads never writes any of these on an update. `status`
# is the one field it sets, but only at append time (plan 5.1: "a new facility
# appends a row with status `new`"); every re-run of an existing row leaves it alone.
# The live sheet's header is authoritative for column *order* (the owner has moved
# columns around; GspreadBackend looks positions up by name on every write), and
# any column the reps add that isn't listed here is simply never written.
REP_COLUMNS = [
    "rep", "status", "last touch date", "followup date", "touch count", "notes",
    "inspection date", "outcome", "FieldRoutes customer ID",
]

# Columns renamed after rows already existed. `reorder_leads_tab` carries the old
# column's values across, so a rep's entries survive the rename.
COLUMN_RENAMES = {"next step date": "followup date"}

# Columns retired after they existed in a live sheet. `reorder_leads_tab` removes
# these, unlike an unrecognised column, which it preserves on the assumption a rep
# added it. Only ever list a column we introduced ourselves and then thought
# better of -- never one of theirs.
RETIRED_COLUMNS = frozenset({"followup"})

# Display order, which is a different question from ownership above. A rep on the
# phone reads left to right and should never scroll to dial: who am I calling,
# what do I dial, what did I say last time, when am I calling back. Everything
# they need only once the call connects (the pest evidence, the address, the
# score) sits to the right of that. Ownership still governs what may be written:
# `notes` and `followup date` are the rep's, and the tool never touches them.
# `followup date` leads deliberately: sorting or filtering on column A turns the
# tab into today's call queue, which is the first thing a rep does each morning.
# When to call back, then who, what to dial, and what was said last time -- there
# is deliberately no separate "next action" column: it would overlap `notes` and
# two reps would fill the pair inconsistently.
DIALER_COLUMNS = ["followup date", "facility", "phone", "notes"]
_REMAINING = [c for c in TOOL_COLUMNS + REP_COLUMNS if c not in DIALER_COLUMNS]
LEADS_COLUMNS = DIALER_COLUMNS + [
    # Kept adjacent to the primary number: the fallback line and the warning that
    # the primary is not the business's.
    "business phone", "phone flag",
] + [c for c in _REMAINING if c not in ("business phone", "phone flag")]
SIGNALS_COLUMNS = ["key", "guid", "date", "result", "pest", "quote", "report url", "recorded at"]
RUNS_COLUMNS = ["run at", "rows added", "rows flagged", "skipped dnc", "skipped cap", "errors"]
DNC_COLUMNS = ["key", "phone", "email", "reason", "added at"]  # tool reads this tab, never writes it

# --- Food Facilities tab ------------------------------------------------------
#
# A second call list on the same spreadsheet: processors, bakeries, breweries,
# wineries, cold storage and food distribution, from the registry sources rather
# than the county inspection feeds. The `Leads` tab's DNC tab is shared -- a number
# a rep has been told not to call is not to be called from either list.

FOOD_TAB = "Food Facilities"
DEFAULT_FOOD_ROW_CAP = _cap("LEADS_FOOD_ROW_CAP", _DEFAULT_FOOD_ROW_CAP)

FOOD_TOOL_COLUMNS = [
    "key", "facility", "type of business", "phone", "phone source",
    "address", "city", "zip", "region", "distance (mi)", "distance basis",
    "sources", "found via", "needs review", "review reason",
    "website", "business status", "contact name", "in leads tab",
    "places checked", "facility class", "first seen", "last updated",
]

# Deliberately the same names the Leads tab uses for the rep's own columns. The
# reps work both tabs; two vocabularies for the same field is how a status ends up
# meaning different things in different places.
FOOD_REP_COLUMNS = [
    "rep", "status", "last touch date", "followup date", "touch count", "notes",
    "inspection date", "outcome", "FieldRoutes customer ID",
]

# Same dialer-first order as the Leads tab, by the owner's decision (plan 13,
# answer 1): the reps get one muscle memory rather than two. `type of business` and
# `city` follow immediately, because on this tab they are what tells a rep which
# pitch to open with -- a cold-storage warehouse and a micro-bakery are not the
# same call.
_FOOD_REMAINING = [
    c for c in FOOD_TOOL_COLUMNS + FOOD_REP_COLUMNS
    if c not in DIALER_COLUMNS and c not in ("type of business", "city")
]
FOOD_COLUMNS = DIALER_COLUMNS + ["type of business", "city"] + _FOOD_REMAINING

# --- Apartments and Property Managers ----------------------------------------
#
# Two tabs from one pull: the properties a rep can walk into, and the companies
# holding more than one of them. They share the spreadsheet's DNC tab and the
# same dialer-first column order as everything else.

APARTMENTS_TAB = "Apartments"
MANAGERS_TAB = "Property Managers"
DEFAULT_APARTMENT_ROW_CAP = 1200

APARTMENT_TOOL_COLUMNS = [
    "key", "facility", "phone", "on-site", "office hours", "size", "reviews",
    "managed by", "manager properties", "address", "city", "zip", "region",
    "distance (mi)", "website", "business status", "national operator",
    "first seen", "last updated",
]
MANAGER_TOOL_COLUMNS = [
    "key", "facility", "phone", "properties", "with leasing office", "cities",
    "website", "grouped by", "property names", "national operator",
    "first seen", "last updated",
]
# Same names the other tabs use for the rep's own columns -- the reps work all of
# them and two vocabularies for one field is how a status drifts in meaning.
APARTMENT_REP_COLUMNS = list(REP_COLUMNS)

_APT_REMAINING = [c for c in APARTMENT_TOOL_COLUMNS + APARTMENT_REP_COLUMNS if c not in DIALER_COLUMNS]
APARTMENT_COLUMNS = DIALER_COLUMNS + _APT_REMAINING
_MGR_REMAINING = [c for c in MANAGER_TOOL_COLUMNS + APARTMENT_REP_COLUMNS if c not in DIALER_COLUMNS]
MANAGER_COLUMNS = DIALER_COLUMNS + _MGR_REMAINING


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

    def header(self, tab: str) -> list[str]:
        """The tab's header row as stored, duplicates and all."""
        raise NotImplementedError

    def rewrite_tab(self, tab: str, header: list[str], rows: list[dict[str, Any]]) -> None:
        """Replace the tab wholesale with `header` and `rows`. Only for
        maintenance like a reorder -- the run loop must never use this, since a
        crash midway would leave the sheet truncated rather than untouched."""
        raise NotImplementedError

    def ensure_columns(self, tab: str, columns: list[str]) -> list[str]:
        """Append any of `columns` the tab's header doesn't have yet, at the far
        right, and return the ones added. Writes are addressed by column *name*,
        so a tool-owned column the live sheet has never heard of is silently
        dropped -- which is what happens the first time a new field ships. Adding
        at the right rather than in TOOL_COLUMNS order deliberately leaves the
        owner's own column arrangement untouched."""
        return []

    def shade_rows(self, tab: str, row_indices: list[int], rgb: tuple[float, float, float] | None) -> int:
        """Give whole data rows a background colour, or clear it with `rgb=None`.

        Colour is the one thing in this sheet a rep reads without reading -- it
        scans before it is parsed -- so it is worth having, but it is also the one
        thing that carries no meaning a later run can recover. Every shading here
        is therefore paired with a real column holding the same fact; the colour is
        the signal, the column is the record. Returns the number of rows shaded."""
        return 0


class FakeSheetBackend(SheetBackend):
    """In-memory stand-in for a real spreadsheet -- mirrors gspread's shape
    closely enough (header-keyed rows, append-only growth, partial-column
    updates) that `sync_leads` doesn't need to know which one it's talking to."""

    def __init__(self) -> None:
        self.columns: dict[str, list[str]] = {}
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.shading: dict[str, dict[int, tuple[float, float, float]]] = {}

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

    def header(self, tab: str) -> list[str]:
        return list(self.columns.get(tab, []))

    def rewrite_tab(self, tab: str, header: list[str], rows: list[dict[str, Any]]) -> None:
        self.columns[tab] = list(header)
        self.rows[tab] = [{c: r.get(c, "") for c in header} for r in rows]

    def ensure_columns(self, tab: str, columns: list[str]) -> list[str]:
        header = self.columns.setdefault(tab, [])
        added = [c for c in columns if c not in header]
        header.extend(added)
        for row in self.rows.get(tab, []):
            for c in added:
                row.setdefault(c, "")
        return added

    def shade_rows(self, tab: str, row_indices: list[int], rgb: tuple[float, float, float] | None) -> int:
        shaded = self.shading.setdefault(tab, {})
        for i in row_indices:
            if rgb is None:
                shaded.pop(i, None)
            else:
                shaded[i] = rgb
        return len(row_indices)


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

    def shade_rows(self, tab: str, row_indices: list[int], rgb: tuple[float, float, float] | None) -> int:
        ws = self._worksheet(tab)
        if ws is None or not row_indices:
            return 0
        requests = _shade_requests(ws.id, list(row_indices), len(self.header(tab)), rgb)
        if requests:
            # One batch, for the same reason every other write here is batched:
            # Sheets allows 60 write requests per minute per user.
            self._ss.batch_update({"requests": requests})
        return len(set(row_indices))

    def rewrite_tab(self, tab: str, header: list[str], rows: list[dict[str, Any]]) -> None:
        ws = self._worksheet(tab)
        if ws is None:
            return
        values = [header] + [[str(r.get(c, "")) for c in header] for r in rows]
        if ws.col_count < len(header):
            ws.add_cols(len(header) - ws.col_count)
        # Clear first: the new grid can be narrower than the old one (duplicates
        # dropped), and a plain update would leave the old trailing columns behind.
        ws.clear()
        ws.update(values, "A1")

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
        """Append below the last row that actually holds data.

        `gspread.append_rows` delegates to the Sheets `values.append` API, which
        picks its own insertion point by "detecting a table". On a tab whose
        leading columns are rep-owned and therefore blank, that detection is
        unreliable: appending two probe rows to the live Apartments tab left the
        row count unchanged while the probes themselves were present, i.e. it had
        overwritten real rows rather than adding to them.

        So the insertion point is computed here from the `key` column -- which is
        tool-owned and never blank on a real row -- and written with an explicit
        range instead of letting the API guess."""
        ws = self._worksheet(tab)
        if ws is None or not rows:
            return
        header = ws.row_values(1)
        import gspread.utils

        first_free = self._first_free_row(tab, header)
        values = [[r.get(c, "") for c in header] for r in rows]
        needed = first_free + len(values) - 1
        if needed > ws.row_count:
            ws.add_rows(needed - ws.row_count + 50)
        end = gspread.utils.rowcol_to_a1(first_free + len(values) - 1, len(header))
        start = gspread.utils.rowcol_to_a1(first_free, 1)
        ws.update(values, f"{start}:{end}", value_input_option="USER_ENTERED")

    def _first_free_row(self, tab: str, header: list[str]) -> int:
        """One past the last row carrying a key. 1-based, header included, so a
        tab with only a header returns 2."""
        ws = self._worksheet(tab)
        if ws is None:
            return 2
        if "key" not in header:
            return len(ws.get_all_values()) + 1
        column = ws.col_values(header.index("key") + 1)
        last = max((i for i, v in enumerate(column, start=1) if str(v).strip()), default=1)
        return last + 1

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


def _shade_requests(sheet_id: int, row_indices: list[int], width: int, rgb) -> list[dict]:
    """One `repeatCell` request per contiguous run of rows, so shading 40 scattered
    rows is a handful of requests rather than 40. Row indices are 0-based *data*
    rows; the grid is 0-based including the header, hence the +1."""
    fmt = {"backgroundColor": {"red": rgb[0], "green": rgb[1], "blue": rgb[2]}} if rgb else {}
    requests, runs = [], []
    for i in sorted(set(row_indices)):
        if runs and i == runs[-1][1]:
            runs[-1][1] = i + 1
        else:
            runs.append([i, i + 1])
    for start, end in runs:
        requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start + 1,
                        "endRowIndex": end + 1,
                        "startColumnIndex": 0,
                        "endColumnIndex": max(width, 1),
                    },
                    "cell": {"userEnteredFormat": fmt},
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        )
    return requests


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
    Audited live: out-of-area county numbers were wrong in every case checked.

    Unless Places lists the very same number, which settles it -- a business can
    legitimately publish an out-of-area line, and four rows in the first live
    backfill did. Flagging those would train reps to ignore the column."""
    phone = candidate.best_phone
    if not phone or regions.is_local_number(phone):
        return ""
    if candidate.business_phone == phone:
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
        "places checked": today.isoformat() if candidate.places_checked else "",
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
    "places checked",
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


def plan_reorder(header: list[str], wanted: list[str], retired: frozenset[str] | None = None) -> list[str]:
    """The header `reorder_leads_tab` should write: `wanted`'s order first (only
    the columns that exist or are ours to add), then every other existing column
    in its current order, with duplicates collapsed to their first occurrence.

    Columns the sheet has that we've never heard of are kept -- a rep may have
    added their own and it is not ours to delete. Duplicates are dropped because
    only the leftmost copy of a tool column is ever kept current, so a second one
    is a stale decoy."""
    retired = RETIRED_COLUMNS if retired is None else retired
    seen: set[str] = set()
    out: list[str] = []
    for name in list(wanted) + list(header):
        if name and name not in seen and name not in retired:
            seen.add(name)
            out.append(name)
    return [c for c in out if c in set(header) | set(wanted)]


def reorder_leads_tab(
    backend: SheetBackend, wanted: list[str] | None = None, renames: dict[str, str] | None = None
) -> dict[str, Any]:
    """Rewrite the Leads tab with its columns in `wanted` order, carrying every
    row's values with them. `renames` maps an old column name to its new one and
    moves the values too, so renaming a column a rep has been filling in does not
    throw their entries away. Returns what changed, for the caller to report."""
    wanted = wanted or LEADS_COLUMNS
    renames = COLUMN_RENAMES if renames is None else renames
    header = backend.header(LEADS_TAB)
    if not header:
        return {"reordered": False, "reason": "no header"}
    header = [renames.get(c, c) for c in header]
    rows = [{renames.get(k, k): v for k, v in row.items()} for row in backend.read_rows(LEADS_TAB)]
    new_header = plan_reorder(header, wanted)
    backend.rewrite_tab(LEADS_TAB, new_header, rows)
    return {
        "reordered": new_header != backend.header(LEADS_TAB) or new_header != header,
        "renamed": {k: v for k, v in renames.items() if v in new_header},
        "rows": len(rows),
        "duplicatesDropped": len(header) - len(set(header)),
        "added": [c for c in new_header if c not in header],
        "first": new_header[:4],
    }


def existing_keys(backend: SheetBackend) -> set[str]:
    """Every key already present as a Leads row."""
    return {(r.get("key") or "").strip() for r in backend.read_rows(LEADS_TAB) if (r.get("key") or "").strip()}


def keys_with_business_phone(backend: SheetBackend) -> set[str]:
    """Keys Places has already been paid for -- either it found a number, or it
    was asked and came back with nothing usable.

    Enrichment is the only billed step in the pipeline, and the second half of
    that condition is what stops it recurring: on 2026-09-09 the live sheet held
    45 rows with no business phone, every one of which had already been looked up
    and had spent the run's whole 50-call budget being looked up again. A row
    Places cannot resolve today is not more resolvable tomorrow -- the address is
    wrong, or the business has no listing -- so asking again every weekday buys
    nothing and costs real money. `places checked` is cleared by hand if a row is
    ever worth a second look."""
    keys = set()
    for r in backend.read_rows(LEADS_TAB):
        key = (r.get("key") or "").strip()
        if not key:
            continue
        if str(r.get("business phone") or "").strip() or str(r.get("places checked") or "").strip():
            keys.add(key)
    return keys


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


def _dnc_digits(raw: Any) -> str:
    """A DNC phone is typed by a rep, not produced by this tool, so it arrives
    however they wrote it -- "(916) 442-0771", "916-442-0771", "1 916 442 0771".
    Every phone this pipeline holds is bare digits, so comparing the two as typed
    silently blocks nothing at all. Reduce both sides to the same 10 digits."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _dnc_sets(rows: list[dict[str, str]]) -> tuple[set[str], set[str], set[str]]:
    keys = {r.get("key", "").strip() for r in rows if r.get("key", "").strip()}
    phones = {d for d in (_dnc_digits(r.get("phone")) for r in rows) if d}
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


# --- Food Facilities ----------------------------------------------------------


FOOD_CONTACT_COLUMNS = (
    "phone", "phone source", "website", "business status", "contact name", "places checked",
)


def _food_row(facility: FoodFacility, *, today: date, in_leads: bool) -> dict[str, Any]:
    return {
        "key": facility.key,
        "facility": facility.name,
        "type of business": facility.category,
        "phone": facility.phone,
        "phone source": facility.phone_source,
        "address": facility.address,
        "city": facility.city,
        "zip": facility.zip5,
        "region": facility.region_name or "",
        "distance (mi)": round(facility.distance_miles, 1) if facility.distance_miles is not None else "",
        # Says how much to trust the number beside it: "coords" is the facility's
        # own location, "zip"/"city" are a centroid standing in for one.
        "distance basis": facility.distance_basis,
        "sources": "; ".join(facility.sources),
        "found via": facility.found_via,
        "needs review": "yes" if facility.needs_review else "",
        "review reason": facility.review_reason,
        "website": facility.website,
        "business status": facility.business_status,
        "contact name": facility.contact_name,
        "in leads tab": "yes" if in_leads else "",
        "places checked": today.isoformat() if facility.places_checked else "",
        "first seen": today.isoformat(),
        "last updated": today.isoformat(),
        "status": "new",
    }


# Contact facts are backfilled only when blank -- a rep may have typed a better
# number by hand and losing that is worse than never filling it.
# Provenance is different: it is derived wholly from this run's sources, a rep
# never edits it, and it only ever grows as more registries corroborate a row. A
# row first found by CalEPA and later confirmed by the Google sweep must say so,
# and a guess a second source confirms must stop being flagged -- otherwise the
# review queue never shrinks and `sources` understates what is known.
FOOD_PROVENANCE_COLUMNS = ("sources", "found via", "needs review", "review reason", "type of business")


def _food_contact_update(facility: FoodFacility, existing: dict[str, Any], *, today: date) -> dict[str, Any]:
    fresh = _food_row(facility, today=today, in_leads=False)
    update = {c: fresh[c] for c in FOOD_CONTACT_COLUMNS if _blank(existing.get(c)) and not _blank(fresh[c])}
    update.update(
        {c: fresh[c] for c in FOOD_PROVENANCE_COLUMNS if str(existing.get(c) or "") != str(fresh[c])}
    )
    if update:
        update["last updated"] = today.isoformat()
    return update


def _leads_fingerprints(backend: SheetBackend) -> tuple[set[str], set[str]]:
    """(normalised name+zip, 10-digit phone) already present in the Leads tab.

    A facility in both tabs is kept and flagged rather than dropped (plan 13,
    answer 5) -- a processor that also holds a retail permit is still a processor
    lead -- but the rep has to be able to see it before opening with the wrong
    pitch."""
    names: set[str] = set()
    phones: set[str] = set()
    for row in backend.read_rows(LEADS_TAB):
        name = _food_name_key(row.get("facility", ""), row.get("zip", ""))
        if name:
            names.add(name)
        for column in ("phone", "business phone"):
            digits = "".join(ch for ch in str(row.get(column) or "") if ch.isdigit())
            if len(digits) == 10:
                phones.add(digits)
    return names, phones


def _food_name_key(name: str, zip5: str) -> str:
    from .food import normalize_name

    normalized = normalize_name(name)
    return f"{normalized}|{str(zip5).strip()[:5]}" if normalized else ""


def food_existing_keys(backend: SheetBackend) -> set[str]:
    return {
        (r.get("key") or "").strip()
        for r in backend.read_rows(FOOD_TAB)
        if (r.get("key") or "").strip()
    }


def food_keys_needing_phone(backend: SheetBackend) -> set[str]:
    """Keys on the tab that a billed Places lookup could still improve: no phone,
    and not already asked about. The second half matters as much as the first --
    a row Places declined to resolve once is not more resolvable tomorrow, and
    re-asking every run is how an optional enrichment becomes a standing bill
    (see `keys_with_business_phone` for the Leads tab's version of this)."""
    return {
        (r.get("key") or "").strip()
        for r in backend.read_rows(FOOD_TAB)
        if (r.get("key") or "").strip()
        and not str(r.get("phone") or "").strip()
        and not str(r.get("places checked") or "").strip()
    }


def sync_food_facilities(
    backend: SheetBackend,
    facilities: list[FoodFacility],
    *,
    today: date,
    new_row_cap: int = DEFAULT_FOOD_ROW_CAP,
    dry_run: bool = False,
) -> SyncResult:
    """Append new facilities, backfill blank contact columns on known ones, and
    write nothing until the end -- the same contract `sync_leads` follows, for the
    same reason (a crash partway leaves the sheet exactly as it was).

    Idempotency here is simpler than the Leads tab's: there are no per-inspection
    signals to record, so a facility already present with nothing new to add is a
    no-op. The key is the discovering registry's own record ID, which is stable
    across a licence renewal, so a rerun recognises the row rather than duplicating
    it.

    The `DNC` tab is shared with the Leads tab on purpose: a number a rep has been
    told not to call must not reappear on a second list."""
    backend.ensure_tab(FOOD_TAB, FOOD_COLUMNS)
    backend.ensure_columns(FOOD_TAB, FOOD_TOOL_COLUMNS)
    backend.ensure_tab(DNC_TAB, DNC_COLUMNS)
    backend.ensure_tab(RUNS_TAB, RUNS_COLUMNS)

    food_rows = backend.read_rows(FOOD_TAB)
    dnc_keys, dnc_phones, _ = _dnc_sets(backend.read_rows(DNC_TAB))
    leads_names, leads_phones = _leads_fingerprints(backend)

    result = SyncResult()
    for name in sorted(_duplicate_food_columns(backend)):
        result.errors.append(
            f'the {FOOD_TAB} tab has more than one "{name}" column -- only the leftmost is kept '
            "up to date; delete the extras"
        )

    key_to_index = {r.get("key", "").strip(): i for i, r in enumerate(food_rows) if r.get("key", "").strip()}
    pending_appends: list[dict[str, Any]] = []
    pending_updates: list[tuple[int, dict[str, Any]]] = []
    new_rows_this_run = 0

    for facility in facilities:
        if facility.key in dnc_keys or (facility.phone and facility.phone in dnc_phones):
            result.skipped_dnc += 1
            continue
        existing_index = key_to_index.get(facility.key)
        if existing_index is None:
            if new_rows_this_run >= new_row_cap:
                result.skipped_cap += 1
                continue
            in_leads = (
                _food_name_key(facility.name, facility.zip5) in leads_names
                or (facility.phone in leads_phones if facility.phone else False)
            )
            pending_appends.append(_food_row(facility, today=today, in_leads=in_leads))
            new_rows_this_run += 1
            result.added += 1
            continue
        update = _food_contact_update(facility, food_rows[existing_index], today=today)
        if update:
            pending_updates.append((existing_index, update))
            result.enriched += 1
        else:
            result.skipped_no_change += 1

    if not dry_run:
        if pending_appends:
            backend.append_rows(FOOD_TAB, pending_appends)
        if pending_updates:
            backend.update_rows(FOOD_TAB, pending_updates)
        backend.append_rows(RUNS_TAB, [_run_row(result, today=today)])

    return result


def _duplicate_food_columns(backend: SheetBackend) -> set[str]:
    header = getattr(backend, "header", None)
    if not callable(header):
        return set()
    names = header(FOOD_TAB)
    return {n for n in FOOD_TOOL_COLUMNS if names.count(n) > 1}


# --- institutional / medical / residential marking ---------------------------

# A muted red: legible behind black text, and distinct from the greens and yellows
# a rep may already be using for their own status colours.
INSTITUTION_RGB = (0.96, 0.80, 0.80)


def mark_institutions(
    backend: SheetBackend, tab: str, *, dry_run: bool = False
) -> dict[str, Any]:
    """Shade every nursing home, care facility, hospital, clinic, apartment complex
    and campus dining row red, and record what each one is in `facility class`.

    The colour and the column are deliberately paired. Colour is what a rep
    actually reads -- it registers before the row is parsed -- but it survives no
    round trip: nothing later can ask the sheet "which rows are care homes", and a
    rep who copies a row loses it. The column is the durable half.

    Re-runnable: a row that stops matching has its shading cleared and its class
    blanked, so a corrected facility name takes effect rather than leaving a stale
    red stripe behind."""
    rows = backend.read_rows(tab)
    tool_columns = FOOD_TOOL_COLUMNS if tab == FOOD_TAB else TOOL_COLUMNS
    if "facility class" in tool_columns:
        backend.ensure_columns(tab, ["facility class"])

    matched: dict[int, str] = {}
    cleared: list[int] = []
    for index, row in enumerate(rows):
        permits = [p.strip() for p in str(row.get("permit types") or "").split(",") if p.strip()]
        label = institutions.classify(row.get("facility", ""), permits)
        current = str(row.get("facility class") or "").strip()
        if label:
            matched[index] = label
        elif current:
            cleared.append(index)

    updates = [(i, {"facility class": label}) for i, label in matched.items()
               if str(rows[i].get("facility class") or "").strip() != label]
    updates += [(i, {"facility class": ""}) for i in cleared]

    if not dry_run:
        if updates:
            backend.update_rows(tab, updates)
        if matched:
            backend.shade_rows(tab, sorted(matched), INSTITUTION_RGB)
        if cleared:
            backend.shade_rows(tab, cleared, None)

    by_class: dict[str, int] = {}
    for label in matched.values():
        by_class[label] = by_class.get(label, 0) + 1
    return {
        "tab": tab,
        "rows": len(rows),
        "marked": len(matched),
        "unmarked": len(cleared),
        "byClass": by_class,
        "dryRun": dry_run,
    }


# --- Apartments / Property Managers sync -------------------------------------


APARTMENT_CONTACT_COLUMNS = (
    "phone", "website", "office hours", "on-site", "size", "reviews",
    "managed by", "manager properties", "business status",
)


def _apartment_row(c: Complex, *, today: date) -> dict[str, Any]:
    return {
        "key": c.key,
        "facility": c.name,
        "phone": c.phone,
        # What a rep finds if they turn up. Posted hours are a direct observation;
        # no public source carries the unit count this used to be inferred from.
        "on-site": c.onsite_tier,
        "office hours": c.hours,
        # A proxy for size, labelled as one -- review volume, not units.
        "size": c.size_hint,
        "reviews": c.review_count,
        "managed by": c.manager,
        "manager properties": c.manager_properties or "",
        "address": c.address,
        "city": c.city,
        "zip": c.zip5,
        "region": c.region_name or "",
        "distance (mi)": round(c.distance_miles, 1) if c.distance_miles is not None else "",
        "website": c.website,
        "business status": c.business_status,
        "national operator": "yes" if c.is_national else "",
        "first seen": today.isoformat(),
        "last updated": today.isoformat(),
        "status": "new",
    }


def _manager_row(m: Manager, *, today: date) -> dict[str, Any]:
    return {
        "key": m.key,
        "facility": m.name,
        "phone": m.phone,
        "properties": m.property_count,
        "with leasing office": m.with_office,
        "cities": ", ".join(m.cities),
        "website": m.website,
        # How the group was formed, so a rep can weigh it: a shared website is
        # strong, a shared phone line could be a shared answering service.
        "grouped by": m.basis,
        "property names": ", ".join(m.properties),
        "national operator": "yes" if m.is_national else "",
        "first seen": today.isoformat(),
        "last updated": today.isoformat(),
        "status": "new",
    }


def _sync_generic(
    backend: SheetBackend,
    tab: str,
    columns: list[str],
    tool_columns: list[str],
    rows_out: list[dict[str, Any]],
    refresh: Sequence[str],
    *,
    today: date,
    new_row_cap: int,
    dry_run: bool,
) -> SyncResult:
    """Append-new / refresh-known, the same contract `sync_leads` follows: batch
    every write to the end, never touch a rep column, dedupe by key."""
    backend.ensure_tab(tab, columns)
    backend.ensure_columns(tab, tool_columns)
    backend.ensure_tab(DNC_TAB, DNC_COLUMNS)
    backend.ensure_tab(RUNS_TAB, RUNS_COLUMNS)

    existing = backend.read_rows(tab)
    dnc_keys, dnc_phones, _ = _dnc_sets(backend.read_rows(DNC_TAB))
    by_key = {r.get("key", "").strip(): i for i, r in enumerate(existing) if r.get("key", "").strip()}

    result = SyncResult()
    appends: list[dict[str, Any]] = []
    updates: list[tuple[int, dict[str, Any]]] = []
    new_rows = 0

    for row in rows_out:
        key = row["key"]
        phone = str(row.get("phone") or "")
        if key in dnc_keys or (phone and phone in dnc_phones):
            result.skipped_dnc += 1
            continue
        index = by_key.get(key)
        if index is None:
            if new_rows >= new_row_cap:
                result.skipped_cap += 1
                continue
            appends.append(row)
            new_rows += 1
            result.added += 1
            continue
        current = existing[index]
        update = {
            c: row[c] for c in refresh
            if c in row and str(current.get(c) or "") != str(row[c]) and not _blank(row[c])
        }
        if update:
            update["last updated"] = today.isoformat()
            updates.append((index, update))
            result.enriched += 1
        else:
            result.skipped_no_change += 1

    if not dry_run:
        if appends:
            backend.append_rows(tab, appends)
        if updates:
            backend.update_rows(tab, updates)
        # Read the tab back and check the arithmetic. Everything in this pipeline
        # soft-fails by design, which means a write that does not land looks
        # exactly like a write that did -- the run reports "added 487" either way.
        # Counting is cheap and turns that into a reported error.
        landed = len(backend.read_rows(tab))
        expected = len(existing) + len(appends)
        if landed != expected:
            result.errors.append(
                f"{tab}: expected {expected} rows after writing {len(appends)} new "
                f"({len(existing)} were already there) but the tab reads back {landed} "
                "-- the append did not land, or something else is writing this tab"
            )
        backend.append_rows(RUNS_TAB, [_run_row(result, today=today)])
    return result


def sync_apartments(
    backend: SheetBackend,
    complexes: list[Complex],
    *,
    today: date,
    new_row_cap: int = DEFAULT_APARTMENT_ROW_CAP,
    dry_run: bool = False,
) -> SyncResult:
    """The properties. Ordered biggest first, since the ask was for the large ones
    -- a rep working top-down reaches the staffed leasing offices first."""
    ordered = sorted(
        complexes,
        key=lambda c: (0 if c.onsite_tier == "Leasing office (posted hours)" else 1, -c.review_count, c.name),
    )
    return _sync_generic(
        backend, APARTMENTS_TAB, APARTMENT_COLUMNS, APARTMENT_TOOL_COLUMNS,
        [_apartment_row(c, today=today) for c in ordered],
        APARTMENT_CONTACT_COLUMNS, today=today, new_row_cap=new_row_cap, dry_run=dry_run,
    )


def sync_managers(
    backend: SheetBackend,
    managers: list[Manager],
    *,
    today: date,
    new_row_cap: int = DEFAULT_APARTMENT_ROW_CAP,
    dry_run: bool = False,
) -> SyncResult:
    """The companies, biggest portfolio first."""
    return _sync_generic(
        backend, MANAGERS_TAB, MANAGER_COLUMNS, MANAGER_TOOL_COLUMNS,
        [_manager_row(m, today=today) for m in managers],
        ("phone", "properties", "with leasing office", "cities", "website",
         "grouped by", "property names"),
        today=today, new_row_cap=new_row_cap, dry_run=dry_run,
    )
