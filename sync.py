"""Daily sync: Meta Ads insights + instant-form leads → Google Sheets.

Run manually with:  python sync.py
Runs automatically every morning via GitHub Actions (.github/workflows/sync.yml).
"""

from connector import ads_insights, leads, sheets


def main() -> None:
    ss = sheets.open_spreadsheet()

    print("\n=== Meta Ads performance ===")
    ad_rows = ads_insights.fetch_ads_rows()
    sheets.replace_window(ss, "Meta Ads", ads_insights.HEADERS, ad_rows)

    print("\n=== Instant form leads ===")
    lead_rows = leads.fetch_lead_rows()
    sheets.append_new(ss, "Meta Leads", leads.HEADERS, lead_rows)

    print("\nDone. Open your Google Sheet to see the data.")


if __name__ == "__main__":
    main()
