"""Google Places API (New) enrichment: the phone number for every lead the
county records don't give one for.

Placer's inspection report carries no owner, phone or email at all, and Yolo's
carries no phone -- so for those two counties this is the only phone source
there is. Sacramento's report header covers ~90% of its rows; this fills the
rest (hotels, country clubs and chains whose county record is blank).

Auth is the *same service-account credential the sheet writer already uses*,
via an OAuth `cloud-platform` token -- Places API (New) accepts those, so no
separate API key has to be created or stored (verified live 2026-09-08: the
credential authenticates; the only thing standing in the way was the API not
being enabled on the project). `token_provider` is injected so tests never
touch Google.

Cost control, because unlike every other source in this pipeline Places is
billed per call: a hard per-run ceiling (`LEADS_PLACES_MAX_CALLS`, default 50),
one request per candidate and never a second for the same one, and a circuit
breaker that stops the rest of the run on a 403/429 rather than burning quota
against a misconfiguration.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

import httpx

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)
# Only the fields a rep needs. Every extra field can move the call into a more
# expensive SKU, so don't widen this without a reason.
FIELD_MASK = (
    "places.displayName,places.nationalPhoneNumber,places.websiteUri,"
    "places.businessStatus,places.formattedAddress"
)
DEFAULT_MAX_CALLS = 50
PERMANENTLY_CLOSED = "CLOSED_PERMANENTLY"

_DIGITS_RE = re.compile(r"\D+")
_HOUSE_NUMBER_RE = re.compile(r"^\s*(\d+)")


class PlacesError(RuntimeError):
    """Places refused the request in a way that makes retrying pointless this
    run (API disabled, billing off, quota exhausted)."""


@dataclass(frozen=True)
class PlaceResult:
    phone: str | None  # 10 digits, no punctuation
    website: str | None
    business_status: str | None  # OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY
    matched_name: str
    matched_address: str

    @property
    def permanently_closed(self) -> bool:
        return self.business_status == PERMANENTLY_CLOSED


def max_calls() -> int:
    raw = os.environ.get("LEADS_PLACES_MAX_CALLS", "").strip()
    try:
        return int(raw) if raw else DEFAULT_MAX_CALLS
    except ValueError:
        return DEFAULT_MAX_CALLS


def normalize_phone(raw: str | None) -> str | None:
    """Places returns nationalPhoneNumber as "(916) 416-1664". FieldRoutes and
    the sheet both want bare digits, and the customer/search phone filter is an
    exact 10-digit match, so anything else is useless downstream."""
    digits = _DIGITS_RE.sub("", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else None


def service_account_token_provider(key_path: str | None = None, key_json: dict[str, Any] | None = None) -> Callable[[], str]:
    """A `token_provider` backed by the deployment's existing service account --
    the same credential `sheet.open_backend()` uses. Refreshes on demand;
    google-auth caches until the token is close to expiry.

    Resolution order mirrors `sheet.open_backend()` exactly -- inline JSON
    first, then a path on disk -- and it has to: Railway's variables UI has no
    secret-file mechanism, so the live deployment sets only the inline
    GOOGLE_SERVICE_ACCOUNT_JSON. Reading only the _PATH form here (as this did
    until 2026-09-09) meant every scheduled run raised on the same credential
    the sheet writer was about to authenticate with perfectly well."""
    from google.oauth2 import service_account

    if key_json is None and key_path is None:
        inline = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        if inline:
            try:
                key_json = json.loads(inline)
            except ValueError as exc:
                raise PlacesError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON.") from exc

    if key_json is not None:
        creds = service_account.Credentials.from_service_account_info(key_json, scopes=list(SCOPES))
    else:
        path = key_path or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON_PATH", "").strip()
        if not path:
            raise PlacesError(
                "No service-account credential for Places "
                "(set GOOGLE_SERVICE_ACCOUNT_JSON inline or GOOGLE_SERVICE_ACCOUNT_JSON_PATH)."
            )
        creds = service_account.Credentials.from_service_account_file(path, scopes=list(SCOPES))

    def token() -> str:
        if not creds.valid:
            import google.auth.exceptions
            import google.auth.transport.requests

            try:
                creds.refresh(google.auth.transport.requests.Request())
            except google.auth.exceptions.GoogleAuthError as exc:
                # Surfaced as PlacesError so the caller has one exception type to
                # degrade on: enrichment is optional, the sheet write is not.
                raise PlacesError(f"Places token refresh failed: {exc}") from exc
        return creds.token

    return token


def _house_number(street: str) -> str:
    m = _HOUSE_NUMBER_RE.match(street or "")
    return m.group(1) if m else ""


def is_same_place(*, street: str, zip5: str, matched_address: str) -> bool:
    """Text search happily returns *a* plausible business for a query that has no
    good match, so the result is only trusted when the address it came back with
    agrees with the one the county published. A wrong phone number is worse than
    a blank one -- the rep calls a stranger and the lead is burned.

    The bar: the zip matches, or the street's house number appears in the
    returned address. Formatting differs too much between the county's records
    and Google's ("Ste A", "#3", "Highway" vs "Hwy") for a stricter comparison
    to be anything but a source of false negatives."""
    addr = matched_address or ""
    if zip5 and zip5 in addr:
        return True
    house = _house_number(street)
    return bool(house) and re.search(rf"\b{re.escape(house)}\b", addr) is not None


def api_key() -> str | None:
    """An existing Google Maps Platform key, if the owner already has one with
    billing sorted -- it avoids enabling Places on this project at all. Takes
    precedence over the service account when set."""
    return os.environ.get("LEADS_PLACES_API_KEY", "").strip() or None


class PlacesClient:
    def __init__(
        self,
        http_client: httpx.AsyncClient,
        token_provider: Callable[[], str] | None = None,
        *,
        call_ceiling: int | None = None,
        key: str | None = None,
    ):
        if token_provider is None and not key:
            raise PlacesError("PlacesClient needs either an API key or a service-account token provider.")
        self._client = http_client
        self._token = token_provider
        self._key = key
        self.call_ceiling = max_calls() if call_ceiling is None else call_ceiling
        self.calls = 0
        self.blocked = False
        self.block_reason: str | None = None
        self.matched = 0
        self.rejected = 0  # a result came back but its address disagreed with the county's

    @property
    def budget_left(self) -> int:
        return max(self.call_ceiling - self.calls, 0)

    async def lookup(self, *, name: str, street: str, city: str, zip5: str) -> PlaceResult | None:
        """One text search. None when there's no usable match, the budget is
        spent, or the run is blocked -- never raises for a single lookup, the
        same soft-fail contract `reports.PoliteFetcher` follows."""
        if self.blocked or self.budget_left <= 0 or not name:
            return None
        query = " ".join(p for p in (name, street, city, "CA", zip5) if p)
        headers = {"X-Goog-FieldMask": FIELD_MASK, "Content-Type": "application/json"}
        if self._key:
            headers["X-Goog-Api-Key"] = self._key
        else:
            headers["Authorization"] = f"Bearer {self._token()}"
        self.calls += 1
        try:
            resp = await self._client.post(
                SEARCH_URL, json={"textQuery": query, "maxResultCount": 1}, headers=headers
            )
        except httpx.TransportError:
            return None
        if resp.status_code in (401, 403, 429):
            # API disabled, billing off, credential wrong, or quota gone: every
            # later call this run fails the same way, so stop paying for them.
            self.blocked = True
            self.block_reason = f"Places returned HTTP {resp.status_code}: {resp.text[:200]}"
            return None
        if resp.status_code != 200:
            return None
        places = (resp.json() or {}).get("places") or []
        if not places:
            return None
        place = places[0]
        matched_address = place.get("formattedAddress") or ""
        if not is_same_place(street=street, zip5=zip5, matched_address=matched_address):
            self.rejected += 1
            return None
        self.matched += 1
        return PlaceResult(
            phone=normalize_phone(place.get("nationalPhoneNumber")),
            website=place.get("websiteUri") or None,
            business_status=place.get("businessStatus") or None,
            matched_name=(place.get("displayName") or {}).get("text") or "",
            matched_address=matched_address,
        )
