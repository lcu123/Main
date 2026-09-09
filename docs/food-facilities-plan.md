# Plan: "Food Facilities" call-list tab for Zest Commercial Leads

Status: **plan only, nothing built yet.** Revised 2026-09-08 (v3) after owner input: 28-mile radius from the office,
category framing, bakeries/breweries/wineries confirmed in scope, and the question "is there a permit or portal that
beats Google?" (answered in section 3). No code has been written and the Google Sheet has not been touched.

**Decided by the owner, 2026-09-08:** coverage is 28 straight-line miles from 6948 West 2nd St, Rio Linda; target
categories are food processing, food manufacturing and food storage facilities; **bakeries, breweries and wineries are
in scope.** Open items are listed in section 13.

Target sheet: *Zest Commercial Leads*
(`https://docs.google.com/spreadsheets/d/1u6e6psiW581rGr92r86aSJQW2XzPZfzsnhpJlolB4F8`).

## 1. Goal

Add one new tab that a salesperson works top-to-bottom to sell pest-control inspections to **food processing
facilities, food manufacturers, food storage facilities and other wholesale food facilities, plus bakeries, breweries
and wineries** within 28 miles of Zest's office. Restaurants and retail markets are out; the county health-inspection pipeline in the `Leads` tab
already covers them. Every row needs business name, phone, type of business and city, plus Notes and Follow-up
date for the rep.

## 2. What is in the sheet today (read-only look)

| Tab | Shape | What it is |
| --- | --- | --- |
| `Leads` | 205 rows x 41 cols | County health-inspection leads (Sacramento EMD, Placer, Yolo), keyed by permit ID. Rep-owned columns already exist: status, notes, rep, last touch date, next step date, touch count, inspection date, outcome, FieldRoutes customer ID. |
| `Signals` | 63 rows | One row per inspection report (vermin quote, result, report URL). |
| `Runs` | 5 rows | Run log of the existing pipeline (rows added / flagged / skipped dnc / skipped cap / errors). |
| `DNC` | header only | Do-not-call list: key, phone, email, reason, added at. |

No formulas, validation, conditional formatting or protected ranges anywhere. The existing pipeline writes by column
header name, caps itself at 40 new rows per run and respects the DNC tab. The new tab follows the same conventions.

**Why food processors are missing from Leads.** County inspection portals cover *retail* food only. Wholesale
processors, manufacturers and food warehouses are permitted by the state (CDPH Food and Drug Branch), meat and
poultry by USDA FSIS or CDFA, dairies by CDFA. That is a different set of permits, covered next.

## 3. Is there a permit or portal? Yes, several, but no single one (owner's question)

Checked 2026-09-08. Short answer: the one permit that covers exactly this population is **not published online**, but
a handful of free permit and registry sources exist and should be pulled *before* Google. Google then fills the gaps
and supplies phones for the sources that lack them.

