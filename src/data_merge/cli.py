"""``data-merge`` command line.

Three verbs:

``years`` / ``paths``   describe the run that would happen
``validate``            build a cycle in memory and gate it, writing nothing
``build``               the same, then write outputs, reports and a manifest

``validate`` exists because the gate is the interesting part. Being able to ask
"does this still reproduce the known-good numbers?" without touching the output
directory makes the check cheap enough to run often.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import click

from data_merge import __version__
from data_merge.config import Paths, resolve_paths
from data_merge.io.csv_io import CsvIO
from data_merge.io.manifest import Manifest
from data_merge.schema import CandidateRow
from data_merge.spec import SPECS, YEARS, YearSpec
from data_merge.validate.expectations import check_year
from data_merge.validate.reports import data_quality_report


class _Built(Protocol):
    """What every year builder returns, regardless of its architecture."""

    candidates: tuple[CandidateRow, ...]
    wards: tuple[dict[str, str], ...]
    local_bodies: tuple[dict[str, str], ...]
    report: Any


def _builders() -> dict[int, Callable[..., _Built]]:
    """Resolve builders lazily so a half-built cycle cannot break ``--help``."""
    found: dict[int, Callable[..., _Built]] = {}
    for year in YEARS:
        try:
            module = __import__(f"data_merge.years.y{year}", fromlist=["build_year"])
        except ImportError:
            continue
        build_year = getattr(module, "build_year", None)
        if build_year is not None:
            found[year] = build_year
    return found


@click.group()
@click.version_option(version=__version__)
@click.option(
    "--root",
    type=click.Path(file_okay=False, path_type=str),
    default=None,
    help=(
        "Data root holding raw/, reference/ and final/. "
        "Defaults to DATA_MERGE_ROOT, then <repo>/data."
    ),
)
@click.pass_context
def main(ctx: click.Context, root: str | None) -> None:
    """Rebuild the Kerala local-body election dataset from raw sources, offline."""
    ctx.obj = resolve_paths(root)


@main.command("years")
@click.pass_obj
def years_cmd(paths: Paths) -> None:
    """List the cycles this package can build and what each declares."""
    available = _builders()
    for year in YEARS:
        spec = SPECS[year]
        click.echo(
            f"{year}  spine={spec.spine.value:<4} members={spec.members.value:<5} "
            f"front={spec.front.value:<9} pdf_sex={spec.pdf_sex.value:<7} "
            f"expect={spec.expect.candidates:,} candidates / "
            f"{spec.expect.wards:,} wards / {spec.expect.local_bodies:,} local bodies"
            f"{'' if year in available else '   [builder not implemented]'}"
        )


@main.command("paths")
@click.pass_obj
def paths_cmd(paths: Paths) -> None:
    """Show resolved data locations and flag anything missing."""
    click.echo(f"root       {paths.root}")
    for label, path in (
        ("raw", paths.raw),
        ("caches", paths.caches),
        ("sec_pdfs", paths.sec_pdfs),
        ("reference", paths.reference),
        ("final", paths.final),
    ):
        click.echo(f"{label:<10} {path}  {'ok' if path.is_dir() else 'MISSING'}")

    click.echo()
    missing_sources: list[str] = []
    for year in YEARS:
        spec = SPECS[year]
        for path in _declared_inputs(spec, paths):
            if not path.exists():
                missing_sources.append(f"{year}: {path}")
    if missing_sources:
        click.echo("missing declared inputs:")
        for line in missing_sources:
            click.echo(f"  {line}")
        sys.exit(1)
    click.echo("all declared inputs present")


def _declared_inputs(spec: YearSpec, paths: Paths) -> list[Path]:
    """Every file a cycle's spec names, resolved to a path."""
    found = [paths.sec_pdfs / spec.pdf]
    for cache in (spec.sec_cache, spec.member_cache, spec.contest_cache):
        if cache:
            found.append(paths.caches / cache)
    if spec.front_table:
        found.append(paths.reference / spec.front_table)
    return found


