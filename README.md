# Meta Ads → Google Sheets Connector

Pulls **Facebook/Meta ads performance** and **instant-form (Lead Ads) leads**
into a Google Sheet daily — a free, self-hosted replacement for Supermetrics.
Runs on GitHub Actions (no server needed). Dashboard via Looker Studio.

```
Meta Graph API ──> GitHub Actions (daily) ──> Google Sheets ──> Looker Studio
   ads insights         python sync.py          "Meta Ads" tab     charts on
   lead forms                                   "Meta Leads" tab   any device
```

**👉 Start here: [SETUP.md](SETUP.md)** — one-time, ~30–45 min, no coding.

## Files

| File | What it does |
|---|---|
| `sync.py` | Entry point — runs the whole sync |
| `connector/ads_insights.py` | Daily ad performance (spend, clicks, leads, CPL…) |
| `connector/leads.py` | Instant-form leads from all your page's lead forms |
| `connector/sheets.py` | Writes to Google Sheets (dedupes leads, refreshes recent ads data) |
| `connector/config.py` | Reads settings from GitHub Secrets |
| `.github/workflows/sync.yml` | The daily schedule |

## Roadmap ideas

- Google Ads & GA4: free native Looker Studio connectors (no code — see SETUP.md 4.3)
- Sales/revenue tab (QuickBooks, Stripe, or manual)
- Real-time lead webhook on a Hetzner VPS (phase 2)
