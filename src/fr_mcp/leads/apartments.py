"""Apartment complexes, and the companies that manage more than one of them.

Two questions this answers, from one pull: which apartment properties are big
enough to have somebody on site a rep can walk in and talk to, and which
management companies hold a portfolio rather than a single building.

**Why this is a Google sweep and not an assessor pull.** The obvious source is
the county parcel roll -- owner name, mailing address, unit count -- and the
plan was built around clustering LLCs by their shared mailing address. Checked
live 2026-09-09: Sacramento County's public parcel layer publishes `APN`,
address, land use and lot size and **no owner, no mailing address and no unit
count**. The whole ownership-clustering approach dies there, and with it the
"16+ units means a resident manager by law" filter, because nothing public
carries the unit count.

What the parcel layer does give is scale: 3,516 "Low Rise Apartment" and 91
"High Rise Apartment" parcels in Sacramento County alone. So the buildings are
knowable; their owners are not.

Google's `apartment_complex` listings replace both halves, and better in one
respect:

- **Posted leasing-office hours are a direct observation of the thing that was
  being inferred.** "16 or more units" was only ever a proxy for "somebody is
  there during the day"; `regularOpeningHours` says so outright, and a rep can
  read the actual hours before driving over. Verified live: every one of the
  first 20 results carried hours, a phone and a website.
- **Review count stands in for size.** No public source carries unit counts, and
  a 443-review property is reliably a bigger operation than an 80-review one. It
  is a proxy and is labelled as one -- never presented as a unit count.

Manager portfolios then come from what the complexes share. Two properties on
one phone number or one website domain are run by one company; that is the same
inference the mailing-address plan rested on, applied to data that is actually
public. It finds the managers who centralise their phones and sites, which is
most of the ones worth calling, and misses an owner who keeps every property
fully separate.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from . import places, regions

# Google's own types for the thing. `condominium_complex` is deliberately absent:
# a condo building is an HOA, not a managed rental, and the buyer is different.
APARTMENT_TYPES = frozenset({"apartment_complex", "apartment_building"})

# Anything that turns up under these keywords and is not a property.
NOT_A_PROPERTY_TYPES = frozenset(
    {
        "real_estate_agency", "corporate_office", "storage", "self_storage",
        "moving_company", "insurance_agency", "bank", "atm", "point_of_interest",
        "general_contractor", "lawyer", "hotel", "motel", "extended_stay_hotel",
    }
)

# Keywords, run over the coverage rectangle and split into quadrants where they
# saturate (see places.PlacesClient.sweep). The plain city-less terms saturate
# immediately across a region this size, which is exactly what the quadrant split
# is for; the qualified ones reach properties the generic terms rank below.
SWEEP_QUERIES: tuple[str, ...] = (
    "apartment complex", "apartments", "apartment building", "apartment homes",
    "luxury apartments", "affordable apartments", "senior apartments",
    "student apartments", "income restricted apartments", "townhomes for rent",
    "apartment community", "gated apartments", "furnished apartments",
    "leasing office", "apartment leasing office",
)

SWEEP_TYPED_QUERIES: tuple[tuple[str, str], ...] = (
    ("apartment_complex", "apartments"),
    ("apartment_building", "apartments"),
)

# Naming a city reaches properties the region-wide terms rank below, and it is
# cheaper per new find than splitting the rectangle again: a quadrant split costs
# 4x the requests for the same keyword, while a city query costs one and changes
# what Google ranks. Every incorporated place inside the 28-mile circle, plus the
# unincorporated communities big enough to be a Google locality.
SWEEP_CITIES: tuple[str, ...] = (
    "Sacramento", "West Sacramento", "Elk Grove", "Rancho Cordova", "Citrus Heights",
    "Folsom", "Roseville", "Rocklin", "Lincoln", "Loomis", "Granite Bay",
    "Carmichael", "Fair Oaks", "Orangevale", "Antelope", "North Highlands",
    "Rio Linda", "Elverta", "Arden-Arcade", "Natomas", "Davis", "Woodland",
    "Winters", "Dixon", "El Dorado Hills", "Auburn", "Galt", "Wilton",
    "Gold River", "Rosemont", "Foothill Farms", "La Riviera", "Parkway",
    "Vineyard", "Florin", "Laguna", "Midtown Sacramento", "Downtown Sacramento",
    "Land Park", "Oak Park", "Del Paso Heights", "Meadowview", "Pocket",
)

# The per-city terms. Kept short deliberately -- each one multiplies by the city
# list, so a fourth term is another 43 requests.
CITY_QUERY_TEMPLATES: tuple[str, ...] = (
    "apartments in {city}",
    "apartment complex {city}",
    "apartment leasing office {city}",
)


def city_queries(cities: tuple[str, ...] = SWEEP_CITIES) -> tuple[str, ...]:
    return tuple(t.format(city=c) for c in cities for t in CITY_QUERY_TEMPLATES)


def all_queries() -> tuple[str, ...]:
    return SWEEP_QUERIES + city_queries()

# On-site presence, read off the posted hours.
TIER_OFFICE = "Leasing office (posted hours)"
TIER_ONSITE = "On-site contact, no posted hours"
TIER_UNKNOWN = "No on-site contact found"

_DOMAIN_RE = re.compile(r"^(?:https?://)?(?:www\.)?([^/:?#]+)", re.I)
_WS_RE = re.compile(r"\s+")

# Domains that host many unrelated properties, so sharing one proves nothing about
# common management. Listing-portal and website-builder domains, mostly.
_GENERIC_DOMAINS = frozenset(
    {
        "apartments.com", "zillow.com", "rent.com", "apartmentguide.com", "trulia.com",
        "facebook.com", "instagram.com", "yelp.com", "google.com", "sites.google.com",
        "wixsite.com", "wordpress.com", "squarespace.com", "godaddysites.com",
        "business.site", "linktr.ee", "hotpads.com", "forrent.com", "padmapper.com",
    }
)

# Toll-free numbers are call-centre or answering-service lines. Two properties
# sharing one is weak evidence of anything -- it may be one manager, or it may be
# the same lead-capture vendor sold to two of them.
_TOLL_FREE = frozenset({"800", "833", "844", "855", "866", "877", "888"})

# The national operators. Kept, because they are real accounts and some regions
# buy locally, but flagged: procurement is corporate, the same call the Leads tab
# makes about grocery chains.
NATIONAL_MANAGERS_RE = re.compile(
    r"\b(GREYSTAR|FPI MANAGEMENT|LINCOLN PROPERTY|AVALON ?BAY|AVALON|ESSEX|"
    r"EQUITY RESIDENTIAL|CAMDEN|MAA\b|UDR\b|AIMCO|BOZZUTO|ALLIANCE RESIDENTIAL|"
    r"PINNACLE|RIVERSTONE|CUSHMAN|JLL\b|CBRE|SARES ?REGIS|PROMETHEUS|"
    r"THOMPSON NATIONAL|ROBERTS ?COMPANIES|JOHN STEWART|EAH HOUSING|USA PROPERTIES)\b",
    re.I,
)


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", (value or "").replace("\xa0", " ")).strip()


def domain_of(website: str | None) -> str:
    """The registrable-ish domain, lowercased. Not a full public-suffix parse --
    it only has to be stable enough that two pages on one company's site collapse
    to the same string."""
    m = _DOMAIN_RE.match((website or "").strip())
    if not m:
        return ""
    host = m.group(1).lower()
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_generic_domain(domain: str) -> bool:
    return domain in _GENERIC_DOMAINS


def is_toll_free(phone: str) -> bool:
    return len(phone or "") == 10 and phone[:3] in _TOLL_FREE


@dataclass
class Complex:
    """One apartment property."""

    key: str
    name: str
    phone: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    zip5: str = ""
    lat: float | None = None
    lng: float | None = None
    distance_miles: float | None = None
    region_id: int = 0
    region_name: str | None = None
    hours: str = ""  # "Mon-Fri 9:00 AM - 6:00 PM"-ish, as Google posts it
    open_days: int = 0
    review_count: int = 0
    business_status: str = ""
    manager: str = ""  # filled by `group_managers` when the property clusters
    manager_properties: int = 0
    is_national: bool = False

    @property
    def onsite_tier(self) -> str:
        """What a rep can expect to find if they turn up.

        Posted hours mean a staffed leasing office. No hours but a local direct
        line usually means a resident manager -- reachable, but by phone first.
        This replaces the "16+ units" rule the plan opened with, because no public
        source carries unit counts and hours observe the same fact directly."""
        if self.open_days:
            return TIER_OFFICE
        if self.phone and not is_toll_free(self.phone):
            return TIER_ONSITE
        return TIER_UNKNOWN

    @property
    def size_hint(self) -> str:
        """Review count bucketed. Explicitly a proxy: no public source carries the
        unit count, and this is what correlates with it."""
        if self.review_count >= 250:
            return "large"
        if self.review_count >= 100:
            return "medium"
        if self.review_count >= 25:
            return "small"
        return "unknown"

    @property
    def in_range(self) -> bool:
        return self.distance_miles is not None and self.distance_miles <= MAX_MILES


MAX_MILES = 28.0


def from_place(row: places.PlaceRow) -> Complex | None:
    """A swept Places row as an apartment property, or None if it is not one."""
    if row.permanently_closed:
        return None
    if row.primary_type in NOT_A_PROPERTY_TYPES:
        return None
    # The keywords pull in management offices and listing agencies as well as
    # properties. Only Google's own apartment types are kept, so "Acme Property
    # Management" lands on the managers tab via its properties, not as a building.
    if row.primary_type not in APARTMENT_TYPES:
        return None

    address = row.address or ""
    lat, lng = row.lat, row.lng
    distance = regions.haversine_miles(lat, lng) if lat is not None and lng is not None else None
    zip5 = places.zip_from_address(address)
    region_id, region_name = regions.region_for_zip(zip5)
    day_lines = [d for d in row.opening_hours if ":" in d]
    return Complex(
        key=f"APT:{row.place_id}",
        name=_clean(row.name),
        phone=row.phone or "",
        website=row.website or "",
        address=_clean(places.street_from_address(address)),
        city=_clean(places.city_from_address(address)),
        zip5=zip5,
        lat=lat,
        lng=lng,
        distance_miles=distance,
        region_id=region_id,
        region_name=region_name,
        hours=_summarise_hours(day_lines),
        open_days=sum(1 for d in day_lines if "closed" not in d.lower()),
        review_count=row.review_count,
        business_status=row.business_status or "",
        is_national=bool(NATIONAL_MANAGERS_RE.search(row.name or "")),
    )


def _summarise_hours(day_lines: list[str]) -> str:
    """Google posts seven "Monday: 10:00 AM - 6:00 PM" lines. A rep needs the shape
    of the week, not all seven, so identical consecutive days collapse."""
    if not day_lines:
        return ""
    parsed = []
    for line in day_lines:
        day, _, rest = line.partition(":")
        parsed.append((day.strip()[:3], _WS_RE.sub(" ", rest.replace(" ", " ").replace(" ", " ")).strip()))
    runs: list[list] = []
    for day, hours in parsed:
        if runs and runs[-1][2] == hours:
            runs[-1][1] = day
        else:
            runs.append([day, day, hours])
    return "; ".join(
        f"{a}-{b} {h}" if a != b else f"{a} {h}" for a, b, h in runs
    )


# --- manager portfolios ---------------------------------------------------


@dataclass
class Manager:
    """A company that appears to run more than one property here."""

    key: str
    name: str
    phone: str = ""
    website: str = ""
    domain: str = ""
    properties: list[str] = field(default_factory=list)
    cities: list[str] = field(default_factory=list)
    total_reviews: int = 0
    with_office: int = 0
    is_national: bool = False
    basis: str = ""  # what tied the properties together

    @property
    def property_count(self) -> int:
        return len(self.properties)


def _cluster_key(c: Complex) -> tuple[str, str] | None:
    """What ties this property to others, best evidence first.

    A shared website domain beats a shared phone: a domain is bought by the
    company, whereas a toll-free number may belong to a lead-capture vendor two
    unrelated properties both hired. A per-property branded domain
    (elevatetolarkspurwoods.com) simply clusters with nothing, which is correct --
    it is evidence of nothing either way."""
    domain = domain_of(c.website)
    if domain and not is_generic_domain(domain):
        return ("domain", domain)
    if c.phone and not is_toll_free(c.phone):
        return ("phone", c.phone)
    return None


def group_managers(complexes: list[Complex], *, min_properties: int = 2) -> list[Manager]:
    """Companies holding `min_properties` or more, and the tie back onto each
    property's `manager` field.

    `min_properties=2` is the whole point: a manager with one building is a
    landlord, and the ask was for companies running several."""
    buckets: dict[tuple[str, str], list[Complex]] = defaultdict(list)
    for c in complexes:
        bucket = _cluster_key(c)
        if bucket is not None:
            buckets[bucket].append(c)

    managers: list[Manager] = []
    for (basis, value), members in buckets.items():
        if len(members) < min_properties:
            continue
        ranked = sorted(members, key=lambda c: -c.review_count)
        national = any(m.is_national for m in members)
        manager = Manager(
            key=f"MGR:{basis}:{value}",
            name=_manager_name(basis, value, members, ranked),
            phone=next((m.phone for m in ranked if m.phone), ""),
            website=next((m.website for m in ranked if m.website), ""),
            domain=value if basis == "domain" else "",
            properties=[m.name for m in ranked],
            cities=sorted({m.city for m in members if m.city}),
            total_reviews=sum(m.review_count for m in members),
            with_office=sum(1 for m in members if m.open_days),
            is_national=national,
            basis="shared website" if basis == "domain" else "shared phone line",
        )
        managers.append(manager)
        for m in members:
            m.manager = manager.name
            m.manager_properties = manager.property_count
            m.is_national = m.is_national or national

    managers.sort(key=lambda m: (-m.property_count, -m.total_reviews, m.name))
    return managers


def _manager_name(basis: str, value: str, members: list[Complex], ranked: list[Complex]) -> str:
    """What to call the company.

    For a domain cluster the domain *is* the identity, and naming the group after
    one of its properties actively misleads -- the 31 properties on `usamfm.com`
    are USA Multifamily Management, not "Terracina at Park Meadows". A shared
    prefix is better still when there is one (rare: managers brand each property
    separately on purpose). For a phone cluster there is no company name anywhere
    in the data, so the biggest property is the honest label and `basis` says how
    the group was formed."""
    if basis == "domain":
        return _common_name(members) or value
    return _common_name(members) or ranked[0].name


_NAME_NOISE_RE = re.compile(r"\b(APARTMENTS?|APARTMENT HOMES|APTS?|COMMUNITY|COMMUNITIES|"
                            r"TOWNHOMES?|VILLAS?|RESIDENCES?|LIVING|AT|THE|OF)\b", re.I)


def _common_name(members: list[Complex]) -> str:
    """A name for the group. Properties under one manager rarely share a name --
    they are branded individually -- so this only fires when there genuinely is a
    common prefix, and otherwise the caller falls back to the biggest property."""
    names = [_NAME_NOISE_RE.sub(" ", m.name).strip() for m in members]
    if len(names) < 2:
        return ""
    first = names[0].split()
    for size in range(len(first), 0, -1):
        prefix = " ".join(first[:size])
        if len(prefix) >= 4 and all(n.startswith(prefix) for n in names):
            return prefix
    return ""