def _target_years(year: tuple[int, ...], every: bool) -> list[int]:
    available = sorted(_builders())
    if every:
        return available
    if not year:
        raise click.UsageError("give --year YYYY (repeatable) or --all")
    unknown = [y for y in year if y not in available]
    if unknown:
        raise click.UsageError(f"no builder for {unknown}; available: {available}")
    return sorted(set(year))


_year_option = click.option(
    "--year", "-y", type=int, multiple=True, help="Cycle to process; repeatable."
)
_all_option = click.option("--all", "every", is_flag=True, help="Every implemented cycle.")


@main.command("validate")
@_year_option
@_all_option
@click.pass_obj
def validate_cmd(paths: Paths, year: tuple[int, ...], every: bool) -> None:
    """Build in memory and check against expectations. Writes nothing."""
    failed = False
    for target in _target_years(year, every):
        built = _build_one(paths, target)
        checks = check_year(SPECS[target], built.candidates)
        click.echo(checks.summary())
        for failure in checks.failures:
            click.echo(f"  {failure}")
        failed = failed or not checks.ok
    if failed:
        sys.exit(1)


@main.command("build")
@_year_option
@_all_option
@click.option(
    "--skip-validation",
    is_flag=True,
    help="Write even if expectations fail. For diagnosis only -- never for data you intend to use.",
)
@click.pass_obj
def build_cmd(paths: Paths, year: tuple[int, ...], every: bool, skip_validation: bool) -> None:
    """Build cycles and write candidates, wards, local bodies, reports and a manifest."""
    for target in _target_years(year, every):
        spec = SPECS[target]
        built = _build_one(paths, target)

        checks = check_year(spec, built.candidates)
        click.echo(checks.summary())
        for failure in checks.failures:
            click.echo(f"  {failure}")
        if not checks.ok and not skip_validation:
            # Abort before writing. A half-written year on disk looks like a
            # successful run to everything downstream.
            raise click.ClickException(
                f"{target}: expectations failed; nothing written. "
                "Re-run with --skip-validation only to inspect the output."
            )

        written = _write_year(paths, spec, built, checks_text=_dq_text(spec, checks))
        for name, rows in written.items():
            click.echo(f"  wrote {name} ({rows:,} rows)" if rows else f"  wrote {name}")


def _dq_text(spec: YearSpec, checks: Any) -> str:
    return data_quality_report(spec, checks).render()


def _build_one(paths: Paths, year: int) -> _Built:
    builder = _builders()[year]
    return builder(paths, pdf_cache_dir=paths.root / "interim" / "pdf_text")


def _write_year(paths: Paths, spec: YearSpec, built: _Built, *, checks_text: str) -> dict[str, int]:
    """Write a cycle's outputs. The only place in the package that writes data."""
    io = CsvIO()
    manifest = Manifest(year=spec.year, version=__version__)
    for path in _declared_inputs(spec, paths):
        if path.exists():
            manifest.add_input(path)

    written: dict[str, int] = {}
    candidates_path = paths.candidates_csv(spec.year)
    written[candidates_path.name] = io.write_candidates(candidates_path, built.candidates)

    for side_rows, path in (
        (built.wards, paths.wards_csv(spec.year)),
        (built.local_bodies, paths.local_bodies_csv(spec.year)),
    ):
        if side_rows:
            written[path.name] = io.write(path, side_rows, list(side_rows[0].keys()))

    report_path = paths.year_dir(spec.year) / f"data_quality_report_{spec.year}.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(checks_text, encoding="utf-8")
    written[report_path.name] = 0

    for name, rows in written.items():
        manifest.add_output(name, rows)
    manifest.counts["wards"] = len(built.wards)
    manifest.counts["local_bodies"] = len(built.local_bodies)
    manifest.notes.extend(spec.expect.notes)
    manifest.write(paths.manifest_json(spec.year))
    written[paths.manifest_json(spec.year).name] = 0
    return written


if __name__ == "__main__":
    main()