| Source | What it is | Gives | Phone? | Covers | How to get it |
| --- | --- | --- | --- | --- | --- |
| **CDPH Food and Drug Branch, Processed Food Registration (PFR)** | The state permit required of anyone who manufactures, repacks, labels or **warehouses** processed food (Health & Safety Code 110460). Exactly our population. | Firm name, address, registration type | Unknown (probably not) | Statewide | Not searchable online. Obtain by a **California Public Records Act request** to CDPH for registrants in Sacramento, Placer, Yolo (and edge counties). Free; the statute gives the agency 10 days to respond, extendable 14. Contact: FDBfood@cdph.ca.gov, (916) 650-6500. |
| **City of Sacramento Business Operation Tax (BOT) open data** | Every business paying the city's operations tax (about 59,000 accounts). | `Business_Name`, `Business_Description`, `Primary_Phone_number`, street address, city, zip, `Current_License_Status`, start/close dates | **Yes** | City of Sacramento only (not West Sacramento, Elk Grove, Roseville, or unincorporated county) | Public ArcGIS REST endpoint, no key, updated daily. Filter `Business_Description` for food manufacturing / processing / warehouse / wholesale terms. |
| **USDA FSIS Meat, Poultry and Egg Product Inspection Directory** | Every federally inspected meat, poultry and egg-product plant. | Establishment name, number, address, activities (slaughter/processing), size | **Yes** | National; filter to CA and our radius | Public CSV, updated weekly, from fsis.usda.gov (also on data.gov). |
| **EPA ECHO facility search** | Facilities with any federal/state environmental ID (air, wastewater, hazmat). Most real plants have one. | Name, address, NAICS code, lat/lng | No | National; filter by county | Search NAICS `311` (food mfg), `312` (beverage), `4244` (grocery wholesalers), `49312` (refrigerated warehousing) for Sacramento, Placer, Yolo, Sutter, Solano, El Dorado; export CSV. |
| **CalEPA Regulated Site Portal / CERS** | Every facility filing a Hazardous Materials Business Plan or under CalARP. Ammonia refrigeration puts cold-storage and many food plants here. | Name, address, programs | No | Statewide | Map search at siteportal.calepa.ca.gov; bulk downloads via CalEPA Open Data. |
| **CDFA Milk and Dairy Food Safety Branch** | Licensed milk-product plants. | Plant name, city (IMS list) | No | Statewide; only a few plants in our area | Lists on cdfa.ca.gov; regional office can supply the licensed-plant list. |
| **Data Axle Reference Solutions** (free with a Sacramento Public Library card) | Commercial business database. | Name, address, **phone**, NAICS, employees, sales, contact name; radius search from an address | **Yes** | Everything | Log in through saclibrary.org, search NAICS 311/312/4244/4931 within 28 miles of the office, export. The strongest single list tool; check the library's terms for sales-prospecting use. |
| County health departments (Sacramento EMD, Placer, Yolo) | Retail food permits. | Already in `Leads` | | | Nothing more to get here for processors. |
| Other cities' business licenses (West Sacramento, Elk Grove, Roseville, Woodland, Davis, Folsom, Citrus Heights, Rancho Cordova) | Per-city lookups exist, but no bulk open data found. | | | | Skip unless a specific city matters; Google covers them. |

**Recommendation: layer them.**

1. Pull the registries that carry phones (City of Sacramento BOT, FSIS) and the ones that do not (ECHO, CalEPA, CDFA).
   Merge on normalized name + address. This is the "permit-grade" core.
2. File the CDPH PRA request in parallel (I draft it; the owner sends it from a Zest address). When the list arrives,
   merge it in the same way.
3. Run the Google Places keyword sweep (section 4) to catch facilities no registry has, especially outside the City
   of Sacramento, and to add phone, website, Maps link and open/closed status to every row that lacks them.
4. If the owner has a library card, a Data Axle export becomes a fourth input and a completeness check.

Every row records its `Source(s)` so the rep and the owner can see where a lead came from.

## 4. Google Places API (New): gap-filler and enrichment

### 4.1 Why Text Search, and why keywords

Verified against Google's place-type table: **no place type exists for food processing, factory, warehouse,
distribution center or cold storage.** Nearest: `manufacturer`, `supplier`, `wholesaler`, `farm`, `bakery`,
`butcher_shop`. So Nearby Search (type-driven) cannot find these; Text Search (keyword-driven) can. Text Search
facts that shape the design: 20 results per page, **60 per query** max (3 pages), `locationRestriction` accepts a
**rectangle only**, one optional `includedType` with `strictTypeFiltering`.

### 4.2 Tiling

Each keyword runs first against the whole coverage rectangle. If it returns 60 (saturated), the rectangle is split
into four quadrants and re-run, recursively, until no quadrant saturates. Results de-duplicate by place ID.

### 4.3 Search terms (draft of about 50, tuned after the first dry run)

Processing/manufacturing: food processing plant, food processing facility, food manufacturer, food manufacturing,
food packaging company, co-packer, commercial bakery, wholesale bakery, tortilla factory, meat processing, meat
packing, slaughterhouse, poultry processing, seafood processor, dairy processing plant, creamery, cheese manufacturer,
egg processing, produce packing, fruit packing house, nut processing, almond processor, rice mill, flour mill, feed
mill, cannery, frozen food manufacturer, snack food manufacturer, candy manufacturer, spice manufacturer, sauce
manufacturer, pet food manufacturer, ice manufacturer.

