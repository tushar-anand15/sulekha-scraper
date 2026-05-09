"""Command-line interface for the SAKARMA meeting scraper.

Provides commands for operators to initialise the database, launch scrape
runs, monitor progress, inspect reconciliation results, and start workers.
"""

from __future__ import annotations

import csv
import io
import subprocess
import sys
from typing import Optional

import click

from sakarma.config import settings
from sakarma.db.repositories import (
    LBProgressRepository,
    LBRepository,
    ReconciliationRepository,
    ScrapeRunRepository,
)
from sakarma.db.session import get_session
from sakarma.utils.logging import setup_logging

# Setup structured logging on module load (mirrors sulekha/cli.py).
setup_logging()

SEP = "=" * 60

# ---------------------------------------------------------------------------
# Colour helpers
# ---------------------------------------------------------------------------

_STATUS_COLOUR = {
    "done": "green",
    "running": "yellow",
    "in_progress": "yellow",
    "failed": "red",
    "error": "red",
    "pending": "cyan",
    "matched": "green",
    "mismatch": "red",
    "missing_kpi": "yellow",
    "missing_manifest": "yellow",
}


def _colour(text: str, status: str) -> None:
    """Echo *text* in the colour associated with *status*."""
    click.secho(text, fg=_STATUS_COLOUR.get(status, "white"))


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------


@click.group()
@click.version_option(version="0.1.0")
def main() -> None:
    """SAKARMA meeting-scraper operator CLI."""


# ---------------------------------------------------------------------------
# init-db
# ---------------------------------------------------------------------------


@main.command("init-db")
def init_db() -> None:
    """Initialise (or migrate) the SAKARMA database schema via Alembic."""
    from alembic import command
    from alembic.config import Config

    click.echo(SEP)
    click.echo("  SAKARMA: init-db")
    click.echo(SEP)
    click.echo(f"  target: {settings.database_url}")
    click.echo()

    cfg = Config("alembic_sakarma.ini")
    cfg.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(cfg, "head")

    click.secho("  Done.", fg="green")
    click.echo()


# ---------------------------------------------------------------------------
# Shared option set for backfill / diff
# ---------------------------------------------------------------------------

_lb_option = click.option(
    "--lb",
    "lb_id",
    type=int,
    default=None,
    help="Scrape only this specific LB id.",
)
_district_option = click.option(
    "--district",
    "district_id",
    type=int,
    default=None,
    help="Scrape only LBs belonging to this district id.",
)
_dry_run_option = click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the planned LB list and exit 0 without creating a run.",
)


def _resolve_lbs(
    lb_id: Optional[int],
    district_id: Optional[int],
    session,
) -> list:
    """Return the list of LB objects that should be scraped."""
    repo = LBRepository(session)
    if lb_id is not None:
        lb = repo.get(lb_id)
        if lb is None:
            click.secho(
                f"  ERROR: LB id {lb_id} not found in the database.", fg="red"
            )
            sys.exit(1)
        return [lb]
    if district_id is not None:
        lbs = repo.list_by_district(district_id)
        if not lbs:
            click.secho(
                f"  ERROR: No LBs found for district id {district_id}.", fg="red"
            )
            sys.exit(1)
        return lbs
    return repo.list_all()


