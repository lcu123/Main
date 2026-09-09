"""One row per food facility, merged from every registry that knows about it.

This is the Food Facilities tab's model layer: it takes what the source adapters
return (`cdfa.Licensee`, `calepa.Site`, later FSIS and Places), decides what kind of
business each one is, folds the duplicates together, and hands `sheet.py` something
already shaped like a call list.

Three things here are worth reading before changing them.

**A registry's own category is a hint, not a fact.** CalEPA files J W AUTO WRECKERS
under NAICS 311 (food manufacturing) and CDFA licenses individual produce brokers
who work out of a house. Classification therefore runs the name against the
category, and a disagreement sets `needs_review` rather than picking a winner --
plan section 6 column T. Dropping on a name alone would lose real plants with
unhelpful names ("MO West Sac"); trusting the code alone puts a wrecking yard in
front of a rep.

**Retail and restaurants belong to the other tab.** The Leads tab already works
restaurants and markets off the county inspection feeds. Anything that reads as a
storefront is dropped here (`RETAIL_RE`), because a lead in two tabs is a rep
calling the same number twice with two different pitches. Bakeries, breweries and
wineries are the deliberate exception -- they are in scope by the owner's decision
even though many of them also have a counter.

**Identity is the address, not the name.** The same plant appears as two CalEPA
SiteIDs (BLUE DIAMOND GROWERS twice, one row with a phone and one without) and
again under a CDFA licence number, and no source shares an ID with any other. The
merge key is house-number + zip, which survives "TONY'S FINE FOODS" vs "TONY'S FINE
FOODS/UNFI"; the `key` written to the sheet stays the first source's own record ID
so a row's provenance is still traceable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from . import calepa, cdfa, fsis, regions
from .reports import is_entity_name

# --- categories ----------------------------------------------------------
#
# Plain-English types from plan section 7, in the order they are tested. First hit
# wins, so the specific ones come before the general ones ("nut & rice processing"
# before "other food manufacturing").

CAT_MEAT = "Meat & poultry processing"
CAT_SEAFOOD = "Seafood processing"
CAT_DAIRY = "Dairy / creamery"
CAT_BAKERY = "Commercial bakery / tortilla"
CAT_PRODUCE = "Produce packing / nut & rice processing"
CAT_SNACK = "Snack, candy & confectionery manufacturing"
CAT_SAUCE = "Sauce, spice & prepared foods manufacturing"
CAT_FROZEN = "Frozen & ready-meal manufacturing"
CAT_ALCOHOL = "Brewery / winery / distillery"
CAT_BEVERAGE = "Beverage production (bottling, juice, coffee roasting)"
CAT_COLD = "Cold storage / refrigerated warehouse"
CAT_DISTRIBUTION = "Food distribution / wholesale grocer"
CAT_COMMISSARY = "Commissary / central kitchen"
CAT_PETFOOD = "Pet & animal food manufacturing"
CAT_PACKAGING = "Food packaging / co-packer"
CAT_OTHER = "Other food manufacturing"
CAT_HANDLER = "Farm product dealer / broker"

_NAME_CATEGORIES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CAT_MEAT, re.compile(r"\b(MEAT|POULTRY|BEEF|PORK|LAMB|BUTCHER|SLAUGHTER|JERKY|SAUSAGE|CARNIC\w*)\b", re.I)),
    (CAT_SEAFOOD, re.compile(r"\b(SEAFOOD|FISH|SALMON|CRAB|OYSTER|SHRIMP)\b", re.I)),
    (CAT_DAIRY, re.compile(r"\b(DAIRY|CREAMERY|CREAM|MILK|CHEESE|YOGURT|BUTTER|GELATO|ICE CREAM)\b", re.I)),
    (CAT_BAKERY, re.compile(r"\b(BAKERY|BAKERIES|BAKING|BAKE|TORTILLA|BREAD|PASTRY|DOUGH)\b", re.I)),
    # BRAUEREI has no leading \b on purpose: the real live row is "Sudwerk
    # Privatbrauerei Hubsch", where the word is a suffix, not a standalone token.
    (CAT_ALCOHOL, re.compile(r"(?:\b(?:BREWING|BREWERY|WINERY|VINEYARD|CELLARS|DISTILL\w*|CIDER|MEAD)|BRAUEREI)\b", re.I)),
    (CAT_PETFOOD, re.compile(r"\b(PET FOOD|ANIMAL FEED|FEED MILL|LIVESTOCK FEED|MILLING)\b", re.I)),
    (CAT_PRODUCE, re.compile(r"\b(PRODUCE|PACKING|PACKERS|ALMOND|WALNUT|PISTACHIO|NUT|NUTS|RICE|GRAIN|"
                             r"ELEVATOR|ORCHARD|OLIVE|FRUIT|TOMATO|GROWERS|HULLER|SHELLER)\b", re.I)),
    (CAT_SNACK, re.compile(r"\b(CANDY|CONFECTION\w*|CHOCOLATE|SNACK|CHIPS|POPCORN|PRETZEL)\b", re.I)),
    (CAT_SAUCE, re.compile(r"\b(SAUCE|SALSA|SPICE|SEASONING|CONDIMENT|DRESSING|BOTANICAL|EXTRACT)\b", re.I)),
    (CAT_FROZEN, re.compile(r"\b(FROZEN|READY MEAL|ENTREE)\b", re.I)),
    (CAT_BEVERAGE, re.compile(r"\b(BOTTLING|BOTTLERS|BEVERAGE|JUICE|COFFEE|ROASTER|ROASTING|WATER CO|SODA|TEA)\b", re.I)),
    (CAT_COLD, re.compile(r"\b(COLD STORAGE|REFRIGERATED|FREEZER|ICE\b|LINEAGE)\b", re.I)),
    (CAT_COMMISSARY, re.compile(r"\b(COMMISSARY|CENTRAL KITCHEN|CATERING|FOOD BANK|SCHOOL DISTRICT|"
                                r"NUTRITION SERVICES|CAFETERIA)\b", re.I)),
    (CAT_PACKAGING, re.compile(r"\b(CO-?PACK\w*|PACKAGING|CANNING|CANNERY)\b", re.I)),
    (CAT_DISTRIBUTION, re.compile(r"\b(WHOLESALE|DISTRIBUT\w*|FOODSERVICE|FOOD SERVICE|SUPPLY|SUPPLIERS|"
                                  r"IMPORTS|EXPORTS|TRADING|FOODS?)\b", re.I)),
)

# NAICS prefix -> category, used when the name says nothing useful.
_NAICS_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("3116", CAT_MEAT),
    ("3117", CAT_SEAFOOD),
    ("3115", CAT_DAIRY),
    ("3118", CAT_BAKERY),
    ("3111", CAT_PETFOOD),
    ("3112", CAT_PRODUCE),
    ("3113", CAT_SNACK),
    ("3114", CAT_PRODUCE),
    ("3119", CAT_SAUCE),
    ("3121", CAT_BEVERAGE),
    ("3122", CAT_OTHER),
    ("312", CAT_ALCOHOL),
    ("311", CAT_OTHER),
    ("4244", CAT_DISTRIBUTION),
    ("4245", CAT_DISTRIBUTION),
    ("49312", CAT_COLD),
    ("4931", CAT_COLD),
)

# --- what does not belong on this tab ------------------------------------
#
# Restaurants and food retail are the Leads tab's job; a facility in both tabs is a
# rep dialling the same number twice. Bakeries, breweries and wineries are exempt
# by the owner's decision even when they read as a storefront.

RETAIL_RE = re.compile(
    r"\b(RESTAURANT|TAQUERIA|TACO|PIZZA|PIZZERIA|SUSHI|BISTRO|GRILL|DINER|CAFE|CAFÉ|COFFEE SHOP|"
    r"BAR & GRILL|STEAKHOUSE|BUFFET|CANTINA|SANDWICH|SUBS?|BURGER|CHICKEN SHACK|DELI|DELICATESSEN|"
    r"7-ELEVEN|7 ELEVEN|CIRCLE K|AM/?PM|CHEVRON|SHELL OIL|LIQUOR|SMOKE SHOP|MINI MART|MINIMART|"
    r"CONVENIENCE|GAS STATION|SUPERMARKET|GROCERY OUTLET|DOLLAR (GENERAL|TREE)|WALGREENS|CVS)\b",
    re.I,
)
KEEP_ANYWAY_RE = re.compile(r"\b(BAKERY|BAKERIES|BAKING|TORTILLA|BREWING|BREWERY|WINERY|DISTILL\w*)\b", re.I)

# Names that are plainly not food at all, however the registry filed them. Each of
# these was seen live in a NAICS 311 result.
NOT_FOOD_RE = re.compile(
    r"\b(AUTO WRECK\w*|WRECKING|SALVAGE|AUTO BODY|TIRE|MUFFLER|LUMBER|CONCRETE|ASPHALT|"
    r"PLUMBING|ELECTRIC(AL)? SUPPLY|FLOORING|CARPET|HARDWARE|NURSERY|FLORAL|CANNABIS|"
    r"DISPENSARY|CAR WASH|SELF STORAGE|MINI STORAGE|APARTMENT|DEALERSHIP|FUNERAL)\b",
    re.I,
)

_HOUSE_RE = re.compile(r"^\s*(\d+)")
_WS_RE = re.compile(r"\s+")

SOURCE_CDFA = "CDFA"
SOURCE_CALEPA = "CalEPA"
SOURCE_FSIS = "USDA FSIS"


@dataclass
class FoodFacility:
    """One row of the Food Facilities tab, before the sheet writer shapes it."""

    key: str  # the first source's own record ID, prefixed -- never edited by a rep
    name: str
    phone: str = ""
    phone_source: str = ""
    category: str = CAT_OTHER
    address: str = ""
    city: str = ""
    zip5: str = ""
    lat: float | None = None
    lng: float | None = None
    distance_miles: float | None = None
    distance_basis: str = ""  # "coords" | "zip" | "city" | "none"
    region_id: int = 0
    region_name: str | None = None
    sources: list[str] = field(default_factory=list)
    found_via: str = ""
    needs_review: bool = False
    review_reason: str = ""
    address_is_mailbox: bool = False
    places_checked: bool = False  # asked, whatever the answer -- see LeadCandidate.places_checked
    is_facility: bool = True  # False for a licence held by a person, with no plant behind it
    website: str = ""
    business_status: str = ""
    contact_name: str = ""

    @property
    def merge_key(self) -> str:
        """House number + zip. No two sources share an ID, and names differ across
        them ("TONY'S FINE FOODS" vs "TONY'S FINE FOODS/UNFI"), so the address is
        the only thing that identifies a plant. A row with no house number falls
        back to its normalised name so it still merges with itself on a rerun."""
        house = _HOUSE_RE.match(self.address or "")
        if house and self.zip5:
            return f"{house.group(1)}|{self.zip5}"
        return f"~{normalize_name(self.name)}|{self.zip5}"

    @property
    def in_range(self) -> bool:
        return self.distance_miles is not None and self.distance_miles <= MAX_MILES

    @property
    def call_rank(self) -> int:
        """0 for a facility, 1 for a licence held by a person with no plant behind it.

        Plan section 6 asks for the tab sorted by distance, and within a tier it is.
        But nearly half of CDFA's in-range rows are individuals -- a produce broker
        working a phone out of a house -- and a plain distance sort puts four of
        them above Blue Diamond Growers. A rep working top-down should reach the
        plants first, so the person-held licences sink below them and keep their
        own distance order there. They are ranked down, never dropped: they are
        real farm-product handlers and some of them do have a warehouse."""
        return 1 if not self.is_facility else 0


MAX_MILES = 28.0


def normalize_name(name: str) -> str:
    """For comparison only -- strips punctuation, the corporate suffix and the
    store-number tail, so "Blue Diamond Growers, Inc. #2" and "BLUE DIAMOND
    GROWERS" collapse."""
    text = re.sub(r"[^A-Za-z0-9 ]+", " ", (name or "").upper())
    text = re.sub(r"\b(LLC|L L C|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LP|L P|LTD)\b", " ", text)
    text = re.sub(r"\s#?\d+\s*$", " ", text)
    return _WS_RE.sub(" ", text).strip()


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", (value or "").replace("\xa0", " ")).strip()


