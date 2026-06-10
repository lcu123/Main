"""Thin wrapper around the Meta Graph API with paging and basic retry."""

import time

import requests

from . import config


class MetaAPIError(SystemExit):
    pass


def _get(url: str, params: dict) -> dict:
    params = dict(params)
    params["access_token"] = config.META_ACCESS_TOKEN
    for attempt in range(5):
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        # Rate limited or transient server error -> back off and retry
        if resp.status_code in (429, 500, 502, 503) or "rate limit" in resp.text.lower():
            wait = 2 ** attempt * 5
            print(f"  Meta API busy (HTTP {resp.status_code}), retrying in {wait}s...")
            time.sleep(wait)
            continue
        try:
            err = resp.json().get("error", {})
            message = err.get("message", resp.text)
            code = err.get("code", resp.status_code)
        except Exception:
            message, code = resp.text, resp.status_code
        raise MetaAPIError(
            f"Meta API error (code {code}): {message}\n"
            f"URL: {url}\n"
            f"Common causes: expired access token (regenerate it — see SETUP.md "
            f"section 1.5), wrong account/page ID, or missing permission."
        )
    raise MetaAPIError("Meta API kept rate-limiting after 5 retries. Try again later.")


def get_paged(path: str, params: dict) -> list[dict]:
    """GET a Graph API edge and follow pagination, returning all rows."""
    url = f"{config.GRAPH_BASE}/{path}"
    rows: list[dict] = []
    data = _get(url, params)
    while True:
        rows.extend(data.get("data", []))
        next_url = data.get("paging", {}).get("next")
        if not next_url:
            return rows
        resp = requests.get(next_url, timeout=60)
        resp.raise_for_status()
        data = resp.json()
