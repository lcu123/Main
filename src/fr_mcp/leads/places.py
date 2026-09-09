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
from typing import Any, Callable, Sequence

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

# --- discovery sweep -----------------------------------------------------
#
# The enrichment path above answers "what is this known facility's phone". The
# sweep answers a different question -- "what food plants exist here that no
# registry lists" -- and it is the only way to reach them: verified against
# Google's own place-type table, there is no type for food processing, factory,
# warehouse, distribution centre or cold storage, so a type-driven Nearby Search
# cannot find them at all. Keyword Text Search can.
#
# Measured live 2026-09-09 against the 28-mile rectangle: a query returns 20 per
# page and **60 at most, over three pages** -- page 3 comes back with no
# nextPageToken even when the results are clearly not exhausted. So 60 is a hard
# ceiling per query, and a query that hits it has more to give.
SWEEP_PAGE_SIZE = 20
SWEEP_MAX_PAGES = 3
SWEEP_CAP = SWEEP_PAGE_SIZE * SWEEP_MAX_PAGES  # 60: a query returning this is saturated

# Relevance decays hard across those three pages -- page 3 of "food processing
# plant" returned a garden centre, a farm and an urban-agriculture nonprofit. The
# answer to "more coverage" is therefore more, narrower queries and smaller
# rectangles, never deeper paging, and `primaryType` is what keeps the tail
# usable. These three lists come from a live 15-keyword probe (197 unique places)
# rather than from Google's documentation.
#
# Types that are a processor, producer or wholesaler on their face.
PROCESSOR_TYPES = frozenset(
    {
        "manufacturer", "wholesaler", "butcher_shop", "winery", "brewery",
        "coffee_roastery", "supplier", "distillery", "dairy",
    }
)
# `bakery` and `cake_shop` are deliberately NOT in that set even though bakeries
# are in scope by the owner's decision. Google gives a wholesale plant and a
# retail counter the same type, and the first live sweep proved how lopsided that
# is: 232 of 588 "confident" rows were bakeries, and the nearest ones were Crumbl
# Cookies, Nothing Bundt Cakes, Paris Baguette, Safeway Bakery and Costco Bakery.
# A bakery is therefore confident only when its *name* says production --
# `food.BAKERY_PRODUCTION_RE` -- and otherwise goes to the review queue.
BAKERY_TYPES = frozenset({"bakery", "cake_shop"})
# Types that are a storefront or nothing to do with food. The Leads tab works
# retail off the county feeds; a lead on both tabs is a rep dialling twice.
RETAIL_TYPES = frozenset(
    {
        # storefronts the Leads tab already works off the county feeds
        "grocery_store", "asian_grocery_store", "supermarket", "convenience_store",
        "market", "department_store", "liquor_store", "candy_store", "chocolate_shop",
        "tea_store", "sporting_goods_store", "store_",
        # food service
        "restaurant", "cafe", "coffee_shop", "bar", "meal_takeaway", "meal_delivery",
        "fast_food_restaurant", "pizza_restaurant", "sandwich_shop", "ice_cream_shop",
        "donut_shop", "juice_shop", "dessert_shop", "bagel_shop", "food_court",
        # not a business we sell to at all
        "general_contractor", "point_of_interest", "association_or_organization",
        "service", "gas_station", "car_wash", "storage", "local_government_office",
        "school", "hospital", "lodging", "corporate_office", "real_estate_agency",
        # A farm is not a processing site. "Ruhstaller Farm", "Sunrise Orchards",
        # "Soil Born Farms" all came back under the produce keywords; the plan's
        # scope is processing, manufacturing and storage, so a grower without a
        # packing operation is out. A farm that does pack is normally typed
        # `manufacturer` or named for it, and survives on that.
        "farm",
    }
)
# Everything else -- "food", "food_store", "store", "farm", or no type at all --
# is kept and flagged. A real plant does turn up under those (Blue Diamond reads
# as "food"), and so does a farm stand.
# Plan section 4.3's keyword list. Grouped only for readability -- the sweep runs
# every one of them. Terms that returned nothing useful in the live probe are kept
# anyway: an empty query costs one request and the vocabulary shifts as businesses
# re-describe themselves.
SWEEP_QUERIES: tuple[str, ...] = (
    # processing and manufacturing
    "food processing plant", "food processing facility", "food manufacturer",
    "food manufacturing", "food packaging company", "co-packer", "commercial bakery",
    "wholesale bakery", "tortilla factory", "meat processing", "meat packing",
    "slaughterhouse", "poultry processing", "seafood processor",
    "dairy processing plant", "creamery", "cheese manufacturer", "egg processing",
    "produce packing", "fruit packing house", "nut processing", "almond processor",
    "rice mill", "flour mill", "feed mill", "cannery", "frozen food manufacturer",
    "snack food manufacturer", "candy manufacturer", "spice manufacturer",
    "sauce manufacturer", "pet food manufacturer", "ice manufacturer",
    # beverage
    "beverage manufacturer", "bottling plant", "juice processing",
    "coffee roaster wholesale", "brewery", "brewery production facility", "winery",
    "winery production facility", "distillery",
    # bakery -- in scope by the owner's decision, so the bare term runs too
    "bakery",
    # storage and distribution
    "food storage facility", "cold storage warehouse", "refrigerated warehouse",
    "food distribution center", "food distributor", "wholesale grocer",
    "produce distributor", "meat distributor", "seafood distributor",
    "beverage distributor", "food warehouse", "food bank warehouse",
    # kitchens -- institutional is in scope, flagged (plan 13, answer 4)
    "commissary kitchen", "central kitchen", "catering commissary",
    "school district central kitchen",
)

