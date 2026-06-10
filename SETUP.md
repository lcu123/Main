# Setup Guide — Meta Ads → Google Sheets connector

This connector pulls your **Facebook/Meta ads performance** and **instant-form
leads** into a Google Sheet every day, for free, with no Supermetrics.

You only do this setup **once**. It takes about 30–45 minutes. After that,
everything runs automatically.

There are 4 parts:

1. Get a Meta (Facebook) access token
2. Set up Google Sheets access
3. Add 5 secrets to GitHub
4. Run it and build your dashboard

---

## Part 1 — Meta (Facebook) access

### 1.1 Create a Meta developer app

1. Go to <https://developers.facebook.com> and log in with the Facebook
   account that has access to your ads (the one you use in Business Manager).
2. Click **My Apps → Create App**.
3. Choose **"Other"** for use case, then **"Business"** as the app type.
4. Name it anything (e.g. `Ads Connector`), pick your Business portfolio, and create it.

### 1.2 Find your IDs (write these down)

- **Ad account ID**: Go to <https://adsmanager.facebook.com>. Look at the URL —
  it contains `act=1234567890`. Your ID is `act_1234567890` (add the `act_` prefix).
- **Page ID**: Go to your Facebook Page → **About → Page transparency** (or
  Settings → Page setup). Copy the numeric **Page ID**.

### 1.3 Generate an access token

1. Go to the **Graph API Explorer**: <https://developers.facebook.com/tools/explorer>
2. Top right: select **your app** in the "Meta App" dropdown.
3. Click **"Generate Access Token"** and log in / approve.
4. In the **Permissions** box, add ALL of these:
   - `ads_read`
   - `leads_retrieval`
   - `pages_show_list`
   - `pages_read_engagement`
   - `pages_manage_ads`
5. Click **Generate Access Token** again and approve the permissions,
   making sure you select your Page and ad account when asked.
6. Copy the token (long string starting with `EAA...`).

### 1.4 Make the token long-lived (60 days)

The token from the explorer expires in ~2 hours. Extend it:

1. Go to the **Access Token Debugger**: <https://developers.facebook.com/tools/debug/accesstoken>
2. Paste your token, click **Debug**.
3. Click the **"Extend Access Token"** button at the bottom. Copy the new token.
   This one lasts **60 days**.

### 1.5 ⚠️ Token renewal (every ~2 months)

The long-lived token expires after 60 days. When the sync starts failing with
a token error (GitHub will email you that the Action failed):

1. Repeat steps 1.3–1.4 to get a fresh token.
2. Update the `META_ACCESS_TOKEN` secret in GitHub (Part 3 below).

Takes 2 minutes. Put a reminder in your calendar every 55 days.

> Tip for later: a "System User" token in Business Manager can be made
> non-expiring. It's a bit more fiddly to set up — happy to walk you through
> it once the basic version is running.

---

## Part 2 — Google Sheets access

### 2.1 Create the spreadsheet

1. Go to <https://sheets.google.com> and create a blank spreadsheet.
   Name it e.g. `Marketing Dashboard Data`.
2. Copy the **Sheet ID** from the URL. It's the long string between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`THIS_LONG_STRING`**`/edit`

### 2.2 Create a Google "service account" (a robot user)

1. Go to <https://console.cloud.google.com> (log in with your Google account).
2. Top bar → project dropdown → **New Project** → name it `ads-connector` → Create.
3. With the project selected, go to **APIs & Services → Library**, search for
   **"Google Sheets API"**, click it, click **Enable**.
4. Go to **APIs & Services → Credentials → Create Credentials → Service account**.
   - Name: `sheets-writer` → Create and continue → skip the optional steps → Done.
5. Click the service account you just created → **Keys** tab →
   **Add key → Create new key → JSON → Create**. A `.json` file downloads.
   **Keep this file safe** — it's a password.
6. Open the JSON file in a text editor and find the `client_email` value
   (looks like `sheets-writer@ads-connector-xxxx.iam.gserviceaccount.com`).
