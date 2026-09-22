# Google's official Google Ads MCP server, packaged for Railway.
# Upstream: https://github.com/googleads/google-ads-mcp (Apache-2.0).
#
# Same discipline as the FieldRoutes image at the repo root: every dependency
# is pinned in requirements.lock (including the upstream commit itself), so a
# rebuild months from now can't pick up a breaking release. Regenerate the
# lock deliberately; see google-ads/README.md.
FROM python:3.11-slim

# git is needed once, to fetch the pinned upstream commit.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock

COPY serve.py ./

# OAuth state (client registrations, encrypted Google tokens) and the
# auto-generated JWT signing key live here. Mount a Railway volume at this path
# so sign-ins survive redeploys.
ENV GOOGLE_ADS_MCP_STORAGE_TYPE=filetree \
    GOOGLE_ADS_MCP_STORAGE_PATH=/var/lib/google-ads-mcp \
    PORT=8080 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "/app/serve.py"]
