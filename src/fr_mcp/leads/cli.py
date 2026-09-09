"""`fr-leads`: the lead scraper's command line entry point.

Subcommands:
  run       Full pipeline: pull every in-scope county feed (Sacramento,
            Placer, Yolo -- `--counties` narrows this), score, dedupe, and
            push into the destination (`--destination sheet|fieldroutes`,
            default `sheet` -- plan section 5). This is what the scheduled
            job runs.
  preview   Same pull and scoring, no writes to either destination at all
            (not even reads) -- a quick look at what a run would find.
  push      Push one or more named Sacramento facilities straight to
            FieldRoutes. `--as-customer` is the validation mode: writes a
            note + task to an existing (allowlisted) customer instead of
            creating one, per docs/lead-scraper-plan.md's validation
            checklist. This one stays FieldRoutes-only and Sacramento-only --
            it is a validation tool for Appendix A's write path, not part of
            the sheet pipeline.

Every subcommand prints one JSON line per candidate decision, then a final
JSON summary line, to stdout -- easy to grep, easy for a log drain. Nothing
here prints the FieldRoutes key/token (client.py already strips `params`
from every response before it reaches this code) or a Google credential.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx
from mcp.server.mcpserver.exceptions import ToolError

from fr_mcp import server
from fr_mcp.client import FieldRoutesError

from . import apartments as apt
from . import arcgis as ag
from . import calepa
from . import cdfa
from . import food
from . import fr_push as push
from . import fsis
from . import myhd
from . import pipeline as pl
from . import places
from . import sheet
from .reports import PoliteFetcher

DEFAULT_LOOKBACK_DAYS = 45
DEFAULT_BACKFILL_DAYS = 180
ALL_COUNTIES = ("sacramento", "placer", "yolo")
_MYHD_CONFIGS = {"placer": myhd.PLACER, "yolo": myhd.YOLO}


def _cache_dir() -> Path:
    return Path(os.environ.get("LEADS_PDF_CACHE_DIR", "./leads_cache/pdf"))


def _parse_counties(raw: str) -> tuple[str, ...]:
    wanted = tuple(c.strip().lower() for c in raw.split(",") if c.strip())
    unknown = sorted(set(wanted) - set(ALL_COUNTIES))
    if unknown:
        raise SystemExit(f"unknown --counties value(s) {unknown} -- choose from {ALL_COUNTIES}")
    return wanted or ALL_COUNTIES


def _print(obj: object) -> None:
    print(server._j(obj))


def _candidate_row(c: pl.LeadCandidate) -> dict:
    return {
        "facilityId": c.facility_id,
        "county": c.county,
        "name": c.name,
        "address": ", ".join(p for p in (c.street, c.city, c.zip5) if p),
        "lane": c.lane,
        "tier": c.score.tier,
        "score": c.score.total,
        "scoreBreakdown": {
            "icpFit": c.score.icp_fit,
            "pestSignal": c.score.pest_signal,
            "recency": c.score.recency,
            "reachability": c.score.reachability,
            "geo": c.score.geo,
        },
        "classification": c.classification.label,
        "distanceMi": round(c.distance_miles, 1) if c.distance_miles is not None else None,
        "regionID": c.region_id,
        "regionName": c.region_name,
        "isChain": c.is_chain,
        "permits": c.permits,
        "signalDate": c.signal.date.isoformat() if c.signal else None,
        "signalResult": c.signal.result if c.signal else None,
    }


async def _pull_facilities(*, since_days: int, today: date) -> dict[str, ag.Facility]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        return await pl.pull_and_normalize(client, lookback_days=since_days, today=today)


async def _build(
    facilities: dict[str, ag.Facility], *, today: date, fetch_pdfs: bool, stats: dict | None = None
) -> list[pl.LeadCandidate]:
    if not fetch_pdfs:
        return await pl.build_candidates(facilities, today=today, fetcher=None)
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        fetcher = PoliteFetcher(_cache_dir(), http_client)
        out = await pl.build_candidates(facilities, today=today, fetcher=fetcher)
    if stats is not None:
        stats["reportsFetched"] = fetcher.fetched
        stats["reportCacheHits"] = fetcher.cache_hits
        stats["reportsBlocked"] = fetcher.blocked
    return out


async def _pull_placer_yolo(
    *, since_days: int, today: date, fetch_pdfs: bool, counties: tuple[str, ...], stats: dict | None = None
) -> list[pl.LeadCandidate]:
    """Placer and Yolo share one PortalCircuit (and, for Yolo, one PoliteFetcher)
    so a block on either county's search stops the other's search and Yolo's PDF
    fetches too for the rest of this run (plan section 8)."""
    wanted = [c for c in ("placer", "yolo") if c in counties]
    if not wanted:
        return []
    circuit = myhd.PortalCircuit()
    out: list[pl.LeadCandidate] = []
    async with httpx.AsyncClient(timeout=30.0) as client:
        fetcher = PoliteFetcher(_cache_dir(), client) if fetch_pdfs else None
        for name in wanted:
            found = await myhd.pull_and_build(
                client, _MYHD_CONFIGS[name], circuit, lookback_days=since_days, today=today, fetcher=fetcher
            )
            if stats is not None:
                stats[name] = len(found)
            out.extend(found)
    if stats is not None and circuit.blocked:
        stats["portalBlocked"] = circuit.block_reason or "portal refused a request"
    return out


async def _build_all(
    *, since_days: int, today: date, fetch_pdfs: bool, counties: tuple[str, ...], stats: dict | None = None
) -> list[pl.LeadCandidate]:
    """Placer and Yolo go FIRST, and the order is not cosmetic.

    Both counties and Sacramento's report PDFs come off the same
    myhealthdepartment.com host, and it rate-limits by IP. Sacramento's PDF pass
    is by far the heaviest thing this pipeline does -- the first Railway run
    fetched about 150 reports at one every two seconds, ran for five minutes, and
    was 403'd; Placer's very first search then hit the same 403 and tripped its
    circuit, so on 2026-09-09 both counties contributed **zero rows and the run
    still reported `errors: []`**. Their searches are a handful of requests, so
    running them before the PDF pass costs nothing and means a block earned by
    report volume can no longer starve them."""
    candidates: list[pl.LeadCandidate] = []
    candidates.extend(
        await _pull_placer_yolo(
            since_days=since_days, today=today, fetch_pdfs=fetch_pdfs, counties=counties, stats=stats
        )
    )
    if "sacramento" in counties:
        facilities = await _pull_facilities(since_days=since_days, today=today)
        sac = await _build(facilities, today=today, fetch_pdfs=fetch_pdfs, stats=stats)
        if stats is not None:
            stats["sacramento"] = len(sac)
        candidates.extend(sac)
    return pl.rank(candidates)


def _worth_enriching(
    candidates: list[pl.LeadCandidate], *, existing_keys: set[str], skip_keys: set[str], new_row_cap: int
) -> list[pl.LeadCandidate]:
    """The candidates a lookup would actually pay off on: rows already in the
    sheet (a backfill lands on them today) plus the new ones the row cap will
    admit. Enriching past the cap buys nothing -- those candidates are dropped
    before they are written, and the next run rebuilds and re-pays for them."""
    out, new_budget = [], new_row_cap
    for c in candidates:
        if not c.pushable or c.customer_link in skip_keys:
            continue
        if c.customer_link in existing_keys:
            out.append(c)
        elif new_budget > 0:
            out.append(c)
            new_budget -= 1
    return out


async def _enrich_phones(
    candidates: list[pl.LeadCandidate],
    *,
    skip_keys: set[str] | None = None,
    existing_keys: set[str] | None = None,
    new_row_cap: int = sheet.DEFAULT_NEW_ROW_CAP,
) -> dict:
    """Look every pushable candidate up in Places for its listed business number,
    website and open/closed status, and fill `phone` too when the county gave us
    none (all of Placer, all of Yolo, ~10% of Sacramento).

    The county's own number comes off the inspection report header and is often
    the owner's personal mobile rather than the line a rep should dial, so the
    Places number is kept alongside it rather than replacing it. `skip_keys` is
    the set already carrying one: this is the only billed step in the pipeline,
    so a row is looked up once, not every morning. Ranked order, so a spent
    budget costs the coldest leads rather than the hottest."""
    needs = _worth_enriching(
        candidates,
        existing_keys=existing_keys or set(),
        skip_keys=skip_keys or set(),
        new_row_cap=new_row_cap,
    )
    if not needs:
        return {"attempted": 0, "matched": 0}
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        # Enrichment is an optional extra on top of the run; the sheet write is
        # the point of it. A credential or quota problem here must degrade to
        # "no business phones this run", never take the whole run down with it
        # -- which is exactly what it did on 2026-09-09 until this was caught.
        client: places.PlacesClient | None = None
        try:
            client = places.PlacesClient(http_client, places.service_account_token_provider())
            for c in needs:
                if client.blocked or client.budget_left <= 0:
                    break
                hit = await client.lookup(name=c.name, street=c.street, city=c.city, zip5=c.zip5)
                if hit is None:
                    continue
                c.business_phone = hit.phone
                if hit.phone and not c.best_phone:
                    c.phone, c.phone_source = hit.phone, "google_places"
                c.website = hit.website
                c.business_status = hit.business_status
        except places.PlacesError as exc:
            if client is None:  # the credential itself never resolved
                return {"attempted": 0, "matched": 0, "blocked": str(exc)}
            client.blocked, client.block_reason = True, str(exc)
    return {
        "attempted": client.calls,
        "matched": client.matched,
        "addressMismatch": client.rejected,
        "budgetLeft": client.budget_left,
        "blocked": client.block_reason,
    }


def _pull_warnings(pull: dict, counties: tuple[str, ...]) -> list[str]:
    """Turn a silent pull failure into something that reads as a problem.

    A blocked portal is not an exception -- every layer soft-fails by design, so
    the run completes, writes what it has, and reports `errors: []`. That is
    right for the write path and wrong for the summary: on 2026-09-09 Placer and
    Yolo contributed nothing at all and nothing said so."""
    out: list[str] = []
    if pull.get("portalBlocked"):
        out.append(f"county portal blocked this run ({pull['portalBlocked']}) -- Placer/Yolo may be incomplete")
    if pull.get("reportsBlocked"):
        out.append(
            "the report-PDF endpoint blocked us partway, so some Sacramento rows "
            "have no owner or phone this run"
        )
    for name in ("placer", "yolo"):
        if name in counties and pull.get(name) == 0:
            out.append(f"{name} returned no candidates at all -- expected on a quiet day, suspicious two days running")
    return out


async def cmd_preview(args: argparse.Namespace) -> int:
    today = date.today()
    counties = _parse_counties(args.counties)
    pull: dict = {}
    candidates = await _build_all(
        since_days=args.since_days, today=today, fetch_pdfs=not args.no_fetch, counties=counties, stats=pull
    )
    shown = [c for c in candidates if c.lane != pl.LANE_CHAIN][: args.top]
    for c in shown:
        _print(_candidate_row(c))
    _print(
        {
            "summary": True,
            "counties": list(counties),
            "totalCandidates": len(candidates),
            "event": sum(1 for c in candidates if c.lane == pl.LANE_EVENT),
            "territory": sum(1 for c in candidates if c.lane == pl.LANE_TERRITORY),
            "parkedChains": sum(1 for c in candidates if c.lane == pl.LANE_CHAIN),
            "byCounty": {
                county: sum(1 for c in candidates if c.county == county)
                for county in sorted({c.county for c in candidates})
            },
            "shown": len(shown),
            "pull": pull,
        }
    )
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    today = date.today()
    counties = _parse_counties(args.counties)
    pull: dict = {}
    candidates = await _build_all(
        since_days=args.since_days, today=today, fetch_pdfs=True, counties=counties, stats=pull
    )
    if args.limit:
        candidates = candidates[: args.limit]

    backend = sheet.open_backend() if args.destination == "sheet" else None
    enrichment = {"attempted": 0, "matched": 0}
    if not args.no_places:
        existing = sheet.existing_keys(backend) if backend is not None else set()
        already = sheet.keys_with_business_phone(backend) if backend is not None else set()
        enrichment = await _enrich_phones(candidates, skip_keys=already, existing_keys=existing)

    if args.destination == "sheet":
        result = sheet.sync_leads(backend, candidates, today=today, dry_run=args.dry_run)
        _print(
            {
                "summary": True,
                "destination": "sheet",
                "dryRun": args.dry_run,
                # Per-county counts and the portal's state: a run where the portal
                # blocked us contributes zero rows for a county while `errors` stays
                # empty, which is how 2026-09-09's run looked fine and was not.
                "pull": pull,
                "added": result.added,
                "flagged": result.flagged,
                "enriched": result.enriched,
                "places": enrichment,
                "skippedDnc": result.skipped_dnc,
                "skippedCap": result.skipped_cap,
                "skippedDuplicate": result.skipped_duplicate,
                "skippedNoChange": result.skipped_no_change,
                "errors": result.errors + _pull_warnings(pull, counties),
            }
        )
        return 1 if result.errors and result.added == 0 and result.flagged == 0 else 0

    summary = await push.push_run(candidates, today=today, dry_run=args.dry_run)
    for r in summary.results:
        _print(
            {
                "facilityId": r.facility_id,
                "action": r.action,
                "customerId": r.customer_id,
                "noteId": r.note_id,
                "taskId": r.task_id,
            }
        )
    usage = server.client().usage.snapshot() if not args.dry_run else None
    _print(
        {
            "summary": True,
            "destination": "fieldroutes",
            "dryRun": args.dry_run,
            "created": summary.created,
            "retouched": summary.retouched,
            "skippedCap": summary.skipped_cap,
            "skippedDuplicate": summary.skipped_duplicate,
            "errors": summary.errors,
            "quota": usage,
        }
    )
    return 1 if summary.errors and summary.created == 0 and summary.retouched == 0 else 0


async def cmd_push(args: argparse.Namespace) -> int:
    today = date.today()
    facilities = await _pull_facilities(since_days=DEFAULT_BACKFILL_DAYS, today=today)
    wanted = set(args.facility)
    facilities = {fid: f for fid, f in facilities.items() if fid in wanted}
    missing = wanted - facilities.keys()
    if missing:
        _print({"error": f"facility ID(s) not found in the feed: {sorted(missing)}"})
        return 2
    candidates = await _build(facilities, today=today, fetch_pdfs=True)
    results = []
    for c in candidates:
        if args.as_customer:
            r = await push.retouch(
                c, args.as_customer, today=today, dry_run=args.dry_run, is_real_customer=True
            )
        else:
            r = await push.push_candidate(c, today=today, dry_run=args.dry_run)
        results.append(r)
        _print(_candidate_row(c))
        _print({"facilityId": r.facility_id, "action": r.action, "customerId": r.customer_id, "noteId": r.note_id, "taskId": r.task_id})
    return 0


# The 28-mile circle's bounding box, in the EPSG:4326 degrees CalEPA's portal wants.
# Padded to a whole degree-ish box because the portal filters by rectangle and
# `food.merge` trims to the circle afterwards -- a tight box would clip a facility
# near the edge that the distance rule would have kept.
FOOD_BBOX = (-121.95, 38.25, -121.00, 39.15)

# Pull order is merge order, and merge order decides which source owns a shared
# address: the ones with real coordinates and a site address come before the one
# with a mailing address. FSIS sits between them -- perfect data, but only meat and
# poultry, so it should not claim a row CalEPA already describes more fully.
FOOD_SOURCES = ("calepa", "fsis", "cdfa")

# The rectangle Places searches, as a Rect. Same box as FOOD_BBOX; `food.merge`
# trims to the 28-mile circle afterwards, since Text Search takes a rectangle and
# nothing else.
FOOD_RECT = places.Rect(south=FOOD_BBOX[1], west=FOOD_BBOX[0], north=FOOD_BBOX[3], east=FOOD_BBOX[2])

# The sweep is many requests where enrichment is one per row, so it gets its own
# ceiling rather than sharing the enrichment budget. ~60 keywords plus four typed
# passes, most resolving in one or two pages, is roughly 100-150 requests; 400
# leaves room for the quadrant splits without letting a recursion run away.
DEFAULT_SWEEP_CALLS = 400


def sweep_call_ceiling() -> int:
    raw = os.environ.get("LEADS_PLACES_SWEEP_CALLS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_SWEEP_CALLS
    except ValueError:
        return DEFAULT_SWEEP_CALLS
    return value if value >= 0 else DEFAULT_SWEEP_CALLS


async def _sweep_places(*, include_uncertain: bool = False) -> tuple[list[food.FoodFacility], dict]:
    """Google's keyword sweep: the businesses no registry lists.

    Kept separate from `_enrich_food_phones` because the two spend money very
    differently -- enrichment is one request per known row, this is a hundred-plus
    requests that discover rows. It is opt-in (`--sweep`) for that reason.

    By default only the rows Google's own `primaryType` confirms as a processor,
    producer or wholesaler are kept. Measured live 2026-09-09 over the full
    28-mile rectangle: the sweep finds ~2,500 businesses, of which ~1,340 survive
    the retail and closed-listing filters -- but **796 of those are unconfirmed
    guesses**, and adding them would bury the 281 registry rows under a majority
    of maybes. A call list a rep cannot work top-to-bottom is not an improvement.
    The confident half is genuinely new business (Kikkoman, HP Hood, The Better
    Meat Co., Pacific International Rice Mills -- none of them in any registry);
    `--sweep-include-uncertain` adds the rest for anyone willing to triage."""
    async with httpx.AsyncClient(timeout=60.0) as http_client:
        client: places.PlacesClient | None = None
        try:
            client = places.PlacesClient(
                http_client,
                places.service_account_token_provider(),
                call_ceiling=sweep_call_ceiling(),
            )
            rows = await client.sweep(
                list(places.SWEEP_QUERIES), FOOD_RECT, typed_queries=list(places.SWEEP_TYPED_QUERIES)
            )
        except places.PlacesError as exc:
            return [], {"attempted": 0, "found": 0, "blocked": str(exc)}
    classified = [f for f in (food.from_places(r) for r in rows.values()) if f]
    kept = classified if include_uncertain else [f for f in classified if not f.needs_review]
    return kept, {
        "attempted": client.calls,
        "found": len(rows),
        "classified": len(classified),
        "kept": len(kept),
        "heldBackAsUncertain": len(classified) - len(kept),
        "budgetLeft": client.budget_left,
        "blocked": client.block_reason,
    }


async def _build_food_facilities(
    *, sources: tuple[str, ...], sweep: bool = False, include_uncertain: bool = False
) -> tuple[list[food.FoodFacility], dict]:
    """Pull every enabled registry, classify, merge, and trim to the radius.

    CalEPA goes in first and CDFA second, and the order is the point: `food.merge`
    lets the first source to claim an address own the row, and CalEPA is the one
    with real coordinates and a site address rather than a mailing one."""
    stats: dict = {}
    groups: list[list[food.FoodFacility]] = []
    async with httpx.AsyncClient(timeout=180.0) as http_client:
        if "calepa" in sources:
            sites = await calepa.fetch_sites(http_client, bbox=FOOD_BBOX)
            kept = [f for f in (food.from_calepa(s) for s in sites) if f]
            stats["calepa"] = {"found": len(sites), "kept": len(kept)}
            groups.append(kept)
        if "fsis" in sources:
            ests, origin = await fsis.fetch_establishments(http_client)
            near = fsis.within(ests, food.MAX_MILES)
            kept = [f for f in (food.from_fsis(e) for e in near) if f]
            stats["fsis"] = {"national": len(ests), "inRange": len(near), "kept": len(kept), "source": origin}
            groups.append(kept)
        if "cdfa" in sources:
            licensees = cdfa.within(await cdfa.fetch_licensees(http_client), food.MAX_MILES)
            kept = [f for f in (food.from_cdfa(x) for x in licensees) if f]
            stats["cdfa"] = {"inRange": len(licensees), "kept": len(kept)}
            groups.append(kept)
    if sweep:
        swept, sweep_stats = await _sweep_places(include_uncertain=include_uncertain)
        stats["sweep"] = sweep_stats
        # Last, so a registry that already describes an address keeps the row: a
        # licence number is better provenance than a place ID, and the registry
        # row carries a category somebody filed rather than one Google inferred.
        groups.append(swept)
    merged = food.merge(*groups)
    stats["merged"] = len(merged)
    return merged, stats


async def _enrich_food_phones(facilities: list[food.FoodFacility]) -> dict:
    """Look up the rows with no phone in Places -- and only those.

    This is the same billed step the Leads tab uses, under the same rules: a hard
    per-run ceiling, one lookup per row ever, and an address check before the
    number is trusted. Rows that already have a registry phone are not looked up at
    all, which is most of them: 85% of the merged set arrives with one."""
    needs = [f for f in facilities if not f.phone and f.name]
    if not needs:
        return {"attempted": 0, "matched": 0}
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        client: places.PlacesClient | None = None
        try:
            client = places.PlacesClient(http_client, places.service_account_token_provider())
            for f in needs:
                if client.blocked or client.budget_left <= 0:
                    break
                hit = await client.lookup(name=f.name, street=f.address, city=f.city, zip5=f.zip5)
                f.places_checked = True  # asked; a blank answer is still an answer
                if hit is None:
                    continue
                if hit.phone:
                    f.phone, f.phone_source = hit.phone, "google_places"
                f.website = hit.website or f.website
                f.business_status = hit.business_status or f.business_status
        except places.PlacesError as exc:
            if client is None:
                return {"attempted": 0, "matched": 0, "blocked": str(exc)}
            client.blocked, client.block_reason = True, str(exc)
    return {
        "attempted": client.calls,
        "matched": client.matched,
        "addressMismatch": client.rejected,
        "budgetLeft": client.budget_left,
        "blocked": client.block_reason,
    }


async def cmd_food(args: argparse.Namespace) -> int:
    today = date.today()
    sources = tuple(s.strip().lower() for s in args.sources.split(",") if s.strip())
    unknown = [s for s in sources if s not in FOOD_SOURCES]
    if unknown:
        raise SystemExit(f"unknown food source(s): {', '.join(unknown)}")

    facilities, stats = await _build_food_facilities(
        sources=sources, sweep=args.sweep, include_uncertain=args.sweep_include_uncertain
    )
    if args.limit:
        facilities = facilities[: args.limit]

    backend = sheet.open_backend() if args.destination == "sheet" else None
    enrichment = {"attempted": 0, "matched": 0}
    if not args.no_places:
        known = sheet.food_existing_keys(backend) if backend is not None else set()
        blank = sheet.food_keys_needing_phone(backend) if backend is not None else set()
        # A key already on the tab is only worth paying for if the sheet still shows
        # it with no phone; a brand-new key is always worth it.
        candidates = [f for f in facilities if f.key not in known or f.key in blank]
        enrichment = await _enrich_food_phones(candidates)

    if args.destination == "preview":
        for f in facilities[: args.top]:
            _print(_food_row_preview(f))
        _print({"summary": True, "destination": "preview", **stats, "places": enrichment,
                "withPhone": sum(1 for f in facilities if f.phone),
                "needsReview": sum(1 for f in facilities if f.needs_review)})
        return 0

    result = sheet.sync_food_facilities(backend, facilities, today=today, dry_run=args.dry_run)
    _print(
        {
            "summary": True,
            "destination": "sheet",
            "tab": sheet.FOOD_TAB,
            "dryRun": args.dry_run,
            **stats,
            "withPhone": sum(1 for f in facilities if f.phone),
            "needsReview": sum(1 for f in facilities if f.needs_review),
            "places": enrichment,
            "added": result.added,
            "enriched": result.enriched,
            "skippedDnc": result.skipped_dnc,
            "skippedCap": result.skipped_cap,
            "skippedNoChange": result.skipped_no_change,
            "errors": result.errors,
        }
    )
    return 1 if result.errors and result.added == 0 else 0


def _food_row_preview(f: food.FoodFacility) -> dict:
    return {
        "key": f.key,
        "name": f.name,
        "category": f.category,
        "phone": f.phone or None,
        "phoneSource": f.phone_source or None,
        "city": f.city,
        "distanceMi": round(f.distance_miles, 1) if f.distance_miles is not None else None,
        "distanceBasis": f.distance_basis,
        "sources": f.sources,
        "needsReview": f.needs_review or None,
        "reviewReason": f.review_reason or None,
    }


DEFAULT_APARTMENT_SWEEP_CALLS = 600


def apartment_sweep_ceiling() -> int:
    raw = os.environ.get("LEADS_APARTMENT_SWEEP_CALLS", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_APARTMENT_SWEEP_CALLS
    except ValueError:
        return DEFAULT_APARTMENT_SWEEP_CALLS
    return value if value >= 0 else DEFAULT_APARTMENT_SWEEP_CALLS


async def cmd_apartments(args: argparse.Namespace) -> int:
    """Sweep Google for apartment properties, then group the ones that share a
    website or a phone line into their managing companies.

    One pull, two tabs: `Apartments` (walk-in targets, biggest and staffed first)
    and `Property Managers` (companies holding two or more). Billed, like every
    Places call -- see LEADS_APARTMENT_SWEEP_CALLS."""
    today = date.today()
    async with httpx.AsyncClient(timeout=60.0) as http_client:
        client: places.PlacesClient | None = None
        try:
            client = places.PlacesClient(
                http_client, places.service_account_token_provider(),
                call_ceiling=apartment_sweep_ceiling(),
            )
            found = await client.sweep(
                list(apt.SWEEP_QUERIES), FOOD_RECT,
                typed_queries=list(apt.SWEEP_TYPED_QUERIES),
                field_mask=places.SWEEP_FIELD_MASK_HOURS,
            )
        except places.PlacesError as exc:
            _print({"error": str(exc)})
            return 2

    complexes = [c for c in (apt.from_place(r) for r in found.values()) if c and c.in_range]
    managers = apt.group_managers(complexes, min_properties=args.min_properties)

    summary: dict = {
        "summary": True,
        "swept": len(found),
        "properties": len(complexes),
        "withPhone": sum(1 for c in complexes if c.phone),
        "withLeasingOffice": sum(1 for c in complexes if c.open_days),
        "large": sum(1 for c in complexes if c.size_hint == "large"),
        "managers": len(managers),
        "managedProperties": sum(m.property_count for m in managers),
        "places": {"attempted": client.calls, "budgetLeft": client.budget_left,
                   "blocked": client.block_reason},
    }

    if args.destination == "preview":
        for c in sorted(complexes, key=lambda c: -c.review_count)[: args.top]:
            _print({"name": c.name, "phone": c.phone or None, "city": c.city,
                    "onSite": c.onsite_tier, "hours": c.hours or None,
                    "size": c.size_hint, "reviews": c.review_count,
                    "managedBy": c.manager or None,
                    "distanceMi": round(c.distance_miles, 1) if c.distance_miles is not None else None})
        for m in managers[: args.top]:
            _print({"manager": m.name, "properties": m.property_count, "phone": m.phone or None,
                    "withLeasingOffice": m.with_office, "cities": m.cities,
                    "groupedBy": m.basis, "national": m.is_national or None})
        _print({**summary, "destination": "preview"})
        return 0

    backend = sheet.open_backend()
    apts = sheet.sync_apartments(backend, complexes, today=today, dry_run=args.dry_run)
    mgrs = sheet.sync_managers(backend, managers, today=today, dry_run=args.dry_run)
    _print({**summary, "destination": "sheet", "dryRun": args.dry_run,
            "apartmentsTab": {"added": apts.added, "enriched": apts.enriched,
                              "skippedNoChange": apts.skipped_no_change,
                              "skippedDnc": apts.skipped_dnc, "skippedCap": apts.skipped_cap},
            "managersTab": {"added": mgrs.added, "enriched": mgrs.enriched,
                            "skippedNoChange": mgrs.skipped_no_change}})
    return 0


async def cmd_mark(args: argparse.Namespace) -> int:
    """Shade the institutional, medical and residential rows red on both tabs.

    Separate from `run` and `food` on purpose: it is a pass over what is already
    in the sheet, makes no network calls to any county or Google, and is safe to
    re-run at any time."""
    backend = sheet.open_backend()
    tabs = [sheet.LEADS_TAB, sheet.FOOD_TAB] if args.tab == "both" else [args.tab]
    for tab in tabs:
        _print(sheet.mark_institutions(backend, tab, dry_run=args.dry_run))
    return 0


def _add_common_pull_args(p: argparse.ArgumentParser, default_days: int) -> None:
    p.add_argument("--since-days", type=int, default=default_days, help="how far back to pull the county feed(s)")
    p.add_argument(
        "--counties", default=",".join(ALL_COUNTIES), help="comma-separated subset of sacramento,placer,yolo (default: all)"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fr-leads", description="Sacramento/Placer/Yolo commercial pest lead scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="pull, score, dedupe, and push into the destination")
    _add_common_pull_args(p_run, DEFAULT_LOOKBACK_DAYS)
    p_run.add_argument(
        "--destination", choices=("sheet", "fieldroutes"), default="sheet",
        help="sheet (default, plan section 5): the Google Sheet is the system of record for prospects. "
        "fieldroutes: push straight into FieldRoutes as a lead, the old phase-1 behaviour.",
    )
    p_run.add_argument("--dry-run", action="store_true", help="do every read but make zero writes to the destination")
    p_run.add_argument(
        "--no-places", action="store_true",
        help="skip Google Places phone enrichment (it is the only billed source in the pipeline)",
    )
    p_run.add_argument("--limit", type=int, default=None, help="only consider the top N ranked candidates")
    p_run.set_defaults(func=cmd_run)

    p_preview = sub.add_parser("preview", help="score without writing to either destination")
    _add_common_pull_args(p_preview, DEFAULT_BACKFILL_DAYS)
    p_preview.add_argument("--top", type=int, default=20)
    p_preview.add_argument("--no-fetch", action="store_true", help="skip PDF fetches; score vermin hits as unclassified")
    p_preview.set_defaults(func=cmd_preview)

    p_food = sub.add_parser(
        "food",
        help="pull the food-processing registries into the Food Facilities tab",
    )
    p_food.add_argument(
        "--destination", choices=("sheet", "preview"), default="sheet",
        help="sheet (default): write the Food Facilities tab. preview: print and write nothing.",
    )
    p_food.add_argument(
        "--sources", default=",".join(FOOD_SOURCES),
        help="comma-separated subset of " + ",".join(FOOD_SOURCES),
    )
    p_food.add_argument("--dry-run", action="store_true", help="do every read but make zero writes")
    p_food.add_argument(
        "--no-places", action="store_true",
        help="skip Google Places lookups for the rows no registry gave a phone (the only billed step)",
    )
    p_food.add_argument(
        "--sweep", action="store_true",
        help="also run Google's keyword sweep to discover facilities no registry lists "
        "(plan section 4). Many billed requests -- see LEADS_PLACES_SWEEP_CALLS.",
    )
    p_food.add_argument(
        "--sweep-include-uncertain", action="store_true",
        help="keep the sweep's unconfirmed rows too (~800 of them, needing triage) "
        "rather than only the ones Google's own type confirms as a processor",
    )
    p_food.add_argument("--limit", type=int, default=None, help="only consider the top N ranked facilities")
    p_food.add_argument("--top", type=int, default=25, help="rows to print in preview mode")
    p_food.set_defaults(func=cmd_food)

    p_apt = sub.add_parser(
        "apartments",
        help="sweep apartment complexes and group them into the companies managing two or more",
    )
    p_apt.add_argument("--destination", choices=("sheet", "preview"), default="sheet")
    p_apt.add_argument("--dry-run", action="store_true", help="do every read, write nothing")
    p_apt.add_argument(
        "--min-properties", type=int, default=2,
        help="how many properties a company needs before it counts as a manager (default 2)",
    )
    p_apt.add_argument("--top", type=int, default=25, help="rows to print in preview mode")
    p_apt.set_defaults(func=cmd_apartments)

    p_mark = sub.add_parser(
        "mark-institutions",
        help="shade nursing/care, hospital, apartment and campus rows red and record their class",
    )
    p_mark.add_argument(
        "--tab", default="both", choices=("both", sheet.LEADS_TAB, sheet.FOOD_TAB),
        help="which call list to mark (default: both)",
    )
    p_mark.add_argument("--dry-run", action="store_true", help="report what would be marked, write nothing")
    p_mark.set_defaults(func=cmd_mark)

    p_push = sub.add_parser("push", help="push one or more named facilities")
    p_push.add_argument("--facility", action="append", required=True, help="Facility_ID, e.g. FA0044262 (repeatable)")
    p_push.add_argument("--as-customer", type=int, default=None, help="validation mode: attach note+task to this existing customer instead of creating one")
    p_push.add_argument("--dry-run", action="store_true")
    p_push.set_defaults(func=cmd_push)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        code = asyncio.run(args.func(args))
    except (
        ag.FeedError,
        push.ConfigError,
        places.PlacesError,
        cdfa.CdfaError,
        calepa.CalEpaError,
        fsis.FsisError,
        FieldRoutesError,
        ToolError,
    ) as exc:
        _print({"error": str(exc)})
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
