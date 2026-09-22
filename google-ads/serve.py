"""Railway entrypoint for Google's official Google Ads MCP server.

Wraps ``ads_mcp`` (https://github.com/googleads/google-ads-mcp) so it runs the
way the FieldRoutes server next door does: always over streamable HTTP, with a
``/healthz`` route Railway can probe, and with the OAuth base URL derived from
Railway's injected public domain so nothing has to be pasted twice.

Three things it adds on top of ``google-ads-mcp``'s own entrypoint:

1. ``GET /healthz`` always answers 200 with a JSON status. When the OAuth
   client isn't configured yet it says exactly which variables are missing and
   what redirect URI to register in Google Cloud, and the MCP endpoint is not
   mounted at all (nothing to authenticate against, so nothing to expose).
2. ``GOOGLE_ADS_MCP_BASE_URL`` defaults to ``https://$RAILWAY_PUBLIC_DOMAIN``.
3. ``GOOGLE_ADS_MCP_JWT_SIGNING_KEY`` is generated once and kept on the
   persistent volume if it isn't set, so tokens survive redeploys without a
   secret to manage. Setting the variable explicitly still wins.

Environment variables the upstream server reads are documented in
``google-ads/README.md``; everything here is read *before* ``ads_mcp`` is
imported because its coordinator snapshots ``os.environ`` at import time.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

log = logging.getLogger("google-ads-mcp.serve")
logging.basicConfig(level=logging.INFO)

MCP_PATH = "/mcp"
OAUTH_CALLBACK_PATH = "/auth/callback"
DEFAULT_STORAGE_PATH = "/var/lib/google-ads-mcp"
JWT_KEY_FILENAME = "jwt_signing_key"

REQUIRED_VARS = (
    "GOOGLE_ADS_MCP_OAUTH_CLIENT_ID",
    "GOOGLE_ADS_MCP_OAUTH_CLIENT_SECRET",
    "GOOGLE_ADS_DEVELOPER_TOKEN",
)


def _apply_defaults() -> None:
    """Fill in the derivable settings so the owner only pastes real secrets."""
    env = os.environ

    if not env.get("GOOGLE_ADS_MCP_BASE_URL"):
        domain = env.get("RAILWAY_PUBLIC_DOMAIN")
        if domain:
            env["GOOGLE_ADS_MCP_BASE_URL"] = f"https://{domain}"

    env.setdefault("GOOGLE_ADS_MCP_STORAGE_TYPE", "filetree")
    env.setdefault("GOOGLE_ADS_MCP_STORAGE_PATH", DEFAULT_STORAGE_PATH)

    if not env.get("GOOGLE_ADS_MCP_JWT_SIGNING_KEY"):
        storage = Path(env["GOOGLE_ADS_MCP_STORAGE_PATH"])
        try:
            storage.mkdir(parents=True, exist_ok=True)
            key_file = storage / JWT_KEY_FILENAME
            if key_file.exists():
                key = key_file.read_text().strip()
            else:
                key = secrets.token_urlsafe(48)
                key_file.touch(mode=0o600)
                key_file.write_text(key)
                log.info("generated a new JWT signing key at %s", key_file)
            if key:
                env["GOOGLE_ADS_MCP_JWT_SIGNING_KEY"] = key
        except OSError as exc:  # read-only or missing volume: tokens won't survive restarts
            log.warning(
                "could not persist a JWT signing key under %s (%s); "
                "tokens will be invalidated on every restart. Set "
                "GOOGLE_ADS_MCP_JWT_SIGNING_KEY or mount a volume there.",
                storage,
                exc,
            )


def _missing() -> list[str]:
    missing = [name for name in REQUIRED_VARS if not os.environ.get(name)]
    if not os.environ.get("GOOGLE_ADS_MCP_BASE_URL"):
        missing.append("GOOGLE_ADS_MCP_BASE_URL (or RAILWAY_PUBLIC_DOMAIN)")
    return missing


def _status() -> dict:
    base = os.environ.get("GOOGLE_ADS_MCP_BASE_URL") or "https://<your-domain>"
    missing = _missing()
    login_cid = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID") or None
    return {
        "status": "ok" if not missing else "unconfigured",
        "server": "google-ads-mcp",
        "mcpUrl": f"{base}{MCP_PATH}",
        "oauthRedirectUri": f"{base}{OAUTH_CALLBACK_PATH}",
        "missing": missing,
        "storage": os.environ.get("GOOGLE_ADS_MCP_STORAGE_TYPE"),
        "loginCustomerId": login_cid,
        "jwtSigningKey": "set" if os.environ.get("GOOGLE_ADS_MCP_JWT_SIGNING_KEY") else "missing",
    }


async def healthz(_: Request) -> JSONResponse:
    return JSONResponse(_status())


def build_app():
    _apply_defaults()
    status = _status()

    if status["missing"]:
        log.warning(
            "Google Ads MCP is NOT configured; serving /healthz only. Missing: %s",
            ", ".join(status["missing"]),
        )
        return Starlette(routes=[Route("/healthz", healthz, methods=["GET"])])

    # Import only once the OAuth client is present: ads_mcp.coordinator builds
    # the FastMCP instance (with or without auth) at import time.
    from ads_mcp.coordinator import mcp
    from ads_mcp.resources import (  # noqa: F401  (registers the resources)
        discovery,
        metrics,
        release_notes,
        segments,
    )

    mcp.custom_route("/healthz", methods=["GET"])(healthz)

    log.info(
        "Google Ads MCP ready: %s (OAuth redirect %s)",
        status["mcpUrl"],
        status["oauthRedirectUri"],
    )
    # Stateful streamable HTTP on purpose (upstream README): it keeps the GET
    # SSE channel that older MCP clients still open. Single replica on Railway.
    return mcp.http_app(path=MCP_PATH, transport="streamable-http")


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    # No access log: the OAuth callback URL carries authorization codes.
    uvicorn.run(build_app(), host="0.0.0.0", port=port, access_log=False)


if __name__ == "__main__":
    main()
