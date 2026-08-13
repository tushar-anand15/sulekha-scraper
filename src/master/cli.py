"""``master`` command line.

Two verbs, the same split ``data-merge`` and ``geo`` already use:

``validate``   resolve the crosswalk against the live sources and gate it,
               creating no table and writing no file
``build``      load the sources, resolve, gate, then write the derived schema,
               a quality report and a manifest

Both verbs run the same gates, from ``master.validate``, against the same source
counts. A validate that passes and a build that fails would therefore be a bug in
one of two callers rather than two disagreeing definitions of "resolved".

``validate`` exists because the gate is the interesting part. Being able to ask
"does every Sakarma body and every Sulekha year-row still resolve to a local
body?" without a 25-minute rebuild makes the check cheap enough to run often.

Restoring the two Postgres dumps is not a verb here. Those are one-time restores
of files fetched from GCS, documented in ``docs/master_db_runbook.md``; wrapping
them in Python would imply this command can reproduce them, which it cannot.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from master import __version__
from master.config import Paths, database_url, resolve_paths
from master.crosswalk import CrosswalkResult
from master.validate import Quality

SCHEMA_SQL = Path(__file__).with_name("schema.sql")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="master")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=None,
    help="Repo root holding data/. Defaults to the checkout this package lives in.",
)
@click.option(
    "--database-url",
    "url",
    default=None,
    help="Master database. Overrides MASTER_DATABASE_URL.",
)
@click.pass_context
def main(ctx: click.Context, root: Path | None, url: str | None) -> None:
    """Build the Gram Sambandh master database from the dumps and files on disk."""
    ctx.obj = (resolve_paths(root), url or database_url())


def _report(result: CrosswalkResult, *, per_year: bool) -> None:
    for label, value in result.counts.items():
        click.echo(f"  {label:34} {value:>7,}")
    click.echo(f"  {'sakarma match methods':34} {dict(result.methods)}")
    click.echo(f"  {'sulekha match methods':34} {dict(result.year_methods)}")
    if per_year:
        for year, matched, unmatched in result.per_year:
            click.echo(f"    {year:<10} {matched:>6,} matched  {unmatched:>4,} unmatched")


def _gate(quality: Quality) -> Quality:
    """Print every gate, and stop the run if any failed.

    Called before anything is written, never after. Both verbs run the same
    gates against the same sources, so a validate that passes and a build that
    fails would mean a bug rather than two different standards.
    """
    for check in quality.gates:
        click.echo(check.render(), err=not check.ok)
    failures = quality.failures()
    if failures:
        click.echo()
        for problem in failures:
            click.echo(f"  FAIL {problem}", err=True)
        raise SystemExit(1)
    click.echo("\n  gates passed")
    return quality


@main.command()
@click.option("--per-year", is_flag=True, help="Break the Sulekha match down by financial year.")
@click.option("--full-report", is_flag=True, help="Print the quality report instead of writing it.")
@click.pass_obj
def validate(obj: tuple[Paths, str], per_year: bool, full_report: bool) -> None:
    """Resolve the crosswalk and gate it. Writes nothing, creates nothing."""
    paths, url = obj
    from master.crosswalk import plan
    from master.db import connect
    from master.validate import gate

    click.echo(f"validating against {url}")
    with connect(url) as db:
        result = plan(db, paths)
        _report(result, per_year=per_year)
        quality = _gate(gate(db, paths, result=result))
    if full_report:
        click.echo()
        click.echo(quality.render())


@main.command()
@click.option("--skip-load", is_flag=True, help="Reuse the src_* schemas already loaded.")
@click.option("--per-year", is_flag=True, help="Break the Sulekha match down by financial year.")
@click.pass_obj
def build(obj: tuple[Paths, str], skip_load: bool, per_year: bool) -> None:
    """Load the sources, gate the crosswalk, then write the derived schema."""
    paths, url = obj
    from master.crosswalk import prepare, write_matches
    from master.db import connect
    from master.load import elections, geo
    from master.validate import gate
    from master.validate import write as write_reports

    click.echo(f"building into {url}")
    with connect(url) as db:
        if not skip_load:
            for label, count in elections.load(db, paths).items():
                click.echo(f"  src_elections.{label:<24} {count:>9,} rows")
            for label, count in geo.load(db, paths).items():
                click.echo(f"  src_geo.{label:<30} {count:>9,} rows")

        # The spine is written first because it is a faithful copy of a source.
        # Everything after this line is a judgement, and none of it -- not a
        # match, not the derived schema, not the report -- is written until
        # every gate has passed.
        result = prepare(db, paths)
        _report(result, per_year=per_year)
        quality = _gate(gate(db, paths, result=result))
        write_matches(db, result)

        click.echo(f"  applying {SCHEMA_SQL.name}")
        db.run_script(SCHEMA_SQL)
        for table in (
            "finance.project",
            "finance.lb_year_summary",
            "meetings.meeting",
            "meetings.artifact",
            "elections.candidate",
            "core.lb_coverage",
        ):
            click.echo(f"  {table:<28} {db.scalar(f'SELECT count(*) FROM {table}'):>9,} rows")

        for path in write_reports(quality, paths):
            click.echo(f"  wrote {path.relative_to(paths.root)}")


if __name__ == "__main__":
    sys.exit(main())