Beverage: beverage manufacturer, bottling plant, juice processing, coffee roaster wholesale, brewery, brewery production
facility, winery, winery production facility, distillery.

Bakery: bakery, commercial bakery, wholesale bakery, tortilla factory (bakeries are in scope per the owner, so the bare
term is searched too, not just the wholesale variants). Type-driven pass: `includedType` = `bakery`.

Storage/distribution: food storage facility, cold storage warehouse, refrigerated warehouse, food distribution
center, food distributor, wholesale grocer, produce distributor, meat distributor, seafood distributor, beverage
distributor, food warehouse, food bank warehouse.

Kitchens: commissary kitchen, central kitchen, catering commissary, school district central kitchen.

Plus type-driven passes: `includedType` = `manufacturer`, `wholesaler`, `supplier` with query "food", strict filtering.

### 4.4 Enrichment for registry rows

For every registry row missing a phone or website, one Text Search by "name + address" (Pro fields) followed by a
Place Details call (Enterprise fields: `nationalPhoneNumber`, `websiteUri`) fills the gaps and attaches the place ID.

## 5. Coverage area: 28 miles from the office

Center: **6948 West 2nd St, Rio Linda, CA 95673** (geocoded 38.6941, -121.4660). A candidate is kept only if its
straight-line distance to that point is **28.0 miles or less**; the distance goes in its own column. Google searches
use the 56 x 56 mile bounding rectangle, then rows are trimmed by the distance rule so the circle, not the square,
is what ends up in the sheet.

In: Sacramento, West Sacramento, Woodland, Davis, Elk Grove, Rancho Cordova, Folsom, El Dorado Hills, Citrus Heights,
Roseville, Rocklin, Lincoln, Wheatland, and Auburn at the edge. Out: Galt, Dixon, Lodi/Stockton, Yuba City/Marysville,
Placerville.

## 6. The new tab

Name: **`Food Facilities`**, appended as the last tab. Columns left to right in the order the owner asked for:

| Col | Header | Owner | Notes |
| --- | --- | --- | --- |
| A | Business name | script | Cleaned name |
| B | Phone | script | `(916) 555-1234`; blank if no source has one |
| C | Type of business | script | Our plain-English category (section 7) |
| D | City | script | |
| E | Notes | **rep** | Free text. Never touched by the script |
| F | Follow-up date | **rep** | Date-validated. Never touched by the script |
| G | Status | **rep** | Dropdown: New, No answer, Callback, Interested, Inspection booked, Not interested, Bad number, Do not call |
| H | Rep | **rep** | |
| I | Last call date | **rep** | |
| J | Address | script | |
| K | Zip | script | |
| L | Website | script | |
| M | Google Maps link | script | |
| N | Open/closed | script | Google `businessStatus`, or registry status |
| O | Distance (mi) | script | From the office, straight line |
| P | Region | script | Same labels as `Leads` |
| Q | Source(s) | script | e.g. `City BOT; FSIS; Google` |
| R | Found via | script | Search terms or registry description that surfaced it |
| S | Google category | script | Raw `primaryType` |
| T | Needs review? | script | `yes` when classification is uncertain |
| U | In Leads tab? | script | `yes` on a name+zip or phone match against `Leads` |
| V | FieldRoutes customer ID | **rep** | Same convention as `Leads` |
| W | Key | script | Google place ID when known, else the registry record ID (BOT account number, FSIS establishment number). Do not edit |
| X | First seen | script | |
| Y | Last refreshed | script | |

One-time formatting: frozen bold header, column widths, header filter, status dropdown, date validation on F and I,
warning-only protection on script-owned columns, rows sorted by distance. A "Source: Google" attribution in the header
note where Google data is shown.

Rep-vs-script contract: the script writes only its own columns. On refresh it appends new rows, updates Open/closed
and Last refreshed, and fills blank Phone/Website. It never rewrites a non-blank cell in A-D and never touches E-I or V.

## 7. Filtering and classification