def category_for(name: str, naics_prefix: str = "") -> tuple[str, bool, str]:
    """(category, needs_review, why). The name is tried first because it is the
    only signal that is about *this* business rather than about a filing clerk's
    choice of code, and a registry code that disagrees with an obvious name is
    exactly the case worth flagging."""
    for category, pattern in _NAME_CATEGORIES:
        if pattern.search(name or ""):
            return category, False, ""
    for prefix, category in _NAICS_CATEGORIES:
        if naics_prefix.startswith(prefix):
            return category, True, f"category from NAICS {naics_prefix} only; the name gives no clue"
    return CAT_OTHER, True, "no category signal in the name or a registry code"


def excluded_reason(name: str) -> str | None:
    """Why this row does not belong on the Food Facilities tab, or None to keep it."""
    if NOT_FOOD_RE.search(name or ""):
        return "not a food business despite the registry's category"
    if RETAIL_RE.search(name or "") and not KEEP_ANYWAY_RE.search(name or ""):
        return "reads as a restaurant or retail storefront -- the Leads tab's territory"
    return None


def _with_region(facility: FoodFacility) -> FoodFacility:
    region_id, region_name = regions.region_for_zip(facility.zip5)
    return replace(facility, region_id=region_id, region_name=region_name)


def from_calepa(site: calepa.Site) -> FoodFacility | None:
    if excluded_reason(site.name):
        return None
    category, review, why = category_for(site.name, site.naics_prefix)
    if site.lat is not None and site.lng is not None:
        distance, basis = regions.haversine_miles(site.lat, site.lng), "coords"
    else:
        centroid, source = regions.locate(site.zip5, site.city)
        distance = regions.haversine_miles(*centroid) if centroid else None
        basis = source
    return _with_region(
        FoodFacility(
            key=site.key,
            name=_clean(site.name),
            phone=site.phone,
            phone_source=f"calepa:{site.phone_role}" if site.phone else "",
            category=category,
            address=_clean(site.address),
            city=_clean(site.city),
            zip5=site.zip5,
            lat=site.lat,
            lng=site.lng,
            distance_miles=distance,
            distance_basis=basis,
            sources=[SOURCE_CALEPA],
            found_via=f"CalEPA NAICS {site.naics_prefix} "
            f"({calepa.NAICS_PREFIXES.get(site.naics_prefix, 'food-related')})",
            needs_review=review,
            review_reason=why,
            contact_name=site.contact_name,
        )
    )