# Type-driven passes: (includedType, textQuery), run with strictTypeFiltering so
# Google filters rather than merely ranking. These catch plants whose name and
# description use none of the words above.
SWEEP_TYPED_QUERIES: tuple[tuple[str, str], ...] = (
    ("manufacturer", "food"),
    ("wholesaler", "food"),
    ("supplier", "food"),
    ("bakery", "wholesale bakery"),
)

SWEEP_FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.primaryType,"
    "places.nationalPhoneNumber,places.websiteUri,places.businessStatus,"
    "places.location,nextPageToken"
)

_DIGITS_RE = re.compile(r"\D+")
_HOUSE_NUMBER_RE = re.compile(r"^\s*(\d+)")


class PlacesError(RuntimeError):
    """Places refused the request in a way that makes retrying pointless this
    run (API disabled, billing off, quota exhausted)."""


@dataclass(frozen=True)
class PlaceRow:
    """One business found by the discovery sweep, before classification."""

    place_id: str
    name: str
    address: str
    primary_type: str
    phone: str | None
    website: str | None
    business_status: str | None
    lat: float | None
    lng: float | None

    @property
    def key(self) -> str:
        return f"PLACES:{self.place_id}"

    @property
    def permanently_closed(self) -> bool:
        return self.business_status == PERMANENTLY_CLOSED


def zip_from_address(address: str) -> str:
    """Places returns one formatted string, not components -- the field mask that
    would break it out costs a more expensive SKU. The zip is the only part the
    sheet needs separately, and a five-digit run before the country suffix is
    unambiguous in a US address."""
    m = re.search(r"\b(\d{5})(?:-\d{4})?\b(?=[^0-9]*$)", address or "")
    return m.group(1) if m else ""


def street_from_address(address: str) -> str:
    """The first comma-separated part: "1802 C St, Sacramento, CA 95811, USA"."""
    return (address or "").split(",")[0].strip()


def city_from_address(address: str) -> str:
    parts = [p.strip() for p in (address or "").split(",")]
    return parts[1] if len(parts) >= 3 else ""


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


