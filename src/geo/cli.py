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


@main.command()
@click.pass_obj
def validate(cfg: Paths) -> None:
    """Build in memory and gate it, writing nothing."""
    raise click.ClickException("not implemented yet -- see Units 4-8 of the plan")


@main.command()
@click.pass_obj
def build(cfg: Paths) -> None:
    """Gate, then write layers, reports and a manifest."""
    raise click.ClickException("not implemented yet -- see Units 4-8 of the plan")


if __name__ == "__main__":
    sys.exit(main())