Drop a candidate if its Google types include any restaurant-family type (`restaurant`, `*_restaurant`, `cafe`,
`coffee_shop`, `bar`, `meal_takeaway`, `meal_delivery`, `food_court`, `ice_cream_shop`, `donut_shop`, `juice_shop`,
`dessert_shop`, `sandwich_shop`) or retail type (`grocery_store`, `supermarket`, `convenience_store`, `liquor_store`,
`asian_grocery_store`, `market`, `warehouse_store`, `candy_store`, `chocolate_shop`), or its name hits a blacklist
(Restaurant, Cafe, Grill, Taqueria, Pizza, Sushi, Deli, Bistro, ...). Registry rows are filtered on their own
description/NAICS instead (keep 311, 312, 4244, 4931; drop 722 food service and 445 food retail).

**Bakeries, breweries and wineries are in scope (owner decision).** Every `bakery`, `butcher_shop`, brewery and winery
hit is kept unless its *primary* type is a restaurant-family type. When a cafe, tasting-room or storefront type is
also present the row is kept and flagged `Needs review? = yes`, so the rep knows it may be retail-facing.

Type of business categories: Meat & poultry processing; Seafood processing; Dairy / creamery; Commercial bakery /
tortilla; Produce packing / nut & rice processing; Snack, candy & confectionery manufacturing; Sauce, spice & prepared
foods manufacturing; Frozen & ready-meal manufacturing; Brewery / winery / distillery; Beverage production (bottling,
juice, coffee roasting); Cold storage / refrigerated warehouse;
Food distribution / wholesale grocer; Commissary / central kitchen; Pet & animal food manufacturing; Food packaging /
co-packer; Other food manufacturing. Assigned by registry NAICS/description first, then name keywords, then the search
term, then Google `primaryType`.

## 8. How the other tabs stay untouched

- A Google Cloud **service account** is shared on this one spreadsheet as Editor; nothing else in Drive.
- Every write targets the `Food Facilities` sheet ID, resolved from the tab title and re-checked before each write.
  No spreadsheet-wide operations: no clears, no reordering, no column inserts elsewhere, no changes to `Leads`,
  `Signals`, `Runs` or `DNC`.
- Tab created with `addSheet` only if absent, appended at the end.
- Rows **upserted by Key** (column W); existing rows updated cell-by-cell in script-owned columns only.
- Before and after every run the script hashes the values of every *other* tab and refuses to report success if any
  changed.
- First run is a **dry run** to CSV; only after sign-off does `--write` run.
- Run log printed and saved locally, not appended to the other pipeline's `Runs` tab.

## 9. Cost, quota, time