@dataclass(frozen=True)
class Rect:
    """A lat/lng rectangle. Text Search's `locationRestriction` accepts a
    rectangle and nothing else -- no circle, no polygon -- so the 28-mile circle
    is searched as its bounding box and trimmed to the circle afterwards."""

    south: float
    west: float
    north: float
    east: float

    def quadrants(self) -> tuple["Rect", "Rect", "Rect", "Rect"]:
        mid_lat = (self.south + self.north) / 2
        mid_lng = (self.west + self.east) / 2
        return (
            Rect(self.south, self.west, mid_lat, mid_lng),
            Rect(self.south, mid_lng, mid_lat, self.east),
            Rect(mid_lat, self.west, self.north, mid_lng),
            Rect(mid_lat, mid_lng, self.north, self.east),
        )


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

    async def _post(self, body: dict[str, Any], field_mask: str) -> dict[str, Any] | None:
        """One billed request, with the budget and circuit breaker applied. None
        on anything that is not a usable 200 -- every caller soft-fails, the same
        contract `reports.PoliteFetcher` follows."""
        headers = {"X-Goog-FieldMask": field_mask, "Content-Type": "application/json"}
        if self._key:
            headers["X-Goog-Api-Key"] = self._key
        else:
            headers["Authorization"] = f"Bearer {self._token()}"
        self.calls += 1
        try:
            resp = await self._client.post(SEARCH_URL, json=body, headers=headers)
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
        return resp.json() or {}

    async def lookup(self, *, name: str, street: str, city: str, zip5: str) -> PlaceResult | None:
        """One text search for a facility we already know about. None when there's
        no usable match, the budget is spent, or the run is blocked."""
        if self.blocked or self.budget_left <= 0 or not name:
            return None
        query = " ".join(p for p in (name, street, city, "CA", zip5) if p)
        payload = await self._post({"textQuery": query, "maxResultCount": 1}, FIELD_MASK)
        if payload is None:
            return None
        places = payload.get("places") or []
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

    async def search_text(
        self, query: str, rect: "Rect", *, included_type: str | None = None
    ) -> tuple[list[PlaceRow], bool]:
        """(rows, saturated) for one keyword over one rectangle.

        `saturated` means the query came back with the full 60 the API will give,
        so there are more businesses in this rectangle than it can return and the
        caller should split it. Pages stop early when Google stops handing out a
        nextPageToken, which it does before 60 on most queries."""
        rows: dict[str, PlaceRow] = {}
        token: str | None = None
        for _ in range(SWEEP_MAX_PAGES):
            if self.blocked or self.budget_left <= 0:
                break
            body: dict[str, Any] = {
                "textQuery": query,
                "pageSize": SWEEP_PAGE_SIZE,
                "locationRestriction": {
                    "rectangle": {
                        "low": {"latitude": rect.south, "longitude": rect.west},
                        "high": {"latitude": rect.north, "longitude": rect.east},
                    }
                },
            }
            if included_type:
                body["includedType"] = included_type
                body["strictTypeFiltering"] = True
            if token:
                body["pageToken"] = token
            payload = await self._post(body, SWEEP_FIELD_MASK)
            if payload is None:
                break
            for raw in payload.get("places") or []:
                row = _row_from(raw)
                if row is not None:
                    rows[row.place_id] = row
            token = payload.get("nextPageToken")
            if not token:
                break
        return list(rows.values()), len(rows) >= SWEEP_CAP

    async def sweep(
        self,
        queries: Sequence[str],
        rect: "Rect",
        *,
        typed_queries: Sequence[tuple[str, str]] = (),
        max_depth: int = 2,
    ) -> dict[str, PlaceRow]:
        """Every keyword over the rectangle, splitting into quadrants wherever a
        keyword saturates, de-duplicated by place ID.

        `max_depth` bounds the recursion rather than the plan's "until no quadrant
        saturates": each level multiplies the request count by four, and the
        budget is the real constraint. A still-saturated quadrant at the bottom
        simply returns its 60 -- some coverage lost, no run blown."""
        found: dict[str, PlaceRow] = {}

        async def run(query: str, box: "Rect", depth: int, included_type: str | None) -> None:
            if self.blocked or self.budget_left <= 0:
                return
            rows, saturated = await self.search_text(query, box, included_type=included_type)
            for row in rows:
                found.setdefault(row.place_id, row)
            if saturated and depth < max_depth:
                for quadrant in box.quadrants():
                    await run(query, quadrant, depth + 1, included_type)

        for query in queries:
            await run(query, rect, 0, None)
        for included_type, query in typed_queries:
            await run(query, rect, 0, included_type)
        return found


def _row_from(raw: dict[str, Any]) -> PlaceRow | None:
    place_id = raw.get("id")
    name = (raw.get("displayName") or {}).get("text") or ""
    if not place_id or not name:
        return None
    location = raw.get("location") or {}
    return PlaceRow(
        place_id=place_id,
        name=name,
        address=raw.get("formattedAddress") or "",
        primary_type=raw.get("primaryType") or "",
        phone=normalize_phone(raw.get("nationalPhoneNumber")),
        website=raw.get("websiteUri") or None,
        business_status=raw.get("businessStatus") or None,
        lat=location.get("latitude"),
        lng=location.get("longitude"),
    )
