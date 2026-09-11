"""Service-account credentials for the Google Ads MCP server.

Google's `ads_mcp.utils._create_credentials` resolves credentials in this order:

1. the FastMCP OAuth-proxy access token (`get_access_token()`), then
2. Application Default Credentials via `google.auth.default()`.

Neither works on Railway: we deliberately leave the OAuth-proxy env vars unset
(see `server.py` for why), and there is no gcloud, no metadata server and no ADC
file in the container. This module installs a third path -- an explicit service
account built from an env var -- by replacing that function.

Two verified facts shape this file:

- `google.auth.default(scopes=...)` **silently drops** `scopes=` for user
  (`authorized_user`) credentials: `default()` withholds them from the file
  loader, and `with_scopes_if_required()` then no-ops because
  `google.oauth2.credentials.Credentials` is `ReadOnlyScoped` with
  `requires_scopes = False`. Service-account credentials are the opposite --
  they implement `Scoped`, so `scopes=` is honoured. That is one of the reasons
  this deployment uses a service account rather than a refresh token.
- Domain-wide delegation is no longer required. In google-ads 32.0.0
  `impersonated_email` moved from the required to the optional service-account
  key tuple, and the library now passes `subject=None` straight through. The
  service account is authorised by being added as a user in the Google Ads UI
  (Admin -> Access and security -> Users), not by a Workspace delegation.
"""

from __future__ import annotations

import json
import os

from google.oauth2 import service_account

# The only scope Google Ads publishes. There is no read-only variant -- which is
# why the write guards in `guards.py`, not the scope, are what keeps an agent
# from mutating the account.
ADS_SCOPE = "https://www.googleapis.com/auth/adwords"

# Raw JSON of the service-account key. Railway holds this as a single variable;
# it never touches the repo.
ENV_SA_JSON = "GOOGLE_ADS_SERVICE_ACCOUNT_JSON"
# Path to the same key on disk, for local development.
ENV_SA_FILE = "GOOGLE_ADS_SERVICE_ACCOUNT_FILE"
# Only set this if the account genuinely requires Workspace delegation. Leaving
# it unset is the normal, supported case.
ENV_IMPERSONATE = "GOOGLE_ADS_IMPERSONATED_EMAIL"


class CredentialsError(RuntimeError):
    """Raised when the deployment has no usable Google Ads credentials."""


def _service_account_info() -> dict:
    """Returns the parsed service-account key, or raises with a usable message."""
    raw = os.environ.get(ENV_SA_JSON, "").strip()
    if raw:
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CredentialsError(
                f"{ENV_SA_JSON} is set but is not valid JSON: {exc}. Paste the "
                "entire key file contents, including the surrounding braces."
            ) from exc
    else:
        path = os.environ.get(ENV_SA_FILE, "").strip()
        if not path:
            raise CredentialsError(
                f"No Google Ads credentials. Set {ENV_SA_JSON} to the contents of "
                f"the service-account key JSON, or {ENV_SA_FILE} to a path to it."
            )
        if not os.path.exists(path):
            raise CredentialsError(f"{ENV_SA_FILE} points at a missing file: {path}")
        with open(path, "r", encoding="utf-8") as handle:
            info = json.load(handle)

    if info.get("type") != "service_account":
        raise CredentialsError(
            "Expected a service-account key (\"type\": \"service_account\"), got "
            f"\"type\": {info.get('type')!r}. An OAuth client_secret.json is a "
            "different file and will not work here."
        )
    for required in ("client_email", "private_key", "token_uri"):
        if not info.get(required):
            raise CredentialsError(
                f"Service-account key is missing {required!r}; it looks truncated."
            )
    return info


def build_credentials() -> service_account.Credentials:
    """Builds scoped service-account credentials from the environment."""
    creds = service_account.Credentials.from_service_account_info(
        _service_account_info(), scopes=[ADS_SCOPE]
    )
    # Delegation is opt-in and almost never needed -- see the module docstring.
    subject = os.environ.get(ENV_IMPERSONATE, "").strip()
    if subject:
        creds = creds.with_subject(subject)
    return creds


def service_account_email() -> str | None:
    """The key's client_email, for health output. Returns None if unconfigured."""
    try:
        return _service_account_info().get("client_email")
    except CredentialsError:
        return None


def install() -> None:
    """Points `ads_mcp.utils` at our credentials instead of ADC.

    Replaces the module attribute rather than subclassing, because
    `_get_googleads_client()` calls `_create_credentials()` as a module-level
    name on every request.
    """
    import ads_mcp.utils as utils

    utils._create_credentials = build_credentials
