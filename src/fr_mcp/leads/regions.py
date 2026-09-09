"""Zip -> Zest regionID mapping and distance from the office.

Owner-reviewed defaults for Sacramento County (see docs/lead-scraper-plan.md
section 12, decision 2, and the office coordinates in section 3.2). Zips not
listed here are "unmapped": regionID 0, and the geo multiplier takes an extra
penalty so an unmapped-territory lead never quietly outranks a mapped one.
"""

from __future__ import annotations

import math

# 6948 W 2nd St, Rio Linda, CA 95673 -- Zest's office. Geocoded 2026-09-09 against
# the US Census geocoder (Public_AR_Current), which returned this address exactly and
# agrees with docs/food-facilities-plan.md's appendix. The rounder pair that shipped
# here first (38.691, -121.448) was off by 0.99 miles, almost all of it in longitude
# -- enough to move a facility across one of geo_multiplier's band edges.
OFFICE_LAT = 38.6941
OFFICE_LNG = -121.4660

# Zip -> (regionID, region name), owner-reviewed 2026-09-07. Elk Grove and Galt are
# folded into South Sacramento (4) per the plan; Natomas into North Highlands (8);
# everything else in the county that isn't listed here is unmapped (regionID 0).
ZIP_REGION: dict[str, tuple[int, str]] = {}


def _add(region_id: int, name: str, *zips: str) -> None:
    for z in zips:
        ZIP_REGION[z] = (region_id, name)


_add(3, "Downtown", "95811", "95814", "95816", "95817", "95818", "95819")
_add(
    4,
    "South Sacramento",
    "95820", "95822", "95823", "95824", "95826", "95828", "95829", "95831", "95832",
    # Elk Grove
    "95624", "95757", "95758",
    # Galt
    "95632",
)
_add(
    5,
    "Carmichael",
    "95608", "95821", "95825", "95864",
    # Fair Oaks / Orangevale
    "95628", "95662",
)
_add(2, "Rancho Cordova", "95670", "95742", "95827", "95655", "95683")
_add(
    8,
    "North Highlands / Antelope / Rio Linda",
    "95660", "95673", "95841", "95842", "95843", "95652", "95626", "95837",
    # Natomas
    "95833", "95834", "95835", "95838",
)
_add(9, "Citrus Heights", "95610", "95621")
_add(10, "Folsom", "95630")
_add(6, "West Sacramento", "95691", "95605")

# Placer County, owner-reviewed 2026-09-07 (docs/lead-scraper-plan.md 2.6, 12, 13):
# Roseville splits across regions 1 (Roseville B) and 7 (Roseville A / Granite Bay) --
# the owner hasn't drawn that line yet, so every Roseville zip defaults to 7 for now;
# revisit once the owner splits it. Rocklin/Loomis/Lincoln -> 11.
_add(7, "Roseville A / Granite Bay", "95661", "95678", "95747", "95746")
_add(11, "Rocklin, Loomis & Lincoln", "95677", "95765", "95648", "95650")

# Placer and Yolo portal rows carry no lat/lng (unlike the Sacramento ArcGIS feed) --
# approximate zip centroids (api.zippopotam.us, queried 2026-09-08) stand in until
# Google Places enrichment lands (plan section 6, phase C).
ZIP_CENTROID: dict[str, tuple[float, float]] = {
    "95661": (38.7346, -121.2340),  # Roseville
    "95678": (38.7609, -121.2867),  # Roseville
    "95747": (38.7703, -121.3372),  # Roseville
    "95746": (38.7435, -121.1897),  # Granite Bay
    "95677": (38.7877, -121.2366),  # Rocklin
    "95765": (38.8136, -121.2677),  # Rocklin
    "95648": (38.8942, -121.2908),  # Lincoln
    "95650": (38.8071, -121.1698),  # Loomis
    "95605": (38.5927, -121.5325),  # West Sacramento
    "95691": (38.5673, -121.5516),  # West Sacramento
}


def centroid_for_zip(zip5: str) -> tuple[float, float] | None:
    return ZIP_CENTROID.get(zip5.strip()[:5])


# Area codes covering the three counties this pipeline works: 916 and its 279
# overlay for Sacramento/Placer, 530 for the outlying north-state edges.
LOCAL_AREA_CODES = frozenset({"916", "279", "530"})


def is_local_number(phone: str | None) -> bool:
    """A county-published number whose area code is from somewhere else is a
    warning sign, not a curiosity. Audited live 2026-09-08 across 30 Sacramento
    rows: every out-of-area number checked (3 of 3) disagreed with the business's
    listed line, including a Long Island number on a Folsom restaurant and one
    that turned out to be a digit-transposed local number. Roughly a quarter of
    county numbers differ from the business line overall, and this is the cheap
    half of that -- detectable with no API call at all."""
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return len(digits) == 10 and digits[:3] in LOCAL_AREA_CODES


def region_for_zip(zip5: str) -> tuple[int, str | None]:
    """(regionID, region name); regionID 0 and name None when the zip isn't mapped."""
    hit = ZIP_REGION.get(zip5.strip()[:5])
    return hit if hit else (0, None)


def haversine_miles(lat: float, lng: float) -> float:
    """Great-circle distance in miles from the office. `lat`/`lng` are the facility's."""
    r = 3958.8
    lat1, lng1, lat2, lng2 = map(math.radians, (OFFICE_LAT, OFFICE_LNG, lat, lng))
    dlat, dlng = lat2 - lat1, lng2 - lng1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def geo_multiplier(distance_miles: float | None, *, region_mapped: bool) -> float:
    """Distance-based multiplier on the total score (plan section 3.2)."""
    if distance_miles is None:
        mult = 0.75  # no coordinates (34 Sacramento rows lack geometry): treat as mid-range
    elif distance_miles <= 10:
        mult = 1.00
    elif distance_miles <= 20:
        mult = 0.90
    elif distance_miles <= 30:
        mult = 0.75
    else:
        mult = 0.50
    if not region_mapped:
        mult = max(0.0, mult - 0.05)
    return mult
