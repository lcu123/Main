"""
QUO VOIP Connector CLI

Usage:
    quo-voip [OPTIONS] COMMAND [ARGS]...

Commands:
    transcript get     Fetch a transcription by ID
    transcript call    Fetch transcript for a call
    transcript list    List transcriptions
    transcript search  Search transcriptions
    transcript export  Export to JSON / CSV / TXT
    transcript wait    Wait for a call transcript to complete
    calls list         List call records
    webhook-server     Start the webhook listener
    mcp-server         Start the MCP stdio server for Claude
    config check       Validate current configuration
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from typing import Optional

import click

from quo_voip import QUOConfig, TranscriptionService
from quo_voip.models import TranscriptionStatus


def _load_config(**overrides) -> QUOConfig:
    """Build config from env + CLI overrides."""
    kwargs = {k: v for k, v in overrides.items() if v is not None}
    return QUOConfig(**kwargs)


def _print_json(obj):
    click.echo(json.dumps(obj, indent=2, default=str))


# ── Root group ────────────────────────────────────────────────────────────────

@click.group()
@click.option("--api-key", envvar="QUO_API_KEY", help="QUO VOIP API key")
@click.option("--base-url", envvar="QUO_BASE_URL", help="QUO VOIP API base URL")
@click.option("--account-id", envvar="QUO_ACCOUNT_ID", help="QUO account ID")
@click.option("--debug/--no-debug", default=False, help="Enable debug logging")
@click.pass_context
def cli(ctx, api_key, base_url, account_id, debug):
    """QUO VOIP Connector – pull transcription data and integrate with Claude."""
    import logging
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    ctx.ensure_object(dict)
    ctx.obj["config"] = _load_config(
        api_key=api_key,
        base_url=base_url,
        account_id=account_id,
    )


# ── transcript group ──────────────────────────────────────────────────────────

@cli.group()
def transcript():
    """Commands for managing call transcriptions."""


@transcript.command("get")
@click.argument("transcription_id")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text")
@click.option("--no-timestamps", is_flag=True, default=False)
@click.option("--no-speakers", is_flag=True, default=False)
@click.pass_context
def transcript_get(ctx, transcription_id, fmt, no_timestamps, no_speakers):
    """Fetch a transcription by its ID."""
    svc = TranscriptionService(ctx.obj["config"])
    try:
        tx = svc.get_transcript(transcription_id)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if fmt == "json":
        _print_json(tx.to_dict())
    else:
        click.echo(svc.export_text(
            tx,
            include_timestamps=not no_timestamps,
            include_speakers=not no_speakers,
        ))


@transcript.command("call")
@click.argument("call_id")
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text")
@click.pass_context
def transcript_call(ctx, call_id, fmt):
    """Fetch the transcription for a specific call."""
    svc = TranscriptionService(ctx.obj["config"])
    try:
        tx = svc.get_transcript(call_id)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if fmt == "json":
        _print_json(tx.to_dict())
    else:
        click.echo(svc.export_text(tx))


@transcript.command("list")
@click.option("--participant", required=True, help="E.164 phone number to filter calls by")
@click.option("--max-results", default=20, show_default=True)
@click.option("--from", "from_date", help="ISO 8601 start date")
@click.option("--to", "to_date", help="ISO 8601 end date")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table")
@click.pass_context
def transcript_list(ctx, participant, max_results, from_date, to_date, fmt):
    """List calls and their transcripts for a participant."""
    svc = TranscriptionService(ctx.obj["config"])
    try:
        result = svc.list_calls(
            participants=[participant],
            max_results=max_results,
            created_after=datetime.fromisoformat(from_date) if from_date else None,
            created_before=datetime.fromisoformat(to_date) if to_date else None,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if fmt == "json":
        _print_json([c.to_dict() for c in result.items])
    else:
        _print_table(
            headers=["Call ID", "Direction", "Status", "Duration", "Created"],
            rows=[
                [
                    c.id,
                    c.direction.value,
                    c.status.value,
                    f"{c.duration or '?'}s",
                    c.created_at.strftime("%Y-%m-%d %H:%M"),
                ]
                for c in result.items
            ],
        )
        click.echo(f"\n{len(result.items)} of {result.total_items} calls | More: {result.has_more}")


@transcript.command("search")
@click.argument("query")
@click.option("--from", "from_date", help="ISO 8601 start date")
@click.option("--to", "to_date", help="ISO 8601 end date")
@click.option("--page", default=1)
@click.option("--page-size", default=20)
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table")
@click.pass_context
def transcript_search(ctx, query, from_date, to_date, page, page_size, fmt):
    """Search transcriptions for text content."""
    click.echo("Error: full-text search is not supported by the OpenPhone API.", err=True)
    click.echo("Use 'transcript get <call_id>' to fetch a specific transcript.", err=True)
    sys.exit(1)


@transcript.command("export")
@click.argument("transcription_ids", nargs=-1, required=True)
@click.option("--format", "fmt", type=click.Choice(["json", "csv", "text"]), default="text")
@click.option("--output", "-o", type=click.Path(), help="Output file path (default: stdout)")
@click.pass_context
def transcript_export(ctx, transcription_ids, fmt, output):
    """Export one or more transcriptions to JSON, CSV, or plain text."""
    svc = TranscriptionService(ctx.obj["config"])
    transcriptions = []
    for tid in transcription_ids:
        try:
            transcriptions.append(svc.get_transcript(tid))
        except Exception as exc:
            click.echo(f"Warning: could not fetch {tid}: {exc}", err=True)

    if not transcriptions:
        click.echo("No transcriptions retrieved.", err=True)
        sys.exit(1)

    if fmt == "json":
        content = json.dumps([t.to_dict() for t in transcriptions], indent=2, default=str)
    elif fmt == "csv":
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["call_id", "status", "duration", "created_at", "text"])
        for t in transcriptions:
            writer.writerow([t.call_id, t.status.value, t.duration,
                             t.created_at.isoformat(), t.full_text or ""])
        content = buf.getvalue()
    else:
        content = "\n\n".join(svc.export_text(t) for t in transcriptions)

    if output:
        with open(output, "w", encoding="utf-8") as f:
            f.write(content)
        click.echo(f"Exported {len(transcriptions)} transcription(s) to {output}")
    else:
        click.echo(content)


@transcript.command("wait")
@click.argument("call_id")
@click.option("--timeout", default=300, show_default=True, help="Timeout in seconds")
@click.option("--poll-interval", default=5, show_default=True)
@click.option("--format", "fmt", type=click.Choice(["json", "text"]), default="text")
@click.pass_context
def transcript_wait(ctx, call_id, timeout, poll_interval, fmt):
    """Wait for a call transcription to complete, then print it."""
    svc = TranscriptionService(ctx.obj["config"])
    click.echo(f"Waiting for transcription of call {call_id}...", err=True)

    def on_status(status):
        click.echo(f"  → Status: {status.value}", err=True)

    try:
        tx = svc.wait_for_transcript(
            call_id,
            poll_interval=float(poll_interval),
            timeout=float(timeout),
            on_status_change=on_status,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if fmt == "json":
        _print_json(tx.to_dict())
    else:
        click.echo(svc.export_text(tx))


# ── calls group ───────────────────────────────────────────────────────────────

@cli.group()
def calls():
    """Commands for browsing call records."""


@calls.command("list")
@click.option("--participant", required=True, help="E.164 phone number to filter calls by")
@click.option("--max-results", default=20, show_default=True)
@click.option("--from", "from_date", help="ISO 8601 start date")
@click.option("--to", "to_date", help="ISO 8601 end date")
@click.option("--format", "fmt", type=click.Choice(["json", "table"]), default="table")
@click.pass_context
def calls_list(ctx, participant, max_results, from_date, to_date, fmt):
    """List call records."""
    svc = TranscriptionService(ctx.obj["config"])
    try:
        result = svc.list_calls(
            participants=[participant],
            max_results=max_results,
            created_after=datetime.fromisoformat(from_date) if from_date else None,
            created_before=datetime.fromisoformat(to_date) if to_date else None,
        )
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if fmt == "json":
        _print_json([c.to_dict() for c in result.items])
    else:
        _print_table(
            headers=["ID", "Direction", "Status", "Participants", "Duration", "Date"],
            rows=[
                [
                    c.id,
                    c.direction.value,
                    c.status.value,
                    ", ".join(c.participants),
                    f"{c.duration or '?'}s",
                    c.created_at.strftime("%Y-%m-%d %H:%M"),
                ]
                for c in result.items
            ],
        )
        click.echo(f"\n{len(result.items)} of {result.total_items} calls | More: {result.has_more}")


# ── standalone servers ────────────────────────────────────────────────────────

@cli.command("mcp-server")
@click.pass_context
def mcp_server(ctx):
    """Start the MCP stdio server for Claude integration."""
    from quo_mcp.server import run_mcp_server
    run_mcp_server(ctx.obj["config"])


@cli.command("webhook-server")
@click.option("--host", default="0.0.0.0", show_default=True)
@click.option("--port", default=8080, show_default=True)
@click.pass_context
def webhook_server(ctx, host, port):
    """Start the webhook listener server."""
    from webhooks.server import WebhookHandler, EVENT_TRANSCRIPTION_COMPLETED

    handler = WebhookHandler(ctx.obj["config"])

    @handler.on(EVENT_TRANSCRIPTION_COMPLETED)
    def on_transcript(event, transcription=None):
        if transcription:
            click.echo(f"\n[Webhook] Transcription ready for call {transcription.call_id}:")
            click.echo(transcription.render_transcript())
        else:
            click.echo(f"[Webhook] {event}")

    handler.serve(host=host, port=port)


@cli.command("config-check")
@click.pass_context
def config_check(ctx):
    """Validate the current configuration."""
    config = ctx.obj["config"]
    errors = config.validate()
    if errors:
        click.echo("Configuration errors:", err=True)
        for e in errors:
            click.echo(f"  ✗ {e}", err=True)
        sys.exit(1)
    else:
        click.echo(f"Configuration OK:\n  {config}")


# ── helpers ───────────────────────────────────────────────────────────────────

def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))

    def fmt_row(cells):
        return "  ".join(str(c).ljust(col_widths[i]) for i, c in enumerate(cells))

    click.echo(fmt_row(headers))
    click.echo("  ".join("─" * w for w in col_widths))
    for row in rows:
        click.echo(fmt_row(row))


def main():
    cli(obj={})


if __name__ == "__main__":
    main()
