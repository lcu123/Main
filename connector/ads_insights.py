"""Pull daily ad performance from the Meta Ads Insights API.

One row per ad per day, with campaign/adset context — the same shape
Supermetrics gives you, ready for pivoting in Sheets / Looker Studio.
"""

import datetime as dt

from . import config, meta_api

HEADERS = [
    "date", "campaign_name", "adset_name", "ad_name",
    "spend", "impressions", "reach", "clicks", "link_clicks",
    "cpc", "cpm", "ctr", "leads", "cost_per_lead",
    "campaign_id", "adset_id", "ad_id",
]


def _actions_count(row: dict, action_type: str) -> float:
    for action in row.get("actions", []):
        if action.get("action_type") == action_type:
            return float(action.get("value", 0))
    return 0.0


def fetch_ads_rows() -> list[list]:
    """Return rows (lists matching HEADERS) for the lookback window."""
    until = dt.date.today() - dt.timedelta(days=1)  # through yesterday
    since = until - dt.timedelta(days=config.ADS_LOOKBACK_DAYS - 1)
    print(f"Fetching ads insights {since} → {until} ...")

    raw = meta_api.get_paged(
        f"{config.META_AD_ACCOUNT_ID}/insights",
        {
            "level": "ad",
            "time_range": f'{{"since":"{since}","until":"{until}"}}',
            "time_increment": 1,  # daily rows
            "fields": ",".join([
                "date_start", "campaign_id", "campaign_name",
                "adset_id", "adset_name", "ad_id", "ad_name",
                "spend", "impressions", "reach", "clicks",
                "inline_link_clicks", "cpc", "cpm", "ctr", "actions",
            ]),
            "limit": 500,
        },
    )

    rows = []
    for r in raw:
        spend = float(r.get("spend", 0))
        leads = _actions_count(r, "lead") or _actions_count(
            r, "onsite_conversion.lead_grouped"
        )
        rows.append([
            r.get("date_start", ""),
            r.get("campaign_name", ""),
            r.get("adset_name", ""),
            r.get("ad_name", ""),
            spend,
            int(r.get("impressions", 0)),
            int(r.get("reach", 0)),
            int(r.get("clicks", 0)),
            int(r.get("inline_link_clicks", 0) or 0),
            float(r.get("cpc", 0) or 0),
            float(r.get("cpm", 0) or 0),
            float(r.get("ctr", 0) or 0),
            int(leads),
            round(spend / leads, 2) if leads else "",
            r.get("campaign_id", ""),
            r.get("adset_id", ""),
            r.get("ad_id", ""),
        ])
    rows.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    print(f"  {len(rows)} ad-day rows fetched.")
    return rows
