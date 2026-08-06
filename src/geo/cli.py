"""``geo`` command line.

Four verbs, split along the same seam as the package:

``paths``      describe where a run would read and write
``fetch``      the only verb permitted to touch the network
``validate``   build in memory and gate it, writing nothing
``build``      the same, then write layers and a manifest

``validate`` exists for the same reason it does in ``data-merge``: the gate is the
interesting part, and being able to ask "does the crosswalk still resolve every local
body?" without touching the output directory makes it cheap enough to run often.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from geo import __version__
from geo.config import Paths, resolve_paths


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="geo")
@click.option(
    "--root",
    type=click.Path(path_type=Path),
    default=None,
    help="Data root to read and write. Overrides GEO_ROOT.",
)
@click.pass_context
def main(ctx: click.Context, root: Path | None) -> None:
    """Boundary geometry for the Kerala LSG election dataset."""
    ctx.obj = resolve_paths(root)


@main.command()
@click.pass_obj
def paths(cfg: Paths) -> None:
    """Show where this run would read and write."""
    rows = [
        ("root", cfg.root),
        ("tiles (cache)", cfg.tiles),
        ("releases (cache)", cfg.releases),
        ("reference (committed)", cfg.reference),
        ("elections (read-only)", cfg.elections),
        ("final (output)", cfg.final),
    ]
    width = max(len(label) for label, _ in rows)
    for label, path in rows:
        mark = " " if path.exists() else "?"
        click.echo(f"{mark} {label:<{width}}  {path}")


@main.command()
@click.pass_obj
def fetch(cfg: Paths) -> None:
    """Populate the tile and release caches. The only verb that uses the network."""
    raise click.ClickException("not implemented yet -- see Units 3 and 7 of the plan")


def _today() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).date().isoformat()


def _report(result, *, wrote: bool) -> None:
    for key, value in result.counts.items():
        click.echo(f"  {key:34} {value:>7,}")
    if wrote:
        for path in result.written:
            size = path.stat().st_size / 1e6
            click.echo(f"  wrote {path.name:34} {size:>7.2f} MB")
    if result.problems:
        click.echo()
        for problem in result.problems:
            click.echo(f"  FAIL {problem}", err=True)
        raise SystemExit(1)
    click.echo("\n  gates passed")


@main.command()
@click.option("--fetched", default=None, help="Date the tiles were fetched (provenance).")
@click.pass_obj
def validate(cfg: Paths, fetched: str | None) -> None:
    """Build in memory and gate it, writing nothing."""
    from geo.build.pipeline import run

    click.echo(f"validating from {cfg.root}")
    # tiers=False: the tier layers emit as they build, and a validate must not write.
    _report(run(cfg, fetched=fetched or _today(), built=_today(), write=False, tiers=False),
            wrote=False)


@main.command()
@click.option("--fetched", default=None, help="Date the tiles were fetched (provenance).")
@click.pass_obj
def build(cfg: Paths, fetched: str | None) -> None:
    """Gate, then write every layer."""
    from geo.build.pipeline import run

    click.echo(f"building into {cfg.final}")
    _report(run(cfg, fetched=fetched or _today(), built=_today(), write=True), wrote=True)


@main.command()
@click.pass_obj
def maps(cfg: Paths) -> None:
    """Render the choropleth PNGs from the emitted layers."""
    from geo.build.maps import render_all

    if not (cfg.final / "wards_2025.geojson").exists():
        raise click.ClickException(f"no layers in {cfg.final} -- run `geo build` first")
    click.echo(f"rendering into {cfg.maps}")
    for path in render_all(cfg):
        click.echo(f"  {path.name:40} {path.stat().st_size / 1e6:>6.2f} MB")


if __name__ == "__main__":
    sys.exit(main())
