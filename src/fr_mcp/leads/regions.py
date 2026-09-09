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

# Rows from every source but the Sacramento ArcGIS feed arrive without coordinates:
# the Placer and Yolo portals carry none, and CDFA's licence registry publishes a
# *mailing* address only. The zip's centroid stands in, which is an approximation
# either way -- and for a PO Box row it is an approximation of where the mail goes,
# not where the plant is, so `distance (mi)` on those rows is a hint, not a filter
# to trust.
#
# Source: the US Census Bureau's 2024 ZCTA national gazetteer (INTPTLAT/INTPTLONG,
# the ZCTA's internal point), clipped to the 151 ZCTAs within 45 miles of the office
# -- comfortably past the 28-mile cut, so a facility near the edge still gets a real
# distance rather than None. This replaced ten hand-entered api.zippopotam.us points
# on 2026-09-09; the two disagree by up to 4.8 miles (95691, a long thin ZCTA whose
# area centroid sits well south of West Sacramento's built-up part), and one uniform
# definition across every zip beats a better-for-some mixture of two. Rows already in
# the sheet keep the distance they were written with -- `sync_leads` never rewrites
# a tool column on an existing row -- so nothing in the sheet churns.
ZIP_CENTROID: dict[str, tuple[float, float]] = {
    "94512": (38.1311, -121.8217), "94533": (38.2807, -122.0064), "94535": (38.2709, -121.937),
    "94558": (38.3891, -122.1884), "94571": (38.1521, -121.7598), "94585": (38.1918, -121.9383),
    "95220": (38.2002, -121.235), "95227": (38.2104, -121.0513), "95237": (38.154, -121.1466),
    "95240": (38.1235, -121.1591), "95242": (38.1371, -121.3845), "95253": (38.1472, -121.1953),
    "95254": (38.1998, -120.9642), "95258": (38.1721, -121.3089), "95601": (38.4249, -120.8303),
    "95602": (38.9915, -121.1035), "95603": (38.9113, -121.0858), "95605": (38.5935, -121.5399),
    "95606": (38.7564, -122.1933), "95607": (38.8306, -122.126), "95608": (38.6269, -121.3312),
    "95610": (38.6949, -121.2722), "95612": (38.3847, -121.5787), "95613": (38.8126, -120.8917),
    "95614": (38.8862, -120.9797), "95615": (38.3108, -121.5418), "95616": (38.5541, -121.7985),
    "95618": (38.543, -121.6905), "95619": (38.6806, -120.8143), "95620": (38.4049, -121.7544),
    "95621": (38.6955, -121.3086), "95623": (38.6325, -120.844), "95624": (38.4342, -121.3059),
    "95625": (38.3573, -121.9086), "95626": (38.7338, -121.489), "95627": (38.7345, -122.0253),
    "95628": (38.6521, -121.2544), "95629": (38.5128, -120.6875), "95630": (38.6628, -121.1401),
    "95632": (38.2745, -121.2592), "95633": (38.8492, -120.8224), "95634": (38.9006, -120.7068),
    "95635": (38.9107, -120.9086), "95637": (38.853, -122.2729), "95638": (38.3427, -121.0701),
    "95639": (38.4085, -121.4954), "95640": (38.3415, -120.9401), "95641": (38.1556, -121.6048),
    "95645": (38.894, -121.7968), "95648": (38.9228, -121.312), "95650": (38.8092, -121.1714),
    "95651": (38.8209, -120.9352), "95652": (38.6626, -121.4008), "95653": (38.6971, -121.9784),
    "95655": (38.5492, -121.2786), "95658": (38.8812, -121.169), "95659": (38.8587, -121.5899),
    "95660": (38.6789, -121.3805), "95661": (38.7413, -121.2493), "95662": (38.6892, -121.2184),
    "95663": (38.8574, -121.1825), "95664": (38.8013, -121.0379), "95667": (38.7334, -120.7913),
    "95668": (38.8276, -121.4952), "95669": (38.4895, -120.8916), "95670": (38.6043, -121.2803),
    "95671": (38.6945, -121.1538), "95672": (38.7223, -120.993), "95673": (38.6905, -121.4657),
    "95674": (38.9539, -121.4819), "95675": (38.5455, -120.7429), "95676": (38.8743, -121.7138),
    "95677": (38.7929, -121.232), "95678": (38.7654, -121.2885), "95680": (38.2414, -121.5783),
    "95681": (38.9981, -121.3542), "95682": (38.599, -120.9631), "95683": (38.512, -121.0963),
    "95685": (38.434, -120.762), "95686": (38.1578, -121.5202), "95687": (38.3331, -121.9202),
    "95688": (38.4213, -122.0301), "95690": (38.2034, -121.6262), "95691": (38.628, -121.5933),
    "95692": (39.0431, -121.4098), "95693": (38.3985, -121.2195), "95694": (38.5717, -122.0642),
    "95695": (38.694, -121.8529), "95697": (38.7329, -121.8072), "95698": (38.8164, -121.9108),
    "95699": (38.4368, -120.8576), "95703": (38.989, -120.9878), "95709": (38.749, -120.6909),
    "95713": (39.0813, -120.9324), "95722": (39.0098, -121.0347), "95736": (39.0391, -120.9775),
    "95742": (38.5622, -121.2047), "95746": (38.7469, -121.1664), "95747": (38.7784, -121.3688),
    "95757": (38.3317, -121.437), "95758": (38.4297, -121.4452), "95762": (38.6837, -121.0647),
    "95765": (38.8197, -121.2783), "95776": (38.6987, -121.6995), "95811": (38.5938, -121.4822),
    "95814": (38.5806, -121.4956), "95815": (38.6056, -121.4455), "95816": (38.5754, -121.4648),
    "95817": (38.5506, -121.457), "95818": (38.5545, -121.4971), "95819": (38.5669, -121.4376),
    "95820": (38.5349, -121.4442), "95821": (38.6257, -121.3849), "95822": (38.5128, -121.4942),
    "95823": (38.4747, -121.4426), "95824": (38.5174, -121.4414), "95825": (38.5919, -121.4085),
    "95826": (38.5438, -121.3783), "95827": (38.5484, -121.3275), "95828": (38.4894, -121.396),
    "95829": (38.4895, -121.3235), "95830": (38.4905, -121.2842), "95831": (38.4958, -121.5303),
    "95832": (38.4474, -121.4961), "95833": (38.619, -121.5176), "95834": (38.6352, -121.5186),
    "95835": (38.6685, -121.5256), "95837": (38.6938, -121.5989), "95838": (38.6457, -121.4453),
    "95841": (38.6603, -121.3482), "95842": (38.687, -121.3489), "95843": (38.7153, -121.3637),
    "95864": (38.584, -121.3758), "95901": (39.2239, -121.4941), "95903": (39.1213, -121.3711),
    "95912": (39.0004, -122.0742), "95918": (39.3012, -121.3379), "95937": (38.8841, -121.9985),
    "95945": (39.1933, -120.9801), "95946": (39.2092, -121.2138), "95949": (39.1027, -121.1339),
    "95950": (39.0434, -121.9181), "95953": (39.2597, -121.7763), "95957": (39.0523, -121.8252),
    "95961": (39.0413, -121.5628), "95962": (39.3234, -121.2725), "95975": (39.2243, -121.1537),
    "95977": (39.1766, -121.2917), "95982": (39.1732, -121.8057), "95991": (39.0155, -121.6112),
    "95993": (39.0825, -121.6801),
}