Registries: free. Google (verified on Google's pricing page 2026-09-08, first tier):

| SKU | Price / 1,000 | Free per month | Our use |
| --- | --- | --- | --- |
| Text Search Pro | $32.00 | 5,000 | Keyword sweep + name lookups: est. 500-1,200 calls |
| Place Details Enterprise (phone + website) | about $20 (confirm) | 1,000 | One per kept row lacking a phone: est. 300-700 |
| Text Search Enterprise | $35.00 | 1,000 | Not used |

Expected: **$0 to a few dollars** for the first pull, $0 for a monthly refresh. A Google Cloud project with billing
enabled is still required. Expected volume: roughly 400-800 facilities inside 28 miles. Full pull 10-20 minutes;
raw responses cached to disk so re-runs while tuning do not re-spend quota.

## 10. Terms-of-service notes

Google lets **place IDs be stored indefinitely** but restricts storing other Places content beyond its terms; the
monthly refresh from stored IDs and the rep's own notes are the practical mitigation, and Google asks for a "Source:
Google" attribution when its data is shown outside a map. Data Axle via the library has its own terms for commercial
use. Registry data (BOT, FSIS, ECHO, CalEPA, PRA) is public record. Not legal advice.

## 11. Where the code lives and how it runs

- `scripts/food_facilities/` in this repo: `sources/` (one module per registry), `places.py` (sweep + enrichment),
  `classify.py`, `sheet.py` (tab creation, formatting, upsert, other-tab checksum), own `requirements.txt`. **Not**
  added to the MCP server's runtime deps, Docker image or `requirements.lock`.
- Tests against recorded responses and a fake Sheets client; CI needs no keys.
- Env vars in `.env.example`: `GOOGLE_MAPS_API_KEY`, `GOOGLE_SHEETS_SA_JSON`, `LEADS_SHEET_ID`, `FOOD_TAB_NAME`
  (default `Food Facilities`), `OFFICE_LAT=38.6941`, `OFFICE_LNG=-121.4660`, `RADIUS_MILES=28`.
- Commands: `pull --dry-run` (CSV only), `pull --write` (first fill), `refresh` (monthly). Manual to start; Railway
  cron optional later.
- README section + CLAUDE.md pointer.

## 12. Phases

0. **Owner setup (blocks the rest).** Google Cloud project with billing; enable Places API (New) and Sheets API; API
   key restricted to Places; service account + JSON key; share the sheet with the service-account email as Editor;
   keys into `.env`/Railway only. Optional: send the CDPH PRA request I draft; get a Sacramento Public Library card
   for Data Axle. Answer section 13.
1. **Registry pull, dry run.** City BOT + FSIS + ECHO + CalEPA (+ CDFA) merged, filtered to 28 miles, classified,
   CSV out with counts by source/category/city. No Google key needed yet. Owner spot-checks 30 rows.
2. **Google sweep + enrichment.** Keyword sweep, name+address lookups for registry rows, phones/websites/status,
   DNC and Leads cross-check, region labels. Second dry-run CSV.
3. **Tab creation and first write.** Create `Food Facilities`, formatting, dropdowns, write rows, verify other-tab
   checksums, hand to the rep.
4. **Refresh and docs.** `refresh` command, tests, README/CLAUDE.md/.env.example.
5. **Optional.** Merge the CDPH PRA list when it arrives; Data Axle export; FieldRoutes existing-customer flag.

## 13. Decisions needed before code

1. Column order A-Y as in section 6 (Name, Phone, Type, City, Notes, Follow-up date, ...)? Alternative: Notes and
   Follow-up date as columns A-B. One-line change.
2. File the CDPH Public Records Act request for the Processed Food Registration list? (I draft, owner sends.)
3. Does anyone have a Sacramento Public Library card for the Data Axle export?
4. Include institutional kitchens (school-district central kitchens, hospital food service, food bank)? Default if
   unanswered: include, flagged.
5. Keep rows already in `Leads` (flagged) or drop them? Default: keep, flagged.
6. Status dropdown values OK, or match what the rep uses in `Leads`? Default: as listed in section 6.
7. Cadence: one-time pull plus monthly refresh on request, or a Railway cron? Default: manual.
8. Who sets up the Google Cloud project and billing (Phase 0)? This one has no default; nothing Google-side can run
   without it. The registry pull (Phase 1) can start without it.

Already decided: 28-mile radius; bakeries, breweries and wineries included.

**Decided by the owner 2026-09-08 (answers to 1, 4 and 5 above):**

1. **Column order mirrors the `Leads` tab**, not section 6's original A-Y. The reps work both tabs, so
   they get one muscle memory: `followup date`, `business name`, `phone`, `notes` first, then the rest.
   This supersedes section 6's ordering; the column *set* there still stands.
4. **Institutional kitchens are in scope** -- school district central kitchens, hospital food service,
   food bank warehouses -- flagged so a rep can see what they are before dialling.
5. **A facility already present in `Leads` stays on this tab**, flagged in `In Leads tab?`, rather than
   being dropped. A processor that also holds a retail permit is still a processor lead.

Still open: 2 (CDPH records request), 3 (library card for Data Axle), 6 (status dropdown values),
7 (cadence). None of them block the first build; the deferred sources are additive.

**Executing in the existing lead-scraper session, not a fresh one** (owner's choice, 2026-09-08).
That makes `src/fr_mcp/leads/`'s shipped modules the substrate rather than the `scripts/food_facilities/`
greenfield of section 11: `places.py` already carries service-account auth, the address-agreement check
that rejects a wrong-business match, a per-run call ceiling and a 401/403/429 circuit breaker;
`sheet.py` carries batched writes (Sheets allows only 60 write requests per minute per user),
column-addressing by header name, and the tool-owned/rep-owned split; `regions.py` carries the office
coordinates, `haversine_miles` and the zip-to-region table. Each of those encodes a fix that only a
real production run surfaced, so rebuilding them fresh would rediscover the same bugs. Section 11's
"not added to the MCP server's runtime deps" constraint still holds -- the MCP server must not gain
dependencies from this work.

**Phase 0 is already satisfied** as of 2026-09-08, which section 12 could not assume: the Google Cloud
project (`zest-leads`) exists with billing, Places API (New) is enabled, the `leads-writer` service
account exists and the sheet is shared with it as Editor. No `GOOGLE_MAPS_API_KEY` is needed --
Places API (New) accepts the service account's OAuth `cloud-platform` token, verified live, so there
is no second credential to store or restrict.

## 13b. Recon findings (2026-09-08/09) — every source verified against its live endpoint

Before writing any adapter, one investigator per source actually hit its endpoint. All eight have
now reported. **Several findings overturn assumptions in sections 3 and 12 above, which should be
read as superseded where they conflict** — most consequentially, the two sources this section
recommends building first are not the two section 3 leads with.

### City of Sacramento BOT — demoted from "permit-grade core" to enrichment only

Section 3 proposed this as a primary discovery source because it carries phones. It does carry
them (97.4% fill on the 23,762 active rows, verified), and the endpoint is live, free,
unauthenticated, daily-refreshed, 63,644 rows, 12 paged requests for a full active pull. All true.

**But it is a tax registry, not a facility registry, and has no concept of food processing.** All
158 distinct `Business_Description` values were enumerated in a single `groupByFieldsForStatistics`
request: the vocabulary is generic tax classes (`Manufacturing`, `Wholesale Business`,
`Retail Sales - General`). Precision on the closest categories runs ~30-50%, and recall fails on
exactly the accounts this division most wants — **Blue Diamond Growers, the largest almond
processing plant in the world, sitting in downtown Sacramento, is filed as `Retail Sales - General`
with the phone `(000) 000-0000`.** A category-based `where` clause would ship a list that is half
restaurants and missing the marquee accounts. Any use of this source needs a name-based classifier.

Two further traps: the phone frequently tracks `Mail_City` (the owner's number, e.g. a 408 number
on a Sacramento site), not the facility line — the same class of defect the `Leads` phone audit
found; and coverage is City of Sacramento only, roughly one third of the 28-mile circle by
population, with the 1,161 rows in other cities present only because they happen to pay Sacramento
business tax. Also note the layer is an ArcGIS **Table**, so `geometryType` is null — there is no
lat/lng, and `returnGeometry=true` returns nothing. Stored values carry trailing spaces that SQL
comparison forgives but Python `==` does not: `.strip()` after parsing, the same trailing-space bug
class CLAUDE.md already documents for `FOOD PREP ESTAB `.

### USDA FSIS — the cleanest data in the effort, but small (and third in the corrected order)

The best-shaped source in the effort: a single static CSV, no auth, no pagination, no PDF parsing.
(It led the order when only three sources had reported; CDFA and CalEPA later came back with five
times the rows and the same phone coverage, so it settles at third — see the corrected order below.)
**28 establishments inside the 28-mile circle, with a phone on every one and real lat/lng on 100%**
— which makes the radius filter exact rather than zip-centroid-approximated as `regions.py` must do
for Placer and Yolo. Carries an activity vocabulary separating slaughter from processing, and an
FSIS size class. Adapter estimated at ~60 lines.

**One real risk, honestly reported:** `www.fsis.usda.gov` returns HTTP 403 to every request from
this sandbox — an Akamai edge denial against our egress IP, not a proxy problem and not a 404.
Browser User-Agent, full `Sec-Fetch` headers and WebFetch all failed. The investigator obtained the
data from the Internet Archive's byte-for-byte copy instead and said so rather than pretending
otherwise. **Before building: run one live fetch from Railway.** If Railway gets 200 this is
trivial; if Railway also gets 403 the adapter needs a Wayback fallback.

### EPA ECHO — supplementary only, and it carries a silent-corruption trap

110 facilities in area, clean lat/lng, and a native radius search that solves the 28-mile circle in
one parameter. But **no phone anywhere in its 202 metadata columns, and no path to one.**

Worth recording even though the source is demoted: **ECHO's QueryIDs are small global integers
shared across every user worldwide, and they get recycled.** The investigator's query returned qid
592 (Sacramento County, NAICS 311, 39 rows); replaying that same qid thirty minutes later returned
23 facilities in Glasford, Illinois — somebody else's query in the reused slot, served as HTTP 200
with `Message: "Success"` and no warning. Any adapter must call `get_facility_info` and
`get_qid`/`get_download` back-to-back in one function, never cache a qid, and assert the returned
rows are actually in California near the office before writing anything downstream.

### CDFA Market Enforcement — the single best source found, and it was not in section 3's shortlist

Not a section 3 candidate at all, and it should be the **first adapter built**: the California
Department of Food and Agriculture's Market Enforcement licence registry returns **157 rows inside
the 28-mile circle and every single one carries a phone number** — the only source in this whole
effort with 100% phone fill. It is licensing data for processors, packers and dealers of farm
products, which is close to a definition of this division's ICP. The names that come back are the
right ones: Blue Diamond Growers, Farmers' Rice Cooperative, BCFoods, Sunwest Foods, Elk Grove
Milling — including the two accounts the BOT registry could not surface by category.

Three caveats, all workable:

- **No lat/lng.** Distance has to come from the zip centroid, the same approximation `regions.py`
  already applies to every Placer and Yolo row, so no new machinery is needed.
- **The addresses are mailing addresses**, and 27 of the 157 are PO Boxes. A PO Box row still has a
  phone (which is what the rep needs), but it cannot be distance-filtered or address-matched
  against Places, and it should not be presented as a site address.
- **It is an ASP.NET WebForms search**, so the POST needs a `__VIEWSTATE`/`__EVENTVALIDATION` pair
  scraped from the form immediately beforehand; a stale one is rejected. Fetch-then-post in one
  function, never cache the token.

**Skip the CDFA/IMS dairy half.** It was checked in the same pass and is a separate, thinner
listing that adds little beyond what Market Enforcement already returns.

### CalEPA Regulated Site Portal — undocumented JSON API, and it has both phone and coordinates

The portal ships a public search UI with no documented API, but reading its SPA bundle turned up
the JSON and CSV endpoints behind it, and they are open — no key, no auth. **190 sites in area,
carrying phone *and* true lat/lng, filterable by NAICS/SIC prefix.** The names lean distribution
rather than manufacturing — Sysco, US Foods, Shamrock Foods, US Cold Storage — which is exactly the
"storage and distribution" half of section 1's target that CDFA's grower-facing registry misses.
Best used as a second discovery source and as an enrichment/targeting layer over the others.

Two failure modes to code against, one of them silent:

- **A missing bounding box returns HTTP 500**, which at least fails loudly.
- **A bounding box in the wrong projection returns HTTP 200 with zero rows** — no error, no
  warning, just an empty result that looks like "nothing in this area". Assert a non-zero row count
  for a known-populated bbox in the adapter's own smoke test.
- It is a private API discovered by reading minified JavaScript. It can change shape without any
  notice or changelog. Treat a schema change as expected maintenance, not an incident.

### Google Places — usable as a discovery source, with a review queue, and a hard result cap

The open question from the previous round was whether a processor can be told apart from a
restaurant using only the fields Places returns. **It can, well enough to automate**: `primaryType`
separates the categories cleanly enough that the uncertain remainder is a review queue rather than
a majority. Roughly **350 findable businesses** across the tiled search terms.

The constraint that shapes the tiling in section 4.2: **Text Search caps at 50 results per query,
hard, and relevance collapses well before that** — page 2 of a query was already thin and page 3
returned 0 useful rows out of 10. So the answer to "more coverage" is more, narrower queries, never
deeper paging on a broad one. Budget accordingly: this remains the only billed step in the
pipeline.

### Codebase reuse — the existing modules carry more than expected

Reusable **unchanged**, because they are already tab-generic rather than Leads-specific:
`SheetBackend`/`GspreadBackend`/`FakeSheetBackend` (every method takes a tab name), `plan_reorder`,
the DNC helpers, `regions.py`, `PoliteFetcher`, `PortalCircuit`, and the `LeadCandidate` dataclass.
That is the whole sheet-writing and polite-fetching substrate, and it carries fixes only real runs
surfaced — the Sheets 60-writes-per-minute batching, the `%PDF` body sniff, the address-agreement
check.

Written fresh: the source adapters, a classifier for this ICP, the `Food Facilities` column set,
and a `sync_food_facilities()` modelled on `sync_leads` (same ownership split, same idempotency
shape).

### What this changes about the build order

Section 12's phase 1 assumed BOT + FSIS + ECHO + CalEPA merge into a "permit-grade core". Three of
those four are weaker than the plan assumed, and the strongest source in the whole effort was not
on the list. Corrected order:

1. **CDFA Market Enforcement** — 157 rows, 100% phone, the right names. Highest value per line of
   code, despite the VIEWSTATE handling.
2. **CalEPA site portal** — 190 rows with phone *and* coordinates; covers distribution and cold
   storage, which CDFA does not.
3. **USDA FSIS** — small (28 rows) but perfect data and ~60 lines. **Blocked on one check:** run a
   live fetch from Railway first, since `fsis.usda.gov` 403s this sandbox at the Akamai edge.
4. **Google Places sweep** — many narrow queries, never deep paging; fills the gaps the registries
   miss and enriches rows that lack a phone.
5. **City of Sacramento BOT** — enrichment only, and only behind a name-based classifier.
6. **EPA ECHO** — optional. No phone, and the qid-recycling trap makes it the most dangerous source
   here for the least return.

CDPH and Data Axle stay deferred at the owner's direction (a PRA request and a library card
respectively), and nothing above depends on them.

### One correction to shipped code that came out of this

The appendix's office coordinates (38.6941, -121.4660) and `regions.py`'s (38.691, -121.448)
disagreed by 0.99 miles. The US Census geocoder (Public_AR_Current, queried 2026-09-09) returns
`6948 W 2ND ST, RIO LINDA, CA, 95673` at **38.694109, -121.466030** — the plan was right and the
shipped constant was wrong. `regions.py` is corrected, which slightly changes every distance the
existing lead scraper reports and can move a facility across one of `geo_multiplier`'s band edges.
Worth knowing before the 28-mile cut is applied to a second tab.

## 14. Known limits

- Registries list what is permitted, not what is reachable: many rows will need Google or the rep for a phone.
- Google lists only mapped businesses with a profile; some plants show a corporate switchboard. Registries first is
  the fix for recall; Google is the fix for contactability.
- Categories are inferred; column T flags the uncertain ones and the first call fixes the rest.

## Appendix: handoff inputs for whoever builds this

- Sheet ID `1u6e6psiW581rGr92r86aSJQW2XzPZfzsnhpJlolB4F8`; tabs today: `Leads`, `Signals`, `Runs`, `DNC`; new tab `Food Facilities`.
- Office: 6948 West 2nd St, Rio Linda, CA 95673; 38.6941, -121.4660; radius 28.0 mi straight-line.
- City of Sacramento BOT: `https://services5.arcgis.com/54falWtcpty3V47Z/arcgis/rest/services/account_data_with_header_NEW/FeatureServer/0`
  (query with `where=1=1&outFields=*&f=json`, page with `resultOffset`).
- USDA FSIS MPI Directory: `https://www.fsis.usda.gov/inspection/establishments/meat-poultry-and-egg-product-inspection-directory` (CSV).
- EPA ECHO facility search: `https://echo.epa.gov/facilities/facility-search` (NAICS filter, CSV export).
- CalEPA Regulated Site Portal: `https://siteportal.calepa.ca.gov/nsite/`; CalEPA Open Data for bulk downloads.
- CDFA Market Enforcement licence search: an ASP.NET WebForms page under cdfa.ca.gov; the recon
  agent's exact endpoint was not carried forward, so re-derive it (and the `__VIEWSTATE` pair) from
  the live form when the adapter is written rather than trusting a URL written down from memory.
- CalEPA: the documented portal is `https://siteportal.calepa.ca.gov/nsite/`; the JSON/CSV
  endpoints the adapter should use are undocumented and were found by reading that SPA's bundle —
  re-derive them the same way, and assert a non-zero row count for a known bbox.
- CDPH Food and Drug Branch (PRA request): FDBfood@cdph.ca.gov, (916) 650-6500.
- Google: Places API (New) Text Search + Place Details; Sheets API v4 with a service account.