def from_cdfa(licensee: cdfa.Licensee) -> FoodFacility | None:
    if excluded_reason(licensee.name):
        return None
    category, review, why = category_for(licensee.name)
    if category is CAT_OTHER and review:
        # Every CDFA licensee handles California farm products by definition, so an
        # unrecognisable name is a dealer or broker rather than "unknown".
        category, why = CAT_HANDLER, "CDFA licence only; the name does not say what they make"
    distance, basis = cdfa.locate(licensee)
    person = not is_entity_name(licensee.name) and not _TRADE_WORD_RE.search(licensee.name)
    return _with_region(
        FoodFacility(
            key=licensee.key,
            name=_clean(licensee.name),
            phone=licensee.phone,
            phone_source="cdfa" if licensee.phone else "",
            category=category,
            address=_clean(licensee.address),
            city=_clean(licensee.city),
            zip5=licensee.zip5,
            distance_miles=distance,
            distance_basis=basis,
            sources=[SOURCE_CDFA],
            found_via="CDFA Market Enforcement licence",
            needs_review=review or person or licensee.address_is_mailbox,
            review_reason=_cdfa_review_reason(why, person, licensee.address_is_mailbox),
            address_is_mailbox=licensee.address_is_mailbox,
            is_facility=not person,
        )
    )