def _run_scrape(kind: str, lb_id: Optional[int], district_id: Optional[int], dry_run: bool) -> None:
    """Shared implementation for ``backfill`` and ``diff`` commands."""
    click.echo(SEP)
    click.echo(f"  SAKARMA: {kind}")
    click.echo(SEP)

    with get_session() as session:
        lbs = _resolve_lbs(lb_id, district_id, session)

        if dry_run:
            click.echo(f"  [dry-run] Would create a '{kind}' run for {len(lbs)} LB(s):")
            for lb in lbs:
                click.echo(f"    lb_id={lb.id}  district_id={lb.district_id}  name={lb.name_ml}")
            click.echo()
            click.secho("  Dry run complete — no rows created.", fg="yellow")
            click.echo()
            return

        # Create the scrape run.
        run_repo = ScrapeRunRepository(session)
        if kind == "backfill":
            run = run_repo.create_backfill()
        else:
            run = run_repo.create_diff()

        click.echo(f"  Created scrape_run id={run.id} kind={kind}")

        # Seed LBProgress rows so workers have a queue to consume.
        lb_ids = [lb.id for lb in lbs]
        LBProgressRepository(session).bulk_create(run.id, lb_ids)
        click.echo(f"  Seeded {len(lb_ids)} lb_progress rows.")

    # Enqueue tasks OUTSIDE the session context (session committed above).
    # Import here to avoid importing celery_app at module load time.
    from sakarma.tasks import discovery as discovery_mod
    from sakarma.tasks import orchestrator as orchestrator_mod

    click.echo("  Enqueuing discovery task…")
    discovery_mod.run.delay(run.id)

    click.echo(f"  Enqueuing {len(lb_ids)} scrape_lb tasks…")
    for _lb_id in lb_ids:
        orchestrator_mod.scrape_lb.delay(run.id, _lb_id)

    click.echo()
    click.secho(
        f"  Enqueued {len(lb_ids)} tasks for run {run.id}. "
        "Monitor progress with `sakarma status`.",
        fg="green",
    )
    click.echo()


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


@main.command("backfill")
@_lb_option
@_district_option
@_dry_run_option
def backfill(lb_id: Optional[int], district_id: Optional[int], dry_run: bool) -> None:
    """Enqueue a full backfill scrape run (captures everything from scratch)."""
    _run_scrape("backfill", lb_id, district_id, dry_run)


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


@main.command("diff")
@_lb_option
@_district_option
@_dry_run_option
def diff(lb_id: Optional[int], district_id: Optional[int], dry_run: bool) -> None:
    """Enqueue an incremental diff run (skips already-captured rows)."""
    _run_scrape("diff", lb_id, district_id, dry_run)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@main.command("status")
@click.option("--scrape-run", "scrape_run_id", type=int, default=None,
              help="Show status for a specific scrape run id.")
@click.option("--detail", is_flag=True, default=False,
              help="Show per-district breakdowns.")
@click.option("--reconciliation", "show_recon", is_flag=True, default=False,
              help="Append reconciliation row counts by status.")
def status_cmd(
    scrape_run_id: Optional[int],
    detail: bool,
    show_recon: bool,
) -> None:
    """Show scrape-run status and LB progress counts."""
    click.echo(SEP)
    click.echo("  SAKARMA: status")
    click.echo(SEP)

    with get_session() as session:
        run_repo = ScrapeRunRepository(session)

        if scrape_run_id is not None:
            run = run_repo.get(scrape_run_id)
            if run is None:
                click.secho(f"  ERROR: scrape_run id={scrape_run_id} not found.", fg="red")
                sys.exit(1)
            runs = [run]
        else:
            runs = run_repo.list_recent(limit=1)
            if not runs:
                click.echo("  No scrape runs found.")
                click.echo()
                return

        for run in runs:
            _print_run_status(
                run,
                session,
                detail=detail,
                show_recon=show_recon,
            )


