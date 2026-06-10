"""Pull instant-form (Lead Ads) leads from the Meta Graph API.

Walks every lead form on the page and fetches recent leads. Form questions
vary per form, so each lead row stores the answers as tidy "field: value"
pairs in fixed columns plus a combined text column.
"""

import datetime as dt
import json

from . import config, meta_api

# Common contact fields get their own columns; everything else lands in all_answers.
KNOWN_FIELDS = ["full_name", "first_name", "last_name", "email", "phone_number", "city"]

HEADERS = (
    ["lead_id", "created_time", "form_name", "campaign_name", "ad_name"]
    + KNOWN_FIELDS
    + ["all_answers"]
)


def fetch_lead_rows() -> list[list]:
    since_ts = int(
        (dt.datetime.now(dt.timezone.utc)
         - dt.timedelta(days=config.LEADS_LOOKBACK_DAYS)).timestamp()
    )

    print("Fetching lead forms on the page ...")
    forms = meta_api.get_paged(
        f"{config.META_PAGE_ID}/leadgen_forms",
        {"fields": "id,name,status", "limit": 100},
    )
    print(f"  {len(forms)} forms found.")

    rows = []
    for form in forms:
        leads = meta_api.get_paged(
            f"{form['id']}/leads",
            {
                "fields": "id,created_time,field_data,campaign_name,ad_name",
                "filtering": json.dumps([
                    {"field": "time_created", "operator": "GREATER_THAN", "value": since_ts}
                ]),
                "limit": 100,
            },
        )
        for lead in leads:
            answers = {
                f["name"]: ", ".join(f.get("values", []))
                for f in lead.get("field_data", [])
            }
            row = [
                lead.get("id", ""),
                lead.get("created_time", ""),
                form.get("name", ""),
                lead.get("campaign_name", ""),
                lead.get("ad_name", ""),
            ]
            row += [answers.get(k, "") for k in KNOWN_FIELDS]
            row.append("; ".join(
                f"{k}: {v}" for k, v in answers.items() if k not in KNOWN_FIELDS
            ))
            rows.append(row)

    rows.sort(key=lambda x: x[1])
    print(f"  {len(rows)} leads fetched (last {config.LEADS_LOOKBACK_DAYS} days).")
    return rows
