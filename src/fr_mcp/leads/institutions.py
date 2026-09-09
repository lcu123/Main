"""Institutional, residential and medical facilities -- the rows a rep should see
as a different kind of account before dialling.

A nursing home, a memory-care wing, a hospital kitchen and an apartment complex
are all pest accounts, but none of them sells like an independent restaurant:
the decision maker is a facilities director or a corporate procurement office,
the contract is usually annual, and the compliance stakes are higher. Both call
lists therefore mark them rather than mixing them in silently.

**What the inspection feeds actually contain, measured over a full 365-day pull
of all three counties (2026-09-09):** three rows carrying
`LICENSED HEALTH CARE FACILITY` and one hospital filed under a food-distribution
permit. That is the whole population, and it is not an accident of the lookback:

- These are **food-facility** inspection feeds. A care home appears only when it
  runs a licensed kitchen, and most contract catering out.
- **An apartment complex is not a food facility at all**, so no health-inspection
  feed will ever list one. Reaching those needs a different source entirely (a
  rental registry or the county assessor), not a longer pull.
- A hospital appears as its cafeteria's permit, under the cafeteria's name, so
  the permit type says "satellite food distribution" rather than "hospital".

So this module marks what is genuinely there. It is deliberately not a way to
*find* care facilities -- see the note above for why that needs another source.

False positives are the main risk, because these words are common in street and
brand names. Two real ones this was built against: `ELDER CREEK MARKET` (a market
on Elder Creek Road) and `Lira Clinical Store of Sacramento` (a skincare brand).
Both are the reason the patterns below are word-bounded and paired, rather than a
bare keyword list.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

CLASS_CARE = "Nursing / memory care / assisted living"
CLASS_MEDICAL = "Hospital / medical facility"
CLASS_RESIDENTIAL = "Apartment / residential complex"
CLASS_SCHOOL = "School / campus dining"

# Ordered most specific first; the first match wins, so a "hospital rehabilitation
# center" reads as medical rather than as care.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        CLASS_MEDICAL,
        re.compile(
            r"\b(HOSPITAL|MEDICAL CENTER|MEDICAL CENTRE|MEDICAL GROUP|MEDICAL PLAZA|"
            r"HEALTH CENTER|SURGERY CENTER|SURGICAL CENTER|DIALYSIS|INFUSION CENTER|"
            r"CLINICS?|URGENT CARE|EMERGENCY DEPARTMENT|CANCER CENTER)\b",
            re.I,
        ),
    ),
    (
        CLASS_CARE,
        re.compile(
            # "CARE" and "LIVING" are only meaningful next to another word -- a bare
            # "care" matches "Urgent Care Deli" and half the catering trade.
            r"\b(SKILLED NURSING|NURSING (HOME|CENTER|FACILITY)|CONVALESCENT|"
            r"MEMORY CARE|ASSISTED LIVING|SENIOR LIVING|INDEPENDENT LIVING|"
            r"RETIREMENT (HOME|CENTER|COMMUNITY|VILLAGE|RESIDENCE)|POST[- ]?ACUTE|"
            r"HOSPICE|CARE (HOME|CENTER|CENTRE|FACILITY|COMMUNITY)|"
            r"HEALTH ?CARE (CENTER|CENTRE|FACILITY)|REHABILITATION (CENTER|CENTRE|HOSPITAL)|"
            r"ELDER ?CARE|SENIOR (CENTER|CENTRE|COMMUNITY)|BOARD AND CARE)\b",
            re.I,
        ),
    ),
    (
        CLASS_RESIDENTIAL,
        re.compile(
            r"\b(APARTMENTS?|APTS?|CONDOMINIUMS?|CONDOS?|MOBILE HOME PARK|"
            r"RESIDENTIAL (COMMUNITY|COMPLEX)|HOUSING (AUTHORITY|COMPLEX))\b",
            re.I,
        ),
    ),
    (
        CLASS_SCHOOL,
        re.compile(
            r"\b(ELEMENTARY|MIDDLE SCHOOL|HIGH SCHOOL|SCHOOL DISTRICT|UNIFIED|"
            r"UNIVERSITY|COLLEGE|CAMPUS|ACADEMY)\b",
            re.I,
        ),
    ),
)

# Permit/licence descriptions that say it outright, whatever the name reads like.
# `LICENSED HEALTH CARE FACILITY` is Sacramento's own string, verified live.
_PERMIT_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CLASS_CARE, re.compile(r"LICENSED HEALTH ?CARE FACILITY|RESIDENTIAL CARE", re.I)),
    (CLASS_SCHOOL, re.compile(r"\bSCHOOL\b", re.I)),
)

# Names that trip the patterns above but are nothing of the kind. Each was a real
# false positive on live data, not a hypothetical.
_NOT_INSTITUTIONAL_RE = re.compile(
    # "Lira Clinical Store" -- a skincare brand. "Clinical" is not "clinic", but a
    # retail or market suffix settles it either way.
    r"\bCLINICAL\b"
    # "ELDER CREEK MARKET" -- Elder Creek Road, Sacramento.
    r"|\bELDER CREEK\b"
    # A shop that merely sells to the trade.
    r"|\b(MEDICAL|DENTAL) SUPPL(Y|IES)\b",
    re.I,
)


def classify(name: str, permits: Sequence[str] | Iterable[str] | None = None) -> str | None:
    """The institutional class of a facility, or None for an ordinary business.

    The permit is checked first where it exists, because a county that bothers to
    file something as a licensed health-care facility is more reliable than any
    reading of its trading name."""
    text = (name or "").strip()
    for label, pattern in _PERMIT_CLASSES:
        for permit in permits or ():
            if pattern.search(permit or ""):
                return label
    if not text or _NOT_INSTITUTIONAL_RE.search(text):
        return None
    for label, pattern in _PATTERNS:
        if pattern.search(text):
            return label
    return None


def is_institutional(name: str, permits: Sequence[str] | Iterable[str] | None = None) -> bool:
    return classify(name, permits) is not None