def _print_run_status(run, session, *, detail: bool, show_recon: bool) -> None:
    """Print status block for one scrape run."""
    status_colour = _STATUS_COLOUR.get(run.status, "white")
    click.echo()
    click.secho(f"  Run id : {run.id}", bold=True)
    click.echo(f"  Kind   : {run.kind}")
    click.secho(f"  Status : {run.status}", fg=status_colour)
    click.echo(f"  Started: {run.started_at}")
    if run.completed_at:
        click.echo(f"  Done   : {run.completed_at}")

    progress_rows = LBProgressRepository(session).list_for_run(run.id)
    total = len(progress_rows)
    by_status: dict[str, int] = {}
    for p in progress_rows:
        by_status[p.status] = by_status.get(p.status, 0) + 1

    done = by_status.get("done", 0)
    in_progress = by_status.get("in_progress", 0)
    errored = by_status.get("error", 0)
    pending = by_status.get("pending", 0)
    pct = round(done / total * 100, 1) if total > 0 else 0.0

    click.echo()
    click.echo(f"  LBs  total      : {total}")
    click.secho(f"  LBs  done       : {done}  ({pct}%)", fg="green" if done else "white")
    click.secho(f"  LBs  in_progress: {in_progress}", fg="yellow" if in_progress else "white")
    click.secho(f"  LBs  error      : {errored}", fg="red" if errored else "white")
    click.secho(f"  LBs  pending    : {pending}", fg="cyan" if pending else "white")

    if detail and total > 0:
        # Group by district.
        lb_ids = [p.lb_id for p in progress_rows]
        lb_map = {lb.id: lb for lb in LBRepository(session).list_all() if lb.id in set(lb_ids)}
        prog_by_district: dict[int, dict[str, int]] = {}
        for p in progress_rows:
            lb = lb_map.get(p.lb_id)
            did = lb.district_id if lb else 0
            dist_stats = prog_by_district.setdefault(did, {})
            dist_stats[p.status] = dist_stats.get(p.status, 0) + 1
            dist_stats["_total"] = dist_stats.get("_total", 0) + 1

        click.echo()
        click.echo("  --- Per-district breakdown ---")
        for did, stats in sorted(prog_by_district.items()):
            dtotal = stats.get("_total", 0)
            ddone = stats.get("done", 0)
            dpct = round(ddone / dtotal * 100, 1) if dtotal > 0 else 0.0
            click.echo(
                f"    district={did}: {ddone}/{dtotal} done ({dpct}%)  "
                f"in_prog={stats.get('in_progress', 0)}  "
                f"err={stats.get('error', 0)}  "
                f"pend={stats.get('pending', 0)}"
            )

    if show_recon:
        recon_rows = ReconciliationRepository(session).list_for_run(run.id)
        recon_by_status: dict[str, int] = {}
        for r in recon_rows:
            recon_by_status[r.status] = recon_by_status.get(r.status, 0) + 1

        click.echo()
        click.echo("  --- Reconciliation summary ---")
        if not recon_rows:
            click.echo("    (no reconciliation rows yet)")
        else:
            for st, count in sorted(recon_by_status.items()):
                click.secho(f"    {st}: {count}", fg=_STATUS_COLOUR.get(st, "white"))

    click.echo()


# ---------------------------------------------------------------------------
# reconcile-report
# ---------------------------------------------------------------------------

_RECON_CSV_HEADER = [
    "scrape_run_id",
    "lb_id",
    "lb_name",
    "year_id",
    "main_group_value_id",
    "category",
    "dashboard_kpi_count",
    "manifest_row_count",
    "delta",
    "status",
    "computed_at",
]

_VALID_RECON_STATUSES = {"matched", "mismatch", "missing_kpi", "missing_manifest"}


@main.command("reconcile-report")
@click.option("--scrape-run", "scrape_run_id", type=int, default=None,
              help="Report for this specific scrape run (defaults to most recent).")
@click.option(
    "--status",
    "status_filter",
    type=click.Choice(["matched", "mismatch", "missing_kpi", "missing_manifest"]),
    default=None,
    help="Filter to rows with this reconciliation status.",
)
@click.option("--csv", "emit_csv", is_flag=True, default=False,
              help="Emit CSV to stdout instead of ASCII-block output.")
def reconcile_report(
    scrape_run_id: Optional[int],
    status_filter: Optional[str],
    emit_csv: bool,
) -> None:
    """Print or export reconciliation rows for a scrape run."""
    with get_session() as session:
        run_repo = ScrapeRunRepository(session)

        if scrape_run_id is not None:
            run = run_repo.get(scrape_run_id)
            if run is None:
                click.secho(f"ERROR: scrape_run id={scrape_run_id} not found.", fg="red")
                sys.exit(1)
        else:
            recent = run_repo.list_recent(limit=1)
            if not recent:
                click.echo("No scrape runs found.")
                return
            run = recent[0]

        rows = ReconciliationRepository(session).list_for_run(
            run.id, status_filter=status_filter
        )

        # Build lb_name lookup.
        lb_ids = {r.lb_id for r in rows}
        lb_map = {lb.id: lb for lb in LBRepository(session).list_all() if lb.id in lb_ids}

        if emit_csv:
            _emit_csv(rows, lb_map)
        else:
            _emit_ascii_table(run, rows, lb_map, status_filter)


