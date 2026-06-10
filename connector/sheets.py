"""Write data into Google Sheets.

Two write strategies:
- replace_window: for ads data — delete rows in the re-pulled date window,
  then append the fresh rows (keeps older history intact, fixes attribution drift).
- append_new: for leads — append only leads whose ID isn't already in the sheet.
"""

import json

import google.auth.exceptions
import gspread
from google.oauth2.service_account import Credentials

from . import config

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def open_spreadsheet() -> gspread.Spreadsheet:
    try:
        info = json.loads(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError:
        raise SystemExit(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the entire "
            "contents of the downloaded key file (see SETUP.md section 2)."
        )
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    client = gspread.authorize(creds)
    try:
        return client.open_by_key(config.GOOGLE_SHEET_ID)
    except (gspread.SpreadsheetNotFound, google.auth.exceptions.GoogleAuthError):
        raise SystemExit(
            "Could not open the Google Sheet. Check that GOOGLE_SHEET_ID is the "
            "long ID from the sheet URL, and that you shared the sheet with the "
            f"service account email ({info.get('client_email', '?')}) as Editor."
        )


def _get_or_create_ws(ss: gspread.Spreadsheet, title: str, headers: list[str]):
    try:
        ws = ss.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=title, rows=1000, cols=len(headers))
        ws.append_row(headers)
        return ws
    if ws.row_values(1) != headers:
        ws.update(values=[headers], range_name="A1")
    return ws


def replace_window(ss, title: str, headers: list[str], rows: list[list],
                   date_col: int = 0):
    """Replace all sheet rows whose date falls inside the new data's date range."""
    ws = _get_or_create_ws(ss, title, headers)
    if not rows:
        print(f"  [{title}] nothing to write.")
        return
    new_dates = {r[date_col] for r in rows}
    existing = ws.get_all_values()[1:]  # skip header
    kept = [r for r in existing if r and r[date_col] not in new_dates]
    ws.clear()
    ws.update(values=[headers] + kept + rows, range_name="A1",
              value_input_option="USER_ENTERED")
    print(f"  [{title}] {len(rows)} rows written ({len(kept)} older rows kept).")


def append_new(ss, title: str, headers: list[str], rows: list[list],
               id_col: int = 0):
    """Append only rows whose ID (column id_col) isn't already present."""
    ws = _get_or_create_ws(ss, title, headers)
    existing_ids = set(ws.col_values(id_col + 1)[1:])
    fresh = [r for r in rows if str(r[id_col]) not in existing_ids]
    if fresh:
        ws.append_rows(fresh, value_input_option="USER_ENTERED")
    print(f"  [{title}] {len(fresh)} new rows appended "
          f"({len(rows) - len(fresh)} already present).")
