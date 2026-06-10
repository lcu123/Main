"""Configuration — everything comes from environment variables (GitHub Secrets).

You never need to edit this file. Set the secrets listed in SETUP.md instead.
"""

import os


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"Missing required secret/environment variable: {name}\n"
            f"See SETUP.md for how to set it."
        )
    return value


# --- Meta (Facebook) ---
META_ACCESS_TOKEN = require("META_ACCESS_TOKEN")
META_AD_ACCOUNT_ID = require("META_AD_ACCOUNT_ID")  # e.g. act_1234567890
META_PAGE_ID = require("META_PAGE_ID")              # numeric page ID

# --- Google Sheets ---
GOOGLE_SERVICE_ACCOUNT_JSON = require("GOOGLE_SERVICE_ACCOUNT_JSON")
GOOGLE_SHEET_ID = require("GOOGLE_SHEET_ID")        # the long ID in the sheet URL

# --- Optional knobs (defaults are fine) ---
# How many days of ads data to (re)pull each run. Meta attributes conversions
# retroactively, so re-pulling a window keeps recent days accurate.
ADS_LOOKBACK_DAYS = int(os.environ.get("ADS_LOOKBACK_DAYS", "28"))
# How many days of leads to fetch each run (deduped by lead ID on write).
LEADS_LOOKBACK_DAYS = int(os.environ.get("LEADS_LOOKBACK_DAYS", "14"))

GRAPH_API_VERSION = os.environ.get("GRAPH_API_VERSION", "v21.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
