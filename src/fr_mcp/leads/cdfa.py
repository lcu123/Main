"""CDFA Market Enforcement licence registry: the phone-bearing backbone of the
Food Facilities list.

The California Department of Food and Agriculture licenses everyone who buys,
handles or processes California farm products for resale -- processors, packers,
dealers, brokers, commission merchants, cash buyers. That is close enough to this
division's ICP to use as a discovery source rather than only an enrichment one,
and it is the only source in the whole pipeline with a phone on **every** row
(5,826 of 5,827 statewide, verified live 2026-09-09).

How it works, and why it is more awkward than a JSON feed:

- The public licensee list is an ASP.NET WebForms page. A GET returns the form;
  the POST that generates the list must echo the `__VIEWSTATE` /
  `__EVENTVALIDATION` pair from *that same* GET, so `fetch_licensees` does both in
  one function and never caches a token. A stale or absent pair comes back as
  HTTP 500 "Invalid postback or callback argument", and so does an empty
  `ddlCommodityType` -- WebForms event validation rejects a value that was not one
  of the rendered options, so the "all commodities" option has to be sent by its
  real value (`0`), not as a blank.
- The POST response is a **CSV body served from an .aspx URL** -- no attachment
  headers, no HTML table. Don't go looking for a grid to scrape.

What the registry does not give you, and what to do about it:

- **No coordinates and no site address** -- only a mailing address, 1,409 of them
  PO Boxes statewide. Distance therefore comes from `regions.locate`, which prefers
  the zip centroid and falls back to the city's: a PO-Box-only zip like 95851 has no
  Census ZCTA at all, so a zip-only lookup would drop most of this source's rows
  without saying so. Either way the number is the distance to where the *mail* goes,
  not to a plant -- `Licensee.address_is_mailbox` marks those so the sheet can say
  so rather than implying a site was located.
- **No licence type on the output rows.** The category checkboxes filter the
  query but the CSV has no column saying which one matched, so if you need to
  know that, query one category at a time.
- Statewide only: there is no county or radius parameter, so the whole 5.8k-row
  list comes back on every call and is filtered locally. That is one request, not
  a paged crawl, which is why it is cheap despite the size.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Iterable

import httpx

from . import regions
from .arcgis import USER_AGENT

LIST_URL = "https://apps4.cdfa.ca.gov/MarketEnforcementLicenseRenewalPublic/licenseelist.aspx"

# The form's own category checkboxes. All six are licence classes over California
# farm products; "Processor" and "Processor Cash Buyer" are the manufacturing end,
# the rest are handlers and distributors -- both halves are in scope for pest work.
CATEGORIES = (
    "cbBroker",
    "cbCashBuyer",
    "cbCommissionMerchant",
    "cbDealer",
    "cbProcessor",
    "cbProcessorCashBuyer",
)
ALL_COMMODITIES = "0"

_HIDDEN_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")
_PO_BOX_RE = re.compile(r"\bP\.?\s*O\.?\s*BOX\b|\bPOST\s+OFFICE\s+BOX\b", re.I)
_WS_RE = re.compile(r"\s+")


class CdfaError(RuntimeError):
    """The registry did not return a licence list this run."""


@dataclass(frozen=True)
class Licensee:
    license_number: str
    name: str
    address: str  # mailing address, not the site
    city: str
    state: str
    zip5: str
    phone: str  # 10 digits, no punctuation; "" only for the one row that has none
    expires: str

    @property
    def address_is_mailbox(self) -> bool:
        return bool(_PO_BOX_RE.search(self.address))

    @property
    def key(self) -> str:
        """`CDFA:<licence number>` -- stable across renewals (the number is the
        licence's identity, the expiry date is not), which is what makes a rerun
        idempotent without any local state."""
        return f"CDFA:{self.license_number}"


def _clean(value: str | None) -> str:
    return _WS_RE.sub(" ", (value or "").replace("\xa0", " ")).strip()


def normalize_phone(raw: str | None) -> str:
    """The registry writes phones inconsistently -- "(530) 308-1234" on most rows,
    a bare "9165654324" on others. Both have to reduce to the same 10 digits, since
    that is what `fr_push.find_existing_customer`'s exact-match dedupe compares."""
    digits = re.sub(r"\D+", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _hidden_fields(html: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for name in _HIDDEN_FIELDS:
        m = re.search(rf'name="{name}"[^>]*?value="([^"]*)"', html) or re.search(
            rf'value="([^"]*)"[^>]*?name="{name}"', html
        )
        if m:
            found[name] = m.group(1)
    if "__VIEWSTATE" not in found or "__EVENTVALIDATION" not in found:
        raise CdfaError(
            "CDFA licensee form carried no __VIEWSTATE/__EVENTVALIDATION -- the page "
            "shape changed, or a block page was served instead of the form."
        )
    return found


def parse_licensees(csv_text: str) -> list[Licensee]:
    """The POST body is a CSV whose header is
    `LicenseNum,Name,MailingAddress,MailingCity,MailingState,MailingZIP,Phone,ExpirationDate`.
    An HTML body means the POST was rejected (a 500 page still arrives as text), so
    a missing header is an error rather than an empty list -- silently returning
    zero rows is how a source disappears from a run without anyone noticing."""
    text = csv_text.lstrip("﻿")
    if not text.lstrip().lower().startswith("licensenum"):
        head = _clean(text[:160])
        raise CdfaError(f"CDFA returned something other than the licence CSV: {head!r}")
    out: list[Licensee] = []
    for row in csv.DictReader(io.StringIO(text)):
        number = _clean(row.get("LicenseNum"))
        name = _clean(row.get("Name"))
        if not number or not name:
            continue
        out.append(
            Licensee(
                license_number=number,
                name=name,
                address=_clean(row.get("MailingAddress")),
                city=_clean(row.get("MailingCity")).title(),
                state=_clean(row.get("MailingState")).upper(),
                zip5=_clean(row.get("MailingZIP"))[:5],
                phone=normalize_phone(row.get("Phone")),
                expires=_clean(row.get("ExpirationDate")),
            )
        )
    return out


async def fetch_licensees(
    client: httpx.AsyncClient, *, categories: Iterable[str] = CATEGORIES
) -> list[Licensee]:
    """GET the form, then POST it back with its own tokens. Both halves in one
    function on purpose: the `__VIEWSTATE`/`__EVENTVALIDATION` pair is only valid
    for the render it came from, so there is nothing here worth caching."""
    headers = {"User-Agent": USER_AGENT}
    try:
        page = await client.get(LIST_URL, headers=headers, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise CdfaError(f"CDFA licensee form unreachable: {exc}") from exc
    if page.status_code != 200:
        raise CdfaError(f"CDFA licensee form returned HTTP {page.status_code}.")

    form = _hidden_fields(page.text)
    form["ddlCommodityType"] = ALL_COMMODITIES
    form["btnGenerateList"] = "Generate List"
    for box in categories:
        form[box] = "on"

    try:
        resp = await client.post(
            LIST_URL,
            data=form,
            headers={**headers, "Referer": LIST_URL},
            follow_redirects=True,
        )
    except httpx.HTTPError as exc:
        raise CdfaError(f"CDFA licence list POST failed: {exc}") from exc
    if resp.status_code != 200:
        raise CdfaError(
            f"CDFA licence list POST returned HTTP {resp.status_code} "
            "(a 500 here is normally a rejected __VIEWSTATE or an unrendered dropdown value)."
        )
    return parse_licensees(resp.text)


def locate(licensee: Licensee) -> tuple[float | None, str]:
    """(miles from the office, how the centroid was found). The source carries no
    coordinates at all, so this is always an approximation -- "zip" is roughly the
    right neighbourhood, "city" only the right town."""
    centroid, source = regions.locate(licensee.zip5, licensee.city)
    if centroid is None:
        return None, source
    return regions.haversine_miles(*centroid), source


def distance_miles(licensee: Licensee) -> float | None:
    return locate(licensee)[0]


def within(licensees: Iterable[Licensee], max_miles: float) -> list[Licensee]:
    """Statewide in, local out. A row we cannot place at all is dropped rather than
    kept as "distance unknown": the gazetteer clip covers every zip and city within
    45 miles of the office, so a miss means the row is elsewhere in California."""
    keep = []
    for lic in licensees:
        d = distance_miles(lic)
        if d is not None and d <= max_miles:
            keep.append(lic)
    return keep
