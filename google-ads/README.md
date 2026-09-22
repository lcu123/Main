# Google Ads MCP (Railway deployment)

Runs Google's official [google-ads-mcp](https://github.com/googleads/google-ads-mcp) server as a second Railway service next to the FieldRoutes one, so Claude can answer "how are my ads doing?" from claude.ai web, iOS, Android, Claude Desktop, Cowork and Claude Code. Sign-in is Google OAuth (the server proxies it), so the same connector works on every device once it's added to your claude.ai account.

What Claude gets:

| Tool | What it does |
| --- | --- |
| `customers_list_accessible_customers` | The Google Ads account IDs your Google login can see. |
| `search_search` | Any read-only [GAQL](https://developers.google.com/google-ads/api/docs/query/overview) query: campaigns, keywords, search terms, cost, conversions, calls, by day/device/geo. |
| `metadata_get_resource_metadata` | Which fields, metrics and segments a resource supports, so Claude writes valid queries. |

It is **read-only**. Google's server does not create or edit campaigns; that stays in the Google Ads UI. Example asks: "Spend and cost per lead by campaign this month", "Which search terms drove calls last week?", "Compare Sacramento vs Folsom CTR", "What's my average CPC on rodent keywords?".

## What's in this folder

| File | Purpose |
| --- | --- |
| `serve.py` | Entrypoint. Adds `/healthz`, derives the OAuth base URL from Railway's domain, generates and persists the JWT signing key on the volume, and refuses to mount `/mcp` until the OAuth client is configured. |
| `Dockerfile` | `python:3.11-slim`, installs `requirements.lock`, runs `serve.py`. |
| `requirements.lock` | Every dependency pinned, including the upstream commit of `google-ads-mcp`. |
| `railway.json` | Dockerfile builder, `/healthz` health check. |
| `.env.example` | Every variable, commented. |

## One-time setup (owner, ~20 minutes)

The Railway service `google-ads-mcp` already exists in the `serene-essence` project (root directory `google-ads`, volume at `/var/lib/google-ads-mcp`). It boots in "unconfigured" mode and its `/healthz` tells you exactly what is missing. You need three secrets from Google.

### 1. Google Cloud OAuth client

1. Open [console.cloud.google.com](https://console.cloud.google.com), create a project (or reuse one), and enable the **Google Ads API** under APIs & Services → Library.
2. APIs & Services → **OAuth consent screen**. User type **External**, publishing status **Testing**. Add your Google account (the one that owns the Google Ads account) as a **test user**. Testing mode caps the app at test users only, which is the access control: nobody outside that list can sign in to this server.
3. APIs & Services → **Credentials** → Create credentials → **OAuth client ID** → type **Web application**.
   Authorized redirect URI: `https://<railway-domain>/auth/callback` (the exact value is shown at `https://<railway-domain>/healthz` as `oauthRedirectUri`).
4. Copy the **Client ID** and **Client secret**.

### 2. Google Ads developer token

1. Developer tokens live on a **manager account** (MCC). If you don't have one, create it at [ads.google.com/home/tools/manager-accounts](https://ads.google.com/home/tools/manager-accounts/) and link your Zest account to it.
2. In the manager account: Admin → **API Center** → apply for API access. A new token starts at **Test account** access; you need **Explorer** (formerly Basic) to read the real account. The application form takes a few minutes; approval is usually within a couple of business days. Until then every query returns *"The developer token is only approved for use with test accounts"*.
3. Copy the token.

### 3. Railway variables

Railway → `google-ads-mcp` → Variables. Add:

```
GOOGLE_ADS_MCP_OAUTH_CLIENT_ID=...apps.googleusercontent.com
GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET=GOCSPX-...
GOOGLE_ADS_DEVELOPER_TOKEN=...
```

If the Zest account is reached *through* the manager account, also set `GOOGLE_ADS_LOGIN_CUSTOMER_ID` to the manager's 10-digit ID (no dashes). Railway redeploys; `https://<railway-domain>/healthz` should now say `"status": "ok"`.

Also confirm Settings → Networking has a public domain (Generate Domain, port 8080) and Settings → Deploy has **App Sleeping off**: a cold start makes the connector's first call time out.

### 4. Connect Claude

- **claude.ai (web, iOS, Android) and Cowork**: Settings → Connectors → **Add custom connector**. Name `Google Ads`, URL `https://<railway-domain>/mcp`. Leave OAuth client ID/secret blank (the server registers claude.ai automatically). Click **Connect**, sign in with the Google account you listed as a test user, approve. Connectors are per account, so it's now available on every device you're signed into.
- **Claude Desktop**: same connector; it syncs from your account.
- **Claude Code**: `claude mcp add --transport http google-ads https://<railway-domain>/mcp`, then `/mcp` to sign in.

First prompt: "What Google Ads customer IDs can you see?" then "Spend, clicks and conversions by campaign for the last 30 days for customer <id>."

## Safety model

- Read-only by construction: the upstream server exposes only `search`, metadata and account listing, all marked `readOnlyHint`. Nothing here can change bids, budgets or ads.
- Who can sign in is controlled by the Google Cloud consent screen (Testing mode + test users). Keep it there; don't publish the app.
- Google tokens are stored encrypted (Fernet key derived from the JWT signing key) on the Railway volume. Nothing is logged from the OAuth callback; uvicorn's access log is off.
- The developer token is server-side only and never reaches Claude. Google Ads API quota for Explorer access is 15,000 operations/day, far above office use.

## Maintenance

Bump upstream deliberately: change the commit hash in `requirements.lock`'s last line, then regenerate the pins in a clean Python 3.11 venv:

```
python3.11 -m venv /tmp/gads && . /tmp/gads/bin/activate
pip install "google-ads-mcp @ git+https://github.com/googleads/google-ads-mcp.git@<sha>"
pip freeze | grep -v '^google-ads-mcp' > google-ads/requirements.lock
echo 'google-ads-mcp @ git+https://github.com/googleads/google-ads-mcp.git@<sha>' >> google-ads/requirements.lock
```

Upstream's own Dockerfile hard-asserts `fastmcp==4.0.3` for a Codex CLI patch and fails to build against newer FastMCP releases, which is why this folder ships its own Dockerfile and pins `fastmcp==4.0.5` instead.

Local run without Google credentials (health check only):

```
pip install -r google-ads/requirements.lock
GOOGLE_ADS_MCP_STORAGE_PATH=/tmp/gads-store PORT=8080 python google-ads/serve.py
curl localhost:8080/healthz      # -> {"status":"unconfigured", "missing":[...]}
```

CI builds this image and checks `/healthz` in both modes on every push.
