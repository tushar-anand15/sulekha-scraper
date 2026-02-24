"""Command-line interface for Sulekha Data Extraction Service.

Provides commands for running the scraping pipeline, checking progress,
and managing the database.
"""

import click
import structlog

from sulekha.utils.logging import setup_logging

# Setup logging on module load
setup_logging()

logger = structlog.get_logger(__name__)


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Sulekha Data Extraction Service CLI."""
    pass


@main.command()
def init_db():
    """Initialize the database schema."""
    from sulekha.db.session import init_db as _init_db

    click.echo("Initializing database...")
    _init_db()
    click.echo("Database initialized successfully.")


@main.command()
@click.option("--batch-size", default=100, help="Number of items per batch")
def run_pipeline(batch_size: int):
    """Run the full scraping pipeline."""
    from sulekha.tasks.orchestrator import run_full_pipeline

    click.echo("Starting full pipeline...")
    result = run_full_pipeline.apply(args=[batch_size])
    click.echo(f"Pipeline result: {result.result}")


@main.command()
@click.argument("phase", type=int)
@click.option("--batch-size", default=100, help="Number of items per batch")
def run_phase(phase: int, batch_size: int):
    """Run a specific phase of the pipeline (1-4)."""
    from sulekha.tasks.orchestrator import run_phase as _run_phase

    if phase not in [1, 2, 3, 4]:
        click.echo("Phase must be 1, 2, 3, or 4")
        return

    click.echo(f"Running phase {phase}...")
    result = _run_phase.apply(args=[phase, batch_size])
    click.echo(f"Phase result: {result.result}")


@main.command()
@click.option("--detail", "-d", is_flag=True, help="Show year-wise breakdown")
def progress(detail: bool):
    """Show current pipeline progress."""
    from sulekha.tasks.orchestrator import get_progress

    result = get_progress.apply()
    progress_data = result.result

    click.echo("\n=== Sulekha Pipeline Progress ===\n")

    # Phase 1
    p1 = progress_data.get("phase1", {})
    click.echo(f"Phase 1: {p1.get('name', 'Discover Districts')}")
    click.echo(f"  Status: {'Complete' if p1.get('complete') else 'In Progress'}")
    click.echo(f"  Total district-year-type combinations: {p1.get('total', 0)}")
    click.echo()

    # Phase 2
    p2 = progress_data.get("phase2", {})
    click.echo(f"Phase 2: {p2.get('name', 'Discover Local Bodies')}")
    click.echo(f"  Status: {'Complete' if p2.get('complete') else 'In Progress'}")
    click.echo(f"  Done: {p2.get('done', 0)}/{p2.get('total', 0)} ({p2.get('percent', 0)}%)")
    if p2.get("by_year_district") and detail:
        click.echo("  By Year & District:")
        for year, districts in sorted(p2["by_year_district"].items(), reverse=True):
            year_total = sum(d["total"] for d in districts.values())
            year_done = sum(d["done"] for d in districts.values())
            year_pct = round(year_done / year_total * 100, 1) if year_total > 0 else 0
            click.echo(f"    {year}: {year_done}/{year_total} ({year_pct}%)")
            for district, data in sorted(districts.items()):
                pct = round(data["done"] / data["total"] * 100, 1) if data["total"] > 0 else 0
                click.echo(f"      {district}: {data['done']}/{data['total']} ({pct}%)")
    elif p2.get("by_year") and detail:
        click.echo("  By Year:")
        for year, data in sorted(p2["by_year"].items(), reverse=True):
            pct = round(data["done"] / data["total"] * 100, 1) if data["total"] > 0 else 0
            click.echo(f"    {year}: {data['done']}/{data['total']} ({pct}%)")
    click.echo()

    # Phase 3
    p3 = progress_data.get("phase3", {})
    click.echo(f"Phase 3: {p3.get('name', 'Scrape Projects')}")
    click.echo(f"  Status: {'Complete' if p3.get('complete') else 'In Progress'}")
    click.echo(f"  Local Bodies Done: {p3.get('done', 0)}/{p3.get('total', 0)} ({p3.get('percent', 0)}%)")
    if p3.get("status"):
        in_progress = p3["status"].get("IN_PROGRESS", 0)
        pending = p3["status"].get("PENDING", 0)
        if in_progress > 0 or pending > 0:
            click.echo(f"    IN_PROGRESS: {in_progress}, PENDING: {pending}")
    if p3.get("by_year_district") and detail:
        click.echo("  By Year & District:")
        for year, districts in sorted(p3["by_year_district"].items(), reverse=True):
            year_total = sum(d["total"] for d in districts.values())
            year_done = sum(d["done"] for d in districts.values())
            year_in_prog = sum(d.get("in_progress", 0) for d in districts.values())
            year_pct = round(year_done / year_total * 100, 1) if year_total > 0 else 0
            status_str = f" [in_prog:{year_in_prog}]" if year_in_prog > 0 else ""
            click.echo(f"    {year}: {year_done}/{year_total} ({year_pct}%){status_str}")
            for district, data in sorted(districts.items()):
                pct = round(data["done"] / data["total"] * 100, 1) if data["total"] > 0 else 0
                in_prog = data.get("in_progress", 0)
                status_str = f" [in_prog:{in_prog}]" if in_prog > 0 else ""
                click.echo(f"      {district}: {data['done']}/{data['total']} ({pct}%){status_str}")
    elif p3.get("by_year") and detail:
        click.echo("  By Year:")
        for year, data in sorted(p3["by_year"].items(), reverse=True):
            pct = round(data["done"] / data["total"] * 100, 1) if data["total"] > 0 else 0
            status_parts = []
            if data.get("in_progress", 0) > 0:
                status_parts.append(f"in_prog:{data['in_progress']}")
            if data.get("pending", 0) > 0:
                status_parts.append(f"pend:{data['pending']}")
            status_str = f" [{', '.join(status_parts)}]" if status_parts else ""
            click.echo(f"    {year}: {data['done']}/{data['total']} ({pct}%){status_str}")
    click.echo()

    # Phase 4
    p4 = progress_data.get("phase4", {})
    click.echo(f"Phase 4: {p4.get('name', 'Download PDFs')}")
    downloaded = p4.get('downloaded', 0)
    missing = p4.get('missing', 0)
    total = p4.get('total', 0)
    downloading = p4.get("status", {}).get("DOWNLOADING", 0)
    pending = p4.get("status", {}).get("PENDING", 0)
    error = p4.get("status", {}).get("ERROR", 0)
    
    click.echo(f"  Total Projects: {total:,}")
    click.echo(f"  Downloaded: {downloaded:,} | Missing: {missing:,} | Downloading: {downloading:,}")
    click.echo(f"  Pending: {pending:,} | Errors: {error:,}")
    click.echo(f"  Progress: {p4.get('percent', 0)}%")
    
    if p4.get("by_year_district") and detail:
        click.echo("  By Year & District:")
        for year, districts in sorted(p4["by_year_district"].items(), reverse=True):
            # Calculate year totals
            year_total = sum(d["total"] for d in districts.values())
            year_done = sum(d.get("downloaded", 0) + d.get("missing", 0) for d in districts.values())
            year_downloading = sum(d.get("downloading", 0) for d in districts.values())
            year_pct = round(year_done / year_total * 100, 1) if year_total > 0 else 0
            click.echo(f"    {year}: {year_done:,}/{year_total:,} ({year_pct}%) [ing:{year_downloading:,}]")
            
            for district, data in sorted(districts.items()):
                done = data.get("downloaded", 0) + data.get("missing", 0)
                pct = round(done / data["total"] * 100, 1) if data["total"] > 0 else 0
                click.echo(
                    f"      {district}: {done:,}/{data['total']:,} ({pct}%) "
                    f"[dl:{data.get('downloaded', 0):,}, ing:{data.get('downloading', 0):,}]"
                )
    elif p4.get("by_year") and detail:
        # Fallback to year-only view
        click.echo("  By Year:")
        for year, data in sorted(p4["by_year"].items(), reverse=True):
            done = data.get("downloaded", 0) + data.get("missing", 0)
            pct = round(done / data["total"] * 100, 1) if data["total"] > 0 else 0
            click.echo(
                f"    {year}: {done:,}/{data['total']:,} ({pct}%) "
                f"[dl:{data.get('downloaded', 0):,}, miss:{data.get('missing', 0):,}, "
                f"ing:{data.get('downloading', 0):,}, pend:{data.get('pending', 0):,}]"
            )
    click.echo()


@main.command()
def worker():
    """Start a Celery worker."""
    from sulekha.tasks.celery_app import celery_app

    click.echo("Starting Celery worker...")
    celery_app.worker_main(
        argv=[
            "worker",
            "--loglevel=INFO",
            "-Q",
            "default,discovery,scraper,pdf,orchestrator",
        ]
    )


@main.command()
def beat():
    """Start the Celery beat scheduler."""
    from sulekha.tasks.celery_app import celery_app

    click.echo("Starting Celery beat scheduler...")
    celery_app.worker_main(argv=["beat", "--loglevel=INFO"])


@main.command()
@click.option("--port", default=5555, help="Port to run Flower on")
def flower(port: int):
    """Start the Flower monitoring UI."""
    import subprocess

    click.echo(f"Starting Flower on port {port}...")
    subprocess.run(
        ["celery", "-A", "sulekha.tasks.celery_app", "flower", f"--port={port}"],
        check=True,
    )


@main.command()
def test_scraper():
    """Test the scraper client by loading the base page."""
    from sulekha.scraper.client import SulekhaClient

    click.echo("Testing scraper client...")

    with SulekhaClient() as client:
        client.load_base()

        years = client.get_year_options()
        lb_types = client.get_lb_type_options()

        click.echo(f"\nFound {len(years)} years:")
        for val, label in years[:5]:
            click.echo(f"  {val}: {label}")
        if len(years) > 5:
            click.echo(f"  ... and {len(years) - 5} more")

        click.echo(f"\nFound {len(lb_types)} LB types:")
        for val, label in lb_types:
            click.echo(f"  {val}: {label}")

        click.echo("\nScraper client is working correctly!")


# =============================================================================
# Phase-specific commands with queue management
# =============================================================================


@main.command("run-discovery")
@click.option("--skip-exists/--no-skip-exists", default=True, help="Skip if data already exists")
@click.option("--batch-size", default=100, help="Number of items per batch")
@click.option("--max-queue", default=1000, help="Maximum queue size before pausing")
def run_discovery_cmd(skip_exists: bool, batch_size: int, max_queue: int):
    """Run Phase 1: Discover all districts.

    This command discovers all Year x LB Type x District combinations
    from the Sulekha portal. If districts already exist in the database,
    the command will skip (use --no-skip-exists to force re-run).
    """
    from sulekha.tasks.runner import PhaseRunner

    click.echo("=" * 60)
    click.echo("  Phase 1: Discovery")
    click.echo("=" * 60)
    click.echo(f"  Skip if exists: {skip_exists}")
    click.echo(f"  Batch size: {batch_size}")
    click.echo(f"  Max queue size: {max_queue}")
    click.echo()

    runner = PhaseRunner(batch_size=batch_size, max_queue_size=max_queue)
    result = runner.run_discovery(skip_if_exists=skip_exists)

    click.echo()
    if result.skipped:
        click.secho(f"SKIPPED: {result.message}", fg="yellow")
    elif "ERROR" in result.message:
        click.secho(f"ERROR: {result.message}", fg="red")
    else:
        click.secho(f"SUCCESS: {result.message}", fg="green")

    click.echo()
    click.echo(f"  Total: {result.total}")
    click.echo(f"  Done: {result.done}")
    click.echo(f"  Pending: {result.pending}")
    click.echo(f"  Errors: {result.error}")
    click.echo()


@main.command("run-local-bodies")
@click.option("--skip-exists/--no-skip-exists", default=True, help="Skip if data already exists")
@click.option("--batch-size", default=100, help="Number of items per batch")
@click.option("--max-queue", default=1000, help="Maximum queue size before pausing")
def run_local_bodies_cmd(skip_exists: bool, batch_size: int, max_queue: int):
    """Run Phase 2: Discover local bodies for all districts.

    This command discovers all local bodies for each district.
    Requires Phase 1 (discovery) to be complete first.
    """
    from sulekha.tasks.runner import PhaseRunner

    click.echo("=" * 60)
    click.echo("  Phase 2: Local Bodies")
    click.echo("=" * 60)
    click.echo(f"  Skip if exists: {skip_exists}")
    click.echo(f"  Batch size: {batch_size}")
    click.echo(f"  Max queue size: {max_queue}")
    click.echo()

    runner = PhaseRunner(batch_size=batch_size, max_queue_size=max_queue)
    result = runner.run_local_bodies(skip_if_exists=skip_exists)

    click.echo()
    if result.skipped:
        click.secho(f"SKIPPED: {result.message}", fg="yellow")
    elif "ERROR" in result.message:
        click.secho(f"ERROR: {result.message}", fg="red")
    else:
        click.secho(f"SUCCESS: {result.message}", fg="green")

    click.echo()
    click.echo(f"  Total: {result.total}")
    click.echo(f"  Done: {result.done}")
    click.echo(f"  Pending: {result.pending}")
    click.echo(f"  Errors: {result.error}")
    click.echo()


@main.command("run-project-tables")
@click.option("--skip-exists/--no-skip-exists", default=True, help="Skip if data already exists")
@click.option("--batch-size", default=100, help="Number of items per batch")
@click.option("--max-queue", default=1000, help="Maximum queue size before pausing")
def run_project_tables_cmd(skip_exists: bool, batch_size: int, max_queue: int):
    """Run Phase 3: Scrape project tables for all local bodies.

    This command scrapes all projects from each local body's project table.
    Requires Phase 2 (local bodies) to be complete first.
    """
    from sulekha.tasks.runner import PhaseRunner

    click.echo("=" * 60)
    click.echo("  Phase 3: Project Tables")
    click.echo("=" * 60)
    click.echo(f"  Skip if exists: {skip_exists}")
    click.echo(f"  Batch size: {batch_size}")
    click.echo(f"  Max queue size: {max_queue}")
    click.echo()

    runner = PhaseRunner(batch_size=batch_size, max_queue_size=max_queue)
    result = runner.run_project_tables(skip_if_exists=skip_exists)

    click.echo()
    if result.skipped:
        click.secho(f"SKIPPED: {result.message}", fg="yellow")
    elif "ERROR" in result.message:
        click.secho(f"ERROR: {result.message}", fg="red")
    else:
        click.secho(f"SUCCESS: {result.message}", fg="green")

    click.echo()
    click.echo(f"  Total: {result.total}")
    click.echo(f"  Done: {result.done}")
    click.echo(f"  Pending: {result.pending}")
    click.echo(f"  Errors: {result.error}")
    click.echo()


@main.command("run-pdfs")
@click.option("--skip-exists/--no-skip-exists", default=True, help="Skip if data already exists")
@click.option("--batch-size", default=500, help="Number of items per batch")
@click.option("--max-queue", default=1000, help="Maximum queue size before pausing")
def run_pdfs_cmd(skip_exists: bool, batch_size: int, max_queue: int):
    """Run Phase 4: Download PDFs for all projects.

    This command downloads PDFs for each project and uploads them to storage.
    Requires Phase 3 (project tables) to be complete first.
    """
    from sulekha.tasks.runner import PhaseRunner

    click.echo("=" * 60)
    click.echo("  Phase 4: PDFs")
    click.echo("=" * 60)
    click.echo(f"  Skip if exists: {skip_exists}")
    click.echo(f"  Batch size: {batch_size}")
    click.echo(f"  Max queue size: {max_queue}")
    click.echo()

    runner = PhaseRunner(batch_size=batch_size, max_queue_size=max_queue)
    result = runner.run_pdfs(skip_if_exists=skip_exists)

    click.echo()
    if result.skipped:
        click.secho(f"SKIPPED: {result.message}", fg="yellow")
    elif "ERROR" in result.message:
        click.secho(f"ERROR: {result.message}", fg="red")
    else:
        click.secho(f"SUCCESS: {result.message}", fg="green")

    click.echo()
    click.echo(f"  Total: {result.total}")
    click.echo(f"  Done: {result.done}")
    click.echo(f"  Pending: {result.pending}")
    click.echo(f"  Errors: {result.error}")
    click.echo()


@main.command("run-all")
@click.option("--skip-exists/--no-skip-exists", default=True, help="Skip phases if data already exists")
@click.option("--batch-size", default=100, help="Number of items per batch")
@click.option("--max-queue", default=1000, help="Maximum queue size before pausing")
def run_all_cmd(skip_exists: bool, batch_size: int, max_queue: int):
    """Run all phases of the pipeline sequentially.

    This command runs all four phases in order:
    1. Discovery (districts)
    2. Local Bodies
    3. Project Tables
    4. PDFs

    Each phase will skip if data already exists (use --no-skip-exists to force).
    """
    from sulekha.tasks.runner import PhaseRunner

    click.echo("=" * 60)
    click.echo("  Running Full Pipeline")
    click.echo("=" * 60)
    click.echo(f"  Skip if exists: {skip_exists}")
    click.echo(f"  Batch size: {batch_size}")
    click.echo(f"  Max queue size: {max_queue}")
    click.echo()

    runner = PhaseRunner(batch_size=batch_size, max_queue_size=max_queue)
    results = runner.run_full_pipeline(skip_if_exists=skip_exists)

    click.echo()
    click.echo("=" * 60)
    click.echo("  Pipeline Summary")
    click.echo("=" * 60)
    
    for phase_key in ["phase1", "phase2", "phase3", "phase4"]:
        if phase_key in results:
            phase = results[phase_key]
            status = "SKIPPED" if phase.get("skipped") else "COMPLETE"
            click.echo(f"  {phase.get('name', phase_key)}: {status}")
            click.echo(f"    Total: {phase.get('total', 0)}, Done: {phase.get('done', 0)}")
    
    click.echo()


@main.command("reset-stuck-pdfs")
@click.option(
    "--older-than-minutes",
    default=30,
    help="Reset projects stuck in DOWNLOADING for longer than this (default: 30)",
)
@click.option("--all", "reset_all", is_flag=True, help="Reset ALL DOWNLOADING regardless of age")
@click.option("--dry-run", is_flag=True, help="Show what would be reset without changing")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation")
def reset_stuck_pdfs_cmd(older_than_minutes: int, reset_all: bool, dry_run: bool, yes: bool):
    """Reset projects stuck in DOWNLOADING back to PENDING for retry.

    Use when workers were restarted/crashed and left projects orphaned.
    """
    import os
    from sqlalchemy import create_engine, text

    engine = create_engine(os.environ.get("DATABASE_URL", "postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha"))
    if reset_all:
        where = "pdf_status = 'DOWNLOADING'"
        desc = "all"
    else:
        where = f"pdf_status = 'DOWNLOADING' AND (pdf_last_attempt_at IS NULL OR pdf_last_attempt_at < NOW() - INTERVAL '{older_than_minutes} minutes')"
        desc = f"older than {older_than_minutes} min"

    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT COUNT(*) FROM projects WHERE {where}"))
        count = result.scalar() or 0

    if count == 0:
        click.echo("No stuck projects found.")
        return

    click.echo(f"Found {count:,} projects stuck in DOWNLOADING ({desc})")
    if dry_run:
        click.echo("Dry run - no changes made. Run without --dry-run to reset.")
        return

    if not yes and not click.confirm("Reset these to PENDING for retry?"):
        return

    with engine.connect() as conn:
        result = conn.execute(text(f"UPDATE projects SET pdf_status = 'PENDING' WHERE {where}"))
        conn.commit()
        click.echo(f"Reset {result.rowcount:,} projects to PENDING. Run 'make pdfs' to resume.")


@main.command("queue-status")
def queue_status_cmd():
    """Show current Celery queue status."""
    from sulekha.tasks.scheduler import get_scheduler_status

    click.echo("=" * 60)
    click.echo("  Celery Queue Status")
    click.echo("=" * 60)

    status = get_scheduler_status()

    click.echo()
    click.echo("  Queues:")
    for queue, size in status["queues"].items():
        can_add = "YES" if status["can_enqueue"][queue] else "NO"
        click.echo(f"    {queue}: {size} tasks (can enqueue: {can_add})")

    click.echo()
    click.echo(f"  Total Queued: {status['total_queued']}")
    click.echo(f"  Active Tasks: {status['active_tasks']}")
    click.echo(f"  Max Queue Size: {status['max_queue_size']}")
    click.echo()


if __name__ == "__main__":
    main()
