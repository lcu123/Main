# Plan: "Food Facilities" call-list tab for Zest Commercial Leads

Status: **plan only, nothing built yet.** Written 2026-09-08 for owner review. No code has been written and the
Google Sheet has not been touched. Everything below is read-only findings plus a proposal.

Target sheet: *Zest Commercial Leads*
(`https://docs.google.com/spreadsheets/d/1u6e6psiW581rGr92r86aSJQW2XzPZfzsnhpJlolB4F8`).

## 1. Goal

Add one new tab that a salesperson works top-to-bottom to sell pest-control inspections to food processing
plants, food manufacturers, and food storage/distribution facilities in Zest's service area. Restaurants and
retail markets are out (they are already covered by the county health-inspection pipeline in the Leads tab).
Every row needs: business name, phone, type of business, city, plus Notes and Follow-up date for the rep.

## 2. What is in the sheet today (read-only look)

| Tab | Shape | What it is |
| --- | --- | --- |
| `Leads` | 205 rows x 41 cols | County health-inspection leads (Sacramento EMD, Placer PCHD, Yolo). Keyed by permit ID (`SACEMD:FA...`). Has rep-owned columns: status, notes, rep, last touch date, next step date, touch count, inspection date, outcome, FieldRoutes customer ID. |
| `Signals` | 63 rows | One row per inspection report (vermin quotes, result, report URL). |
| `Runs` | 5 rows | Run log of the existing lead pipeline (rows added / flagged / skipped dnc / skipped cap / errors). |
| `DNC` | header only | Do-not-call list: key, phone, email, reason, added at. |

No formulas, data validation, conditional formatting, or protected ranges anywhere, so nothing to break by
accident. The existing pipeline clearly writes by column header name (its last run logged a complaint about a
duplicate "report link" column), caps itself at 40 new rows per run, and respects the DNC tab. The new tab will
follow those same conventions.

**Why food processors are missing from Leads:** the county inspection portals only cover *retail* food
(restaurants, markets, commissaries, satellite distribution). Wholesale processors and manufacturers are
licensed by the California Department of Public Health Food and Drug Branch, meat/poultry by USDA FSIS or
CDFA, and dairies by CDFA. None of those publish an open dataset for our area (checked the CHHS open-data
portal on 2026-09-08: nothing for FDB facility registrations). That is why this list has to come from Google.

## 3. The new tab

Name: **`Food Facilities`** (append as the last tab so nothing that addresses tabs by position shifts).

Column layout, left to right, in the order the owner asked for (call columns first, rep columns next, supporting
data after):

| Col | Header | Owner | Notes |
| --- | --- | --- | --- |
| A | Business name | script | From Google `displayName`, whitespace-cleaned |
| B | Phone | script | Google `nationalPhoneNumber`, formatted `(916) 555-1234`; blank if Google has none |
| C | Type of business | script | Our plain-English category (list in section 4.4), not Google's raw type |
| D | City | script | From `addressComponents` |
| E | Notes | **rep** | Free text. Never touched by the script |
| F | Follow-up date | **rep** | Date-validated. Never touched by the script |
| G | Status | **rep** | Dropdown: New, No answer, Callback, Interested, Inspection booked, Not interested, Bad number, Do not call |
| H | Rep | **rep** | Who owns the lead |
| I | Last call date | **rep** | Date |
| J | Address | script | Street address |
| K | Zip | script | |
| L | Website | script | Google `websiteUri` |
| M | Google Maps link | script | `googleMapsUri`, one click to see the building/reviews |
| N | Open/closed | script | Google `businessStatus` (OPERATIONAL / CLOSED_TEMPORARILY / CLOSED_PERMANENTLY) |
| O | Distance (mi) | script | Straight-line miles from the Rio Linda office, same idea as the Leads tab |
| P | Region | script | Same labels as the Leads tab (Downtown, Carmichael, North Highlands / Antelope / Rio Linda, ...) |
| Q | Found via | script | Which search terms surfaced it, e.g. `meat processing; cold storage` |
| R | Google category | script | Raw `primaryType` for transparency |
| S | Needs review? | script | `yes` when the classifier is unsure (e.g. a bakery that may be retail) |
| T | In Leads tab? | script | `yes` if the same name+zip or phone already exists in `Leads` |
| U | FieldRoutes customer ID | **rep** | Same convention as Leads; filled by hand when they sign |
| V | Place ID | script | Google place ID. The row key. Do not edit |
| W | First seen | script | Date the row was added |
| X | Last refreshed | script | Date the script last checked it |