# A zip centroid only exists for a zip that delivers to street addresses. CDFA's
# registry is a *mailing* list, and 1,409 of its 5,827 rows are PO Boxes -- whose
# zips (95851, say) are PO-Box-only and have no ZCTA at all, so a zip-only lookup
# silently drops the majority of that source. The city is still known on those rows,
# so it stands in: same 2024 Census gazetteer, the CA places file, clipped to the
# 133 places within 45 miles, name upper-cased with the "city"/"town"/"CDP" suffix
# stripped so "Sacramento city" matches a registry's "SACRAMENTO".
#
# It is a coarser answer than a zip and much coarser than real coordinates -- a
# city-centroid distance says which town, not which end of it -- so it is a
# fallback, never a preference, and `centroid_source` reports which one was used.
CITY_CENTROID: dict[str, tuple[float, float]] = {
    "ACAMPO": (38.1735, -121.2799), "ALLENDALE": (38.443, -121.9833),
    "ALTA SIERRA": (39.1262, -121.0491), "AMADOR CITY": (38.419, -120.8233),
    "ANTELOPE": (38.7153, -121.361), "ARBUCKLE": (39.0142, -122.061),
    "ARDEN-ARCADE": (38.6006, -121.3846), "AUBURN": (38.8951, -121.0767),
    "AUBURN LAKE TRAILS": (38.8862, -120.9797), "BEALE AFB": (39.1081, -121.3512),
    "BROOKS": (38.7369, -122.1468), "BUENA VISTA": (38.2975, -120.9174),
    "CAMANCHE NORTH SHORE": (38.2442, -120.9539), "CAMANCHE VILLAGE": (38.2665, -120.9866),
    "CAMERON PARK": (38.674, -120.9883), "CAMINO": (38.7423, -120.6808),
    "CARMICHAEL": (38.6308, -121.3256), "CITRUS HEIGHTS": (38.6948, -121.288),
    "CLARKSBURG": (38.4193, -121.5416), "CLAY": (38.314, -121.1596),
    "COLD SPRINGS": (38.7464, -120.8738), "COLFAX": (39.0938, -120.9532),
    "COLLEGE CITY": (39.0061, -122.0051), "COLLIERVILLE": (38.2128, -121.265),
    "COLOMA": (38.8026, -120.8946), "COURTLAND": (38.3329, -121.557),
    "DAVIS": (38.5561, -121.7378), "DIAMOND SPRINGS": (38.692, -120.8387),
    "DIXON": (38.4475, -121.8242), "DOGTOWN": (38.2087, -121.1548),
    "DRYTOWN": (38.4415, -120.8605), "DUNNIGAN": (38.8926, -121.9742),
    "EAST NICOLAUS": (38.91, -121.5443), "EL DORADO HILLS": (38.6761, -121.0477),
    "EL MACERO": (38.544, -121.6852), "ELK GROVE": (38.4146, -121.385),
    "ELMIRA": (38.3523, -121.9077), "ELVERTA": (38.7185, -121.4455),
    "ESPARTO": (38.6934, -122.024), "FAIR OAKS": (38.6502, -121.251),
    "FAIRFIELD": (38.2621, -122.0324), "FIDDLETOWN": (38.5069, -120.7601),
    "FLORIN": (38.4832, -121.4043), "FOLSOM": (38.6666, -121.1416),
    "FOOTHILL FARMS": (38.6867, -121.3475), "FORESTHILL": (39.0053, -120.8314),
    "FRANKLIN": (38.3675, -121.4616), "FREEPORT": (38.4629, -121.5021),
    "FRUITRIDGE POCKET": (38.5326, -121.4558), "GALT": (38.2685, -121.299),
    "GEORGETOWN": (38.9114, -120.8346), "GOLD RIVER": (38.6269, -121.2492),
    "GRANITE BAY": (38.7604, -121.1682), "GRASS VALLEY": (39.2203, -121.0527),
    "GRIMES": (39.0742, -121.8988), "GUINDA": (38.8274, -122.1984),
    "HARTLEY": (38.4204, -121.9508), "HERALD": (38.2884, -121.2311),
    "HONCUT": (39.3326, -121.5388), "HOOD": (38.3693, -121.5155),
    "IONE": (38.3629, -120.9477), "ISLETON": (38.1613, -121.605),
    "JACKSON": (38.3485, -120.7728), "KNIGHTS LANDING": (38.798, -121.7174),
    "LA RIVIERA": (38.5685, -121.355), "LAKE OF THE PINES": (39.0387, -121.0613),
    "LAKE WILDWOOD": (39.2332, -121.1977), "LEMON HILL": (38.5172, -121.4573),
    "LINCOLN": (38.8745, -121.2929), "LINDA": (39.1241, -121.5422),
    "LIVE OAK": (39.2788, -121.6624), "LOCKEFORD": (38.1491, -121.1543),
    "LODI": (38.1216, -121.2908), "LOMA RICA": (39.3203, -121.4038),
    "LOOMIS": (38.8094, -121.1954), "MADISON": (38.675, -121.9703),
    "MARTELL": (38.3673, -120.8072), "MARYSVILLE": (39.1515, -121.5834),
    "MATHER": (38.5484, -121.2838), "MCCLELLAN PARK": (38.6629, -121.4017),
    "MEADOW VISTA": (39.0017, -121.0376), "MERIDIAN": (39.1404, -121.9079),
    "MONUMENT HILLS": (38.6641, -121.8754), "MOSKOWITE CORNER": (38.4439, -122.192),
    "NEWCASTLE": (38.8667, -121.132), "NICOLAUS": (38.8982, -121.5728),
    "NORTH AUBURN": (38.9307, -121.0811), "NORTH HIGHLANDS": (38.6713, -121.3721),
    "OLIVEHURST": (39.0795, -121.5567), "ORANGEVALE": (38.6894, -121.2231),
    "PARKWAY": (38.4993, -121.452), "PENN VALLEY": (39.1953, -121.1942),
    "PENRYN": (38.8482, -121.1698), "PLACERVILLE": (38.7311, -120.7977),
    "PLUMAS LAKE": (38.9791, -121.5581), "PLYMOUTH": (38.4717, -120.8572),
    "RANCHO CORDOVA": (38.5771, -121.2362), "RANCHO MURIETA": (38.5007, -121.074),
    "RIO LINDA": (38.6875, -121.4417), "RIO OSO": (38.9518, -121.531),
    "RIO VISTA": (38.1767, -121.7034), "RIVER PINES": (38.5455, -120.7429),
    "ROBBINS": (38.8669, -121.7071), "ROCKLIN": (38.8069, -121.2497),
    "ROSEMONT": (38.5477, -121.3554), "ROSEVILLE": (38.7703, -121.3196),
    "ROUGH AND READY": (39.234, -121.138), "RUMSEY": (38.8933, -122.2436),
    "SACRAMENTO": (38.5677, -121.4682), "SHERIDAN": (38.9731, -121.3509),
    "SHINGLE SPRINGS": (38.6659, -120.9362), "SMARTSVILLE": (39.2053, -121.2929),
    "SUISUN CITY": (38.2483, -122.0101), "SUTTER": (39.1556, -121.7493),
    "SUTTER CREEK": (38.3924, -120.799), "TANCRED": (38.7619, -122.1547),
    "TERMINOUS": (38.1153, -121.4896), "THORNTON": (38.2297, -121.4261),
    "TROWBRIDGE": (38.9264, -121.5148), "UNIVERSITY OF CALIFORNIA-DAVIS": (38.5377, -121.7579),
    "VACAVILLE": (38.3586, -121.9686), "VICTOR": (38.1385, -121.1988),
    "VINEYARD": (38.474, -121.324), "WALLACE": (38.1998, -120.9642),
    "WALNUT GROVE": (38.2506, -121.535), "WEST SACRAMENTO": (38.5544, -121.5487),
    "WHEATLAND": (39.0312, -121.39), "WILTON": (38.413, -121.2127),
    "WINTERS": (38.5333, -121.9755), "WOODBRIDGE": (38.1721, -121.3089),
    "WOODLAND": (38.6712, -121.75), "YOLO": (38.7405, -121.8093),
    "YUBA CITY": (39.1298, -121.6415),
}


def centroid_for_zip(zip5: str) -> tuple[float, float] | None:
    return ZIP_CENTROID.get(zip5.strip()[:5])


def centroid_for_city(city: str) -> tuple[float, float] | None:
    return CITY_CENTROID.get((city or "").strip().upper())


def locate(zip5: str, city: str = "") -> tuple[tuple[float, float] | None, str]:
    """Best available centroid for a row with no coordinates, plus how it was found
    ("zip", "city" or "none") so a caller can label the distance honestly."""
    hit = centroid_for_zip(zip5)
    if hit:
        return hit, "zip"
    hit = centroid_for_city(city)
    if hit:
        return hit, "city"
    return None, "none"


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