def _emit_csv(rows: list, lb_map: dict) -> None:
    """Write reconciliation rows as CSV to stdout."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(_RECON_CSV_HEADER)
    for r in rows:
        lb = lb_map.get(r.lb_id)
        lb_name = lb.name_ml if lb else ""
        writer.writerow([
            r.scrape_run_id,
            r.lb_id,
            lb_name,
            r.year_id,
            r.main_group_value_id,
            r.category,
            r.dashboard_kpi_count,
            r.manifest_row_count,
            r.delta,
            r.status,
            r.computed_at.isoformat() if r.computed_at else "",
        ])
    click.echo(out.getvalue(), nl=False)


def _emit_ascii_table(run, rows: list, lb_map: dict, status_filter: Optional[str]) -> None:
    """Print reconciliation rows as an ASCII-block table grouped by status."""
    click.echo(SEP)
    click.echo(f"  SAKARMA: reconcile-report  (run={run.id}  kind={run.kind})")
    click.echo(SEP)

    if status_filter:
        click.echo(f"  Status filter: {status_filter}")

    if not rows:
        click.echo("  (no reconciliation rows)")
        click.echo()
        return

    # Group by status for display.
    by_status: dict[str, list] = {}
    for r in rows:
        by_status.setdefault(r.status, []).append(r)

    for st, st_rows in sorted(by_status.items()):
        click.echo()
        click.secho(
            f"  [{st.upper()}]  {len(st_rows)} row(s)",
            fg=_STATUS_COLOUR.get(st, "white"),
        )
        click.echo(
            f"  {'lb_id':>7}  {'lb_name':<30}  {'year':>6}  {'mg':>6}  "
            f"{'cat':>4}  {'kpi':>6}  {'mnf':>6}  {'delta':>6}"
        )
        click.echo("  " + "-" * 76)
        for r in st_rows:
            lb = lb_map.get(r.lb_id)
            lb_name = (lb.name_ml[:28] + "..") if lb and len(lb.name_ml) > 30 else (lb.name_ml if lb else "")
            click.echo(
                f"  {r.lb_id:>7}  {lb_name:<30}  {r.year_id:>6}  "
                f"{r.main_group_value_id:>6}  {r.category:>4}  "
                f"{r.dashboard_kpi_count:>6}  {r.manifest_row_count:>6}  "
                f"{r.delta:>+6}"
            )

    click.echo()
    click.echo(f"  Total rows: {len(rows)}")
    click.echo()


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


@main.command("worker")
def worker() -> None:
    """Start a SAKARMA Celery worker consuming all SAKARMA queues."""
    click.echo(SEP)
    click.echo("  SAKARMA: worker")
    click.echo(SEP)
    click.echo("  Starting Celery worker…")
    click.echo()
    subprocess.run(
        [
            "celery",
            "-A",
            "sakarma.tasks.celery_app",
            "worker",
            "--loglevel=INFO",
            "-Q",
            "sakarma_orchestrate,sakarma_manifest,sakarma_fetch,"
            "sakarma_reconcile,sakarma_discovery,sakarma_default",
            "--concurrency",
            str(settings.celery_worker_concurrency),
        ],
        check=False,
    )


# ---------------------------------------------------------------------------
# flower
# ---------------------------------------------------------------------------


@main.command("flower")
def flower() -> None:
    """Start the Flower task-monitoring UI on port 5556."""
    click.echo(SEP)
    click.echo("  SAKARMA: flower")
    click.echo(SEP)
    click.echo("  Starting Flower on http://localhost:5556 …")
    click.echo()
    subprocess.run(
        [
            "celery",
            "-A",
            "sakarma.tasks.celery_app",
            "flower",
            "--port=5556",
            "--broker=" + settings.redis_url,
        ],
        check=False,
    )


# ---------------------------------------------------------------------------
# Entrypoint guard
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