Formatting done once when the tab is created: frozen header row, bold header, sensible column widths, filter
on the header row, status dropdown (data validation), date validation on F and I, and warning-only protection
on the script-owned columns so a rep gets a "are you sure?" before overwriting a phone number. Rows sorted by
distance so the densest, closest facilities come first.

Rep-vs-script contract: the script only ever writes to its own columns. On a refresh it (a) appends new
facilities, (b) updates Open/closed and Last refreshed, and (c) fills in a blank Phone or Website if Google
now has one. It never rewrites a non-blank cell in A-D and never touches E-I or U.

## 4. Data source: Google Places API (New)

### 4.1 Why Text Search, and why keywords

Verified against Google's place-type table on 2026-09-08: there is **no place type for food processing,
factory, warehouse, distribution center, or cold storage.** The nearest types are `manufacturer`, `supplier`,
`wholesaler`, `farm`, `bakery`, `butcher_shop`. Nearby Search (type-driven) therefore cannot find these
businesses. Text Search (keyword-driven) can, so the pull is a battery of keyword queries, each restricted to a
geographic rectangle, with post-filtering on the returned types.

Text Search facts that shape the design (from Google's docs, same date): max 20 results per page and **60
results per query** (3 pages via `nextPageToken`); `locationRestriction` accepts a **rectangle only**;
`includedType` accepts one type with optional `strictTypeFiltering`.

### 4.2 Coverage area and tiling

Default coverage: a rectangle roughly 30 miles in each direction from the Rio Linda office. That reaches
Woodland and Davis (west), Elk Grove and Galt (south), Folsom and El Dorado Hills (east), and Roseville,
Rocklin, Lincoln and Auburn (northeast), and includes the West Sacramento and Power Inn Road industrial
corridors and McClellan Park, where most of the region's food plants and cold storage sit. Yuba City/Marysville,
Lodi/Stockton and Dixon are outside by default (owner decision, see section 9).

Because a query returns at most 60 places, each keyword is first run against the whole rectangle. If it comes
back saturated (60 results), the rectangle is split into four quadrants and the keyword re-run in each,
recursively, until no quadrant saturates. Food processing is sparse, so most keywords finish in one query and
only the dense ones (e.g. "food distributor") subdivide. Results are de-duplicated by place ID across all
queries.

### 4.3 Search terms (draft, about 50; tune after the first dry run)

Processing/manufacturing: food processing plant, food processing facility, food manufacturer, food packaging
company, co-packer, commercial bakery, wholesale bakery, tortilla factory, meat processing, meat packing,
slaughterhouse, poultry processing, seafood processor, dairy processing plant, creamery, cheese manufacturer,
egg processing, produce packing, fruit packing house, nut processing, almond processor, rice mill, flour mill,
feed mill, cannery, frozen food manufacturer, snack food manufacturer, candy manufacturer, spice manufacturer,
sauce manufacturer, pet food manufacturer, ice manufacturer.

Beverage: beverage manufacturer, bottling plant, juice processing, coffee roaster wholesale, brewery production
facility, winery production facility, distillery.

Storage/distribution: cold storage warehouse, refrigerated warehouse, food distribution center, food
distributor, wholesale grocer, produce distributor, meat distributor, seafood distributor, beverage
distributor, food warehouse, food bank warehouse.

Kitchens: commissary kitchen, central kitchen, catering commissary, school district central kitchen.

Plus type-driven passes with `includedType` = `manufacturer`, `wholesaler`, `supplier` and the query "food",
strict type filtering on, to catch places Google categorised but whose names do not contain a food word.

### 4.4 Filtering and classification

Drop a place if its `primaryType` or `types` contain any restaurant-family type (`restaurant` and all
`*_restaurant`, `cafe`, `coffee_shop`, `bar`, `meal_takeaway`, `meal_delivery`, `food_court`, `ice_cream_shop`,
`donut_shop`, `juice_shop`, `dessert_shop`, `sandwich_shop`) or any retail type (`grocery_store`,
`supermarket`, `convenience_store`, `liquor_store`, `asian_grocery_store`, `market`, `warehouse_store`,
`candy_store`, `chocolate_shop`). Retail is already worked from the county permits in Leads. Also drop on a
name blacklist (Restaurant, Cafe, Grill, Taqueria, Pizza, Sushi, Deli, Bistro, ...).

Keep `bakery`, `butcher_shop`, brewery and winery hits only when no restaurant/cafe type is present, and mark
them `Needs review? = yes`, because Google cannot tell a wholesale bakery from a cupcake shop.

"Type of business" (column C) is assigned in this order: name keywords, then the search term that found it,
then Google's `primaryType`. Categories:

- Meat & poultry processing
- Seafood processing
- Dairy / creamery
- Commercial bakery / tortilla
- Produce packing / nut & rice processing
- Snack, candy & confectionery manufacturing
- Sauce, spice & prepared foods manufacturing
- Frozen & ready-meal manufacturing
- Beverage production (bottling, juice, coffee roasting, brewery, winery, distillery)
- Cold storage / refrigerated warehouse
- Food distribution / wholesale grocer
- Commissary / central kitchen
- Pet & animal food manufacturing
- Food packaging / co-packer
- Other food manufacturing

### 4.5 Phones and websites (two-step, to stay in the free tier)

Discovery runs Text Search with **Pro-tier fields only** (`id`, `displayName`, `formattedAddress`,
`addressComponents`, `location`, `primaryType`, `types`, `businessStatus`, `googleMapsUri`). Phone and website
are **Enterprise-tier** fields, so they are fetched with a Place Details call only for the places that survive
the filter. That way we pay for phone lookups on real food facilities, not on the restaurants we throw away.

### 4.6 Cross-checks before writing

- **DNC:** any phone present in the `DNC` tab is skipped (read-only read of that tab).
- **Already in Leads:** name+zip or phone match against `Leads` sets `In Leads tab? = yes` (the row is kept so
  the rep sees it, but knows it is being worked elsewhere). Read-only read of that tab.
- **Optional, later:** flag facilities that are already FieldRoutes customers by pulling commercial customers'
  phones once through the FieldRoutes MCP and matching locally (a handful of reads, not one per row).

## 5. How the other tabs stay untouched

- A Google Cloud **service account** is shared on this one spreadsheet as Editor; it has no access to anything
  else in Drive.
- Every write is addressed to the `Food Facilities` sheet ID, resolved from the tab title at startup and
  re-checked before each write. The script never issues a spreadsheet-wide operation: no clears, no sheet
  reordering, no column inserts on other tabs, no changes to `Leads`, `Signals`, `Runs` or `DNC`.
- The tab is created with `addSheet` only if it does not exist, appended at the end.
- Rows are **upserted by Place ID** (column V): existing rows are updated in place cell-by-cell in script-owned
  columns only; new rows are appended below the last row.
- Before and after every run the script snapshots the row count and a hash of the values of every *other* tab
  and refuses to report success if any of them changed. This makes "did not disturb the other tabs" a checked
  fact, not a promise.
- First run is a **dry run**: it writes a CSV to disk for the owner to eyeball (row count, category breakdown,
  30 sample rows) and touches nothing in Google Sheets. Only after sign-off does `--write` run.
- The run log is printed and saved locally, not appended to the existing `Runs` tab (that tab belongs to the
  other pipeline).

## 6. Cost, quota, time

Verified on Google's pricing page 2026-09-08 (Places API (New), first 100k tier):

| SKU | Price / 1,000 | Free per month | Our use |
| --- | --- | --- | --- |
| Text Search Pro | $32.00 | 5,000 calls | Discovery: est. 300-800 calls for a full pull |
| Place Details Enterprise (phone + website) | about $20 (confirm on the pricing page) | 1,000 calls | One per kept facility: est. 400-800 |
| Text Search Enterprise | $35.00 | 1,000 calls | Not used (phones inline would cost more) |

Expected result: **$0 for the first pull and $0 for a monthly refresh** as long as both stay inside the free
allowances; worst case a few dollars if the kept list passes 1,000. A Google Cloud project with billing enabled
is still required (Google needs a card on file even for free-tier usage). API key restricted to Places API (New)
and, separately, a Sheets API service account.

Expected volume after filtering: roughly 400-800 unique facilities across the 30-mile box (estimate; Sacramento
County alone has a few hundred food-manufacturing establishments, plus Yolo and Placer, plus distributors and
cold storage). A full pull takes about 10-15 minutes with gentle pacing; raw API responses are cached to disk so
re-runs while tuning filters do not re-spend quota.

## 7. Terms-of-service notes (owner should be aware)

Google's Places policy lets **place IDs be stored indefinitely** but restricts caching or storing other Places
content beyond Google's terms. Two practical mitigations are built in: the tab is refreshed from the stored
place IDs (so the Google-sourced cells are never stale), and once a rep has spoken to a facility the useful
facts live in the rep's own Notes. Google also asks for a "Source: Google" attribution when its data is shown
outside a Google Map; the header row will carry it. Not legal advice; flagging it so the owner can decide.

## 8. Where the code lives and how it runs

- `scripts/food_facilities/` in this repo: `pull.py` (search, filter, classify, enrich), `sheet.py` (tab
  creation, formatting, upsert, other-tab checksum), `classify.py` (categories, blacklists),
  `requirements.txt` (google-api-python-client, google-auth, httpx). **Not** added to the MCP server's
  runtime dependencies, Docker image, or `requirements.lock`; the Railway service is unaffected.
- Tests in `tests/test_food_facilities.py` run against recorded Places responses and a fake Sheets client,
  so CI needs no keys and spends no quota.
- New env vars, documented in `.env.example`: `GOOGLE_MAPS_API_KEY`, `GOOGLE_SHEETS_SA_JSON` (path to the
  service-account key), `LEADS_SHEET_ID`, `FOOD_TAB_NAME` (default `Food Facilities`), `OFFICE_LAT`/`OFFICE_LNG`.
- Commands: `pull --dry-run` (CSV only), `pull --write` (first fill), `refresh` (monthly: new rows, open/closed,
  fill blank phones). Run by hand to start; a Railway cron for the monthly refresh is optional later.
- README gets a short "Food Facilities tab" section; CLAUDE.md gets a pointer so future sessions know the
  script exists and what it must never touch.

## 9. Phases and what I need from the owner

**Phase 0, owner setup (about 20 minutes, blocks everything else)**
1. Google Cloud project (recommend the Zest Workspace account that owns the sheet), billing enabled.
2. Enable *Places API (New)* and *Google Sheets API*. Create an API key restricted to Places API (New).
3. Create a service account, download its JSON key, share the spreadsheet with the service account's email
   as Editor.
4. Put the key and JSON path in a local `.env` (or Railway variables). Never paste them into chat or GitHub.
5. Answer the decisions in section 10.

**Phase 1, discovery dry run**: search + filter + classify, CSV out. Owner spot-checks 30 rows; we tune
search terms and blacklists. No Sheets access needed yet.

**Phase 2, enrichment and cross-checks**: phones/websites, distance/region, DNC and Leads matching.

**Phase 3, tab creation and first write**: create `Food Facilities`, apply formatting and dropdowns, write
rows, verify the other-tab checksums, hand the tab to the rep.

**Phase 4, refresh and docs**: `refresh` command, tests, README/CLAUDE.md/.env.example updates.

**Phase 5, optional**: FieldRoutes existing-customer flag; one-time import of the USDA FSIS Meat, Poultry and
Egg Product Inspection Directory (public, includes phones) for meat/poultry plants Google may not list.

## 10. Decisions needed before code

1. Tab name `Food Facilities`, and the A-X column order in section 3? (Alternative: Notes and Follow-up date
   as columns A-B ahead of Name/Phone/Type/City. Either is a one-line change.)
2. Coverage: 30-mile box as described, or extend to Yuba City/Marysville, Dixon, Lodi/Stockton?
3. Include beverage producers (breweries, wineries, distilleries) and bakeries with storefronts, flagged for
   review, or exclude them?
4. Include institutional kitchens (school-district central kitchens, hospital food service, food bank)?
5. Keep rows that already appear in `Leads` (flagged) or drop them?
6. Status dropdown values in section 3 OK, or match whatever the rep uses in `Leads` today?
7. Run cadence: one-time pull now plus monthly refresh run on request, or a Railway cron?
8. Who sets up the Google Cloud project and billing (Phase 0)?

## 11. Known limits

- Google only lists businesses that have a Business Profile or that Google has mapped. Some industrial plants
  behind a corporate name will be missing or will show a corporate HQ phone. Expect good but not complete
  recall; the FSIS import in Phase 5 is the cheapest patch for meat/poultry.
- Google's categories are coarse for industrial businesses, so column C is our best inference and column S
  flags the uncertain ones. The rep's first call fixes any misclassification.
- Phones are whatever Google has: sometimes a switchboard, sometimes nothing. Blank phones stay in the list
  (with the website) so the rep can still look them up.