# A name with no LLC/INC suffix is usually an individual licensee -- a produce
# broker working from a house, not a facility -- unless it carries a trade word.
_TRADE_WORD_RE = re.compile(
    r"\b(FARM|FARMS|PRODUCE|PACKING|PACKERS|RANCH|ORCHARD|ORCHARDS|FOOD|FOODS|COMPANY|"
    r"BROTHERS|BROS|DISTRIBUT\w*|WHOLESALE|MARKET|MILL|MILLING|GROWERS|COOP|COOPERATIVE|"
    r"TRADING|IMPORTS|EXPORTS?|DAIRY|MEAT|SEED|NUT|NUTS|OLIVE|RICE|WINE|WINERY|BREW\w*|"
    r"BAKERY|BAKING|PROCESS\w*|SUPPLY|GROUP|ENTERPRISES|INDUSTRIES|INTERNATIONAL|SALES)\b",
    re.I,
)


def _cdfa_review_reason(base: str, person: bool, mailbox: bool) -> str:
    notes = [n for n in (base,) if n]
    if person:
        notes.append("licensed to an individual -- may be a broker with no facility")
    if mailbox:
        notes.append("mailing address is a PO Box, so the distance is where the mail goes")
    return "; ".join(notes)


# --- merge ---------------------------------------------------------------


