"""`fr-leads`: the lead scraper's command line entry point.

Subcommands:
  run       Full pipeline: pull the county feed, score, dedupe, push into
            FieldRoutes. This is what the Railway cron service runs.
  preview   Same pull and scoring, no FieldRoutes access at all (not even
            reads) -- a quick look at what a run would find.
  push      Push one or more named facilities. `--as-customer` is the
            validation mode: writes a note + task to an existing (allowlisted)
            customer instead of creating a new one, per
            docs/lead-scraper-plan.md's validation checklist.

Every subcommand prints one JSON line per candidate decision, then a final
JSON summary line, to stdout -- easy to grep, easy for Railway's log drain.
Nothing here prints the FieldRoutes key/token (client.py already strips
`params` from every response before it reaches this code).
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

from . import arcgis as ag
from . import fr_push as push
from . import pipeline as pl
from .reports import PoliteFetcher

DEFAULT_LOOKBACK_DAYS = 45
DEFAULT_BACKFILL_DAYS = 180
def _cache_dir() -> Path:
    return Path(os.environ.get("LEADS_PDF_CACHE_DIR", "./leads_cache/pdf"))


def _print(obj: object) -> None:
    print(server._j(obj))


def _candidate_row(c: pl.LeadCandidate) -> dict:
    return {
        "facilityId": c.facility_id,
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
    facilities: dict[str, ag.Facility], *, today: date, fetch_pdfs: bool
) -> list[pl.LeadCandidate]:
    if not fetch_pdfs:
        return pl.rank(await pl.build_candidates(facilities, today=today, fetcher=None))
    async with httpx.AsyncClient(timeout=30.0) as http_client:
        fetcher = PoliteFetcher(_cache_dir(), http_client)
        candidates = await pl.build_candidates(facilities, today=today, fetcher=fetcher)
    return pl.rank(candidates)


async def cmd_preview(args: argparse.Namespace) -> int:
    today = date.today()
    facilities = await _pull_facilities(since_days=args.since_days, today=today)
    candidates = await _build(facilities, today=today, fetch_pdfs=not args.no_fetch)
    shown = [c for c in candidates if c.lane != pl.LANE_CHAIN][: args.top]
    for c in shown:
        _print(_candidate_row(c))
    _print(
        {
            "summary": True,
            "totalCandidates": len(candidates),
            "event": sum(1 for c in candidates if c.lane == pl.LANE_EVENT),
            "territory": sum(1 for c in candidates if c.lane == pl.LANE_TERRITORY),
            "parkedChains": sum(1 for c in candidates if c.lane == pl.LANE_CHAIN),
            "shown": len(shown),
        }
    )
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    today = date.today()
    facilities = await _pull_facilities(since_days=args.since_days, today=today)
    candidates = await _build(facilities, today=today, fetch_pdfs=True)
    if args.limit:
        candidates = candidates[: args.limit]
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


def _add_common_pull_args(p: argparse.ArgumentParser, default_days: int) -> None:
    p.add_argument("--since-days", type=int, default=default_days, help="how far back to pull the county feed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fr-leads", description="Sacramento County inspection lead scraper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="pull, score, dedupe, and push into FieldRoutes")
    _add_common_pull_args(p_run, DEFAULT_LOOKBACK_DAYS)
    p_run.add_argument("--dry-run", action="store_true", help="do every read but make zero FieldRoutes writes")
    p_run.add_argument("--limit", type=int, default=None, help="only consider the top N ranked candidates")
    p_run.set_defaults(func=cmd_run)

    p_preview = sub.add_parser("preview", help="score without touching FieldRoutes at all")
    _add_common_pull_args(p_preview, DEFAULT_BACKFILL_DAYS)
    p_preview.add_argument("--top", type=int, default=20)
    p_preview.add_argument("--no-fetch", action="store_true", help="skip PDF fetches; score vermin hits as unclassified")
    p_preview.set_defaults(func=cmd_preview)

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
    except (ag.FeedError, push.ConfigError, FieldRoutesError, ToolError) as exc:
        _print({"error": str(exc)})
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