7. Back in your Google Sheet: click **Share**, paste that email, give it
   **Editor** access, uncheck "Notify", Share.

---

## Part 3 — Add the secrets to GitHub

In this GitHub repository:

1. Go to **Settings → Secrets and variables → Actions → New repository secret**.
2. Add these 5 secrets, one at a time:

| Secret name | Value |
|---|---|
| `META_ACCESS_TOKEN` | The long-lived token from step 1.4 |
| `META_AD_ACCOUNT_ID` | e.g. `act_1234567890` (with the `act_` prefix) |
| `META_PAGE_ID` | Your numeric page ID |
| `GOOGLE_SHEET_ID` | The long ID from the sheet URL (step 2.1) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | The **entire contents** of the downloaded JSON key file — open it, select all, copy, paste |

---

## Part 4 — Run it & build the dashboard

### 4.1 First run

1. In GitHub, go to the **Actions** tab.
2. Click **"Daily Meta Ads sync"** in the left sidebar.
3. Click **"Run workflow" → Run workflow** (green button).
4. Wait ~1 minute. A green checkmark = success. Open your Google Sheet —
   you should see two new tabs: **Meta Ads** and **Meta Leads**.

If it fails (red X), click into the run → click the **Run sync** step — the
error message will say exactly what's wrong (almost always a typo'd secret).

From now on it runs automatically every morning (06:30 UTC — edit
`.github/workflows/sync.yml` to change the time).

### 4.2 Build the Looker Studio dashboard (free, mobile-friendly)

1. Go to <https://lookerstudio.google.com> → **Create → Report**.
2. Choose **Google Sheets** as the data source → pick your spreadsheet →
   the **Meta Ads** tab → Add.
3. Add charts: a time-series of `spend` and `leads` by `date`, a table by
   `campaign_name`, scorecards for total spend / leads / cost per lead.
4. **Resource → Manage added data sources → Add a data source** to also add
   the **Meta Leads** tab for a leads table.
5. Click **Share** to give anyone the link — it works on phones, tablets, anything.

### 4.3 Adding Google Ads, Analytics & sales data later

This is the nice part of the Sheets + Looker Studio approach:

- **Google Ads** and **GA4** have *free native connectors in Looker Studio* —
  no code needed at all. In your report: Add data → choose Google Ads / GA4 →
  log in → done. They appear alongside your Meta data.
- **Sales/revenue data**: add a tab to the same spreadsheet (manually or via a
  future connector like this one — e.g. QuickBooks/Stripe) and add it as
  another data source.

---

## What the data looks like

**Meta Ads tab** — one row per ad per day:
`date, campaign_name, adset_name, ad_name, spend, impressions, reach, clicks,
link_clicks, cpc, cpm, ctr, leads, cost_per_lead, …ids`

The last 28 days are re-pulled every run so Meta's retroactive conversion
attribution stays accurate; older history is kept forever.

**Meta Leads tab** — one row per lead (never duplicated):
`lead_id, created_time, form_name, campaign_name, ad_name, full_name,
first_name, last_name, email, phone_number, city, all_answers`

> ⚠️ Leads are personal data. Anyone with access to the Sheet can see them —
> share the spreadsheet carefully, and mind GDPR/your local privacy rules.

---

## Running it on your own computer (optional)

```bash
pip install -r requirements.txt
export META_ACCESS_TOKEN="EAA..."
export META_AD_ACCOUNT_ID="act_1234567890"
export META_PAGE_ID="1234567890"
export GOOGLE_SHEET_ID="..."
export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat path/to/key.json)"
python sync.py
```

## About Hetzner

You don't need a server for this — GitHub Actions runs it free. If you later
want **real-time lead notifications** (Meta pushes each lead the second it
arrives), that needs a small always-on server, and your Hetzner account is
perfect for it. The same `connector/` code will be reused. Ask and we'll
build that as phase 2.