def _better_phone(current: FoodFacility, incoming: FoodFacility) -> tuple[str, str]:
    if current.phone:
        return current.phone, current.phone_source
    return incoming.phone, incoming.phone_source


def merge(*groups: list[FoodFacility]) -> list[FoodFacility]:
    """Fold every source's rows into one row per plant, ordered by distance.

    Order matters: the first source to claim a merge key owns the row's `key`, name
    and category, and later sources only fill what is still blank. Pass the source
    with the best identity data first -- CalEPA, which has real coordinates and a
    site address -- and the mailing-address registry after it."""
    merged: dict[str, FoodFacility] = {}
    for group in groups:
        for facility in group:
            existing = merged.get(facility.merge_key)
            if existing is None:
                merged[facility.merge_key] = replace(facility, sources=list(facility.sources))
                continue
            phone, phone_source = _better_phone(existing, facility)
            existing.phone, existing.phone_source = phone, phone_source
            for source in facility.sources:
                if source not in existing.sources:
                    existing.sources.append(source)
            if facility.found_via and facility.found_via not in existing.found_via:
                existing.found_via = f"{existing.found_via}; {facility.found_via}"
            if existing.distance_miles is None or (
                facility.distance_miles is not None
                and _basis_rank(facility.distance_basis) < _basis_rank(existing.distance_basis)
            ):
                existing.distance_miles = facility.distance_miles
                existing.distance_basis = facility.distance_basis
            if not existing.address and facility.address:
                existing.address = facility.address
            if not existing.website and facility.website:
                existing.website = facility.website
            if not existing.contact_name and facility.contact_name:
                existing.contact_name = facility.contact_name
            # Corroboration by a second registry answers the "is this real?" half of
            # a review flag, but not a PO Box or an individual licensee.
            if facility.is_facility and not existing.is_facility:
                # A registry that knows this address as a site outranks a licence
                # held in a person's name at the same address.
                existing.is_facility = True
            if existing.needs_review and not facility.needs_review and not existing.address_is_mailbox:
                existing.needs_review = False
                existing.review_reason = ""

    out = [f for f in merged.values() if f.in_range]
    out.sort(key=lambda f: (f.call_rank, f.distance_miles if f.distance_miles is not None else 999.0, f.name))
    return out


_BASIS_ORDER = {"coords": 0, "zip": 1, "city": 2, "none": 3, "": 3}


def _basis_rank(basis: str) -> int:
    return _BASIS_ORDER.get(basis, 3)


def from_fsis(est: fsis.Establishment) -> FoodFacility | None:
    """FSIS rows never need a category guess: every establishment in the directory
    is a meat, poultry or egg operation by definition, and `activities` says which.
    That makes this the one source that contributes no review flags."""
    if excluded_reason(est.name):
        return None
    # The name still gets first say, so "Pacific Seafood" is not filed as poultry
    # and a jerky plant is not filed as "other". Everything unrecognised falls to
    # meat and poultry, which is what the directory is.
    named, _, _ = category_for(est.name)
    category = named if named is not CAT_OTHER else CAT_MEAT
    if "egg product" in est.activities.lower():
        category = CAT_OTHER
    detail = est.activities or "federally inspected"
    if est.size:
        detail = f"{detail} ({est.size})"
    return _with_region(
        FoodFacility(
            key=est.key,
            name=_clean(est.name),
            phone=est.phone,
            phone_source="fsis" if est.phone else "",
            category=category,
            address=_clean(est.street),
            city=_clean(est.city),
            zip5=est.zip5,
            lat=est.lat,
            lng=est.lng,
            distance_miles=fsis.distance_miles(est),
            distance_basis="coords" if est.lat is not None and est.lng is not None else "zip",
            sources=[SOURCE_FSIS],
            found_via=f"USDA FSIS establishment {est.number} -- {detail}",
        )
    )
