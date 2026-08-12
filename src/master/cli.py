"""``master`` command line.

Two verbs, the same split ``data-merge`` and ``geo`` already use:

``validate``   resolve the crosswalk against the live sources and gate it,
               creating no table and writing no file
``build``      load the sources, resolve, gate, then write the derived schema

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


def _report(result: CrosswalkResult, *, per_year: bool) -> list[str]:
    for label, value in result.counts.items():
        click.echo(f"  {label:34} {value:>7,}")
    click.echo(f"  {'sakarma match methods':34} {dict(result.methods)}")
    click.echo(f"  {'sulekha match methods':34} {dict(result.year_methods)}")
    if per_year:
        for year, matched, unmatched in result.per_year:
            click.echo(f"    {year:<10} {matched:>6,} matched  {unmatched:>4,} unmatched")
    return result.gate()


def _gate(problems: list[str]) -> None:
    if problems:
        click.echo()
        for problem in problems:
            click.echo(f"  FAIL {problem}", err=True)
        raise SystemExit(1)
    click.echo("\n  gates passed")


@main.command()
@click.option("--per-year", is_flag=True, help="Break the Sulekha match down by financial year.")
@click.pass_obj
def validate(obj: tuple[Paths, str], per_year: bool) -> None:
    """Resolve the crosswalk and gate it. Writes nothing, creates nothing."""
    paths, url = obj
    from master.crosswalk import validate as resolve_only
    from master.db import connect

    click.echo(f"validating against {url}")
    with connect(url) as db:
        _gate(_report(resolve_only(db, paths), per_year=per_year))


@main.command()
@click.option("--skip-load", is_flag=True, help="Reuse the src_* schemas already loaded.")
@click.option("--per-year", is_flag=True, help="Break the Sulekha match down by financial year.")
@click.pass_obj
def build(obj: tuple[Paths, str], skip_load: bool, per_year: bool) -> None:
    """Load the sources, gate the crosswalk, then write the derived schema."""
    paths, url = obj
    from master.crosswalk import build as build_crosswalk
    from master.db import connect
    from master.load import elections, geo

    click.echo(f"building into {url}")
    with connect(url) as db:
        if not skip_load:
            for label, count in elections.load(db, paths).items():
                click.echo(f"  src_elections.{label:<24} {count:>9,} rows")
            for label, count in geo.load(db, paths).items():
                click.echo(f"  src_geo.{label:<30} {count:>9,} rows")

        # The gate sits inside build_crosswalk: the spine is a faithful copy of
        # a source and is written first, but no match is written until every
        # Sakarma body and Sulekha year-row has resolved.
        result = build_crosswalk(db, paths)
        _gate(_report(result, per_year=per_year))

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


if __name__ == "__main__":
    sys.exit(main())
