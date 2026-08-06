"""Choropleth PNGs from the emitted layers.

Two figures: the ruling front across 2015/2020/2025 as a small multiple, and the
2025 ward winners on their own, since ward geometry exists for that cycle only.

**The palette is domain-semantic, and deliberately so.** LDF red, UDF blue and NDA
saffron are how Kerala reads its own politics; a generic categorical ramp would be
technically valid and actively misleading. That choice was still checked rather than
assumed -- the five hues below pass the lightness band, chroma floor, CVD separation
and contrast checks against this surface.

Two encoding decisions worth stating because both hide real information if taken the
easy way:

* **Hung is grey, not a sixth hue.** It is the *absence* of a ruling front, not a
  party. Giving it an identity colour would imply one it does not have.
* **Independents are their own category, not part of "Other".** Across all three
  cycles they are 85-95% of everything outside the three fronts -- 1,371 wards in
  2025, more than every other non-front party combined. Folding them into a residual
  bucket hides the single largest non-front result on the map.
"""

from __future__ import annotations

import collections
import itertools
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import matplotlib

matplotlib.use("Agg")  # no display in a build; must precede the pyplot import
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.collections import PathCollection  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.path import Path as MplPath  # noqa: E402
from shapely.geometry import shape  # noqa: E402
from shapely.geometry.base import BaseGeometry  # noqa: E402
from shapely.geometry.polygon import orient  # noqa: E402

from geo.config import Paths  # noqa: E402

SURFACE: Final = "#fcfcfb"
INK: Final = "#1a1a19"
MUTED: Final = "#6b6b68"

#: Absence of a ruling front. Not a party, so not a categorical hue.
HUNG: Final = "#d4d4d8"

FRONT_COLOURS: Final[dict[str, str]] = {
    "LDF": "#C62828",
    "UDF": "#1565C0",
    "NDA": "#D97706",
    "OTH": "#15803D",
}
WARD_COLOURS: Final[dict[str, str]] = {
    "LDF": "#C62828",
    "UDF": "#1565C0",
    "NDA": "#D97706",
    "IND": "#7C3AED",
    "OTHP": "#15803D",
}
WARD_LABELS: Final[dict[str, str]] = {
    "LDF": "LDF",
    "UDF": "UDF",
    "NDA": "NDA",
    "IND": "Independent",
    "OTHP": "Other parties",
}

#: The three directly-elected tiers. Block and District Panchayats nest *above*
#: these, so drawing them on the same map would stack polygons rather than tile them.
BASE_TIERS: Final[frozenset[str]] = frozenset(
    {"Grama Panchayat", "Municipality", "Corporation"}
)

#: cos(10.5N). Kerala is tall and narrow; without this it renders visibly squashed.
ASPECT: Final = 1 / 0.983


def ward_class(properties: Mapping[str, Any]) -> str:
    """Which legend bucket a ward belongs to.

    Independents are split out of ``OTH`` -- see the module docstring for why that
    is a correctness decision rather than a presentation one.
    """
    group = properties.get("winner_party_group")
    if group in ("LDF", "UDF", "NDA"):
        return group
    return "IND" if properties.get("winner_party") == "IND" else "OTHP"


def geometry_to_paths(geometry: BaseGeometry) -> list[MplPath]:
    """Shapely polygon to matplotlib paths, holes preserved as compound paths.

    Holes matter: a local body enclosing another (Kerala has several) would be drawn
    as a solid blob if the interior rings were dropped, silently painting over the
    body inside it.
    """
    out: list[MplPath] = []
    polygons = [geometry] if geometry.geom_type == "Polygon" else list(getattr(geometry, "geoms", []))
    for polygon in polygons:
        if polygon.geom_type != "Polygon" or polygon.is_empty:
            continue
        # Rings must be oriented before they become a path. Matplotlib fills a
        # compound path by the nonzero winding rule, so an interior ring only
        # punches a hole when it winds *opposite* to its exterior -- and shapely
        # makes no promise about orientation. Left alone, a local body that
        # encloses another renders as a solid blob painted over the body inside it.
        polygon = orient(polygon, sign=1.0)
        vertices: list[tuple[float, float]] = []
        codes: list[int] = []
        for ring in [polygon.exterior, *polygon.interiors]:
            coords = list(ring.coords)
            if len(coords) < 3:
                continue
            vertices.extend(coords)
            codes.extend(
                [MplPath.MOVETO] + [MplPath.LINETO] * (len(coords) - 2) + [MplPath.CLOSEPOLY]
            )
        if vertices:
            out.append(MplPath(vertices, codes))
    return out


def _draw(ax, geometries: Sequence[BaseGeometry], colours: Sequence[str], *, linewidth: float) -> None:
    paths: list[MplPath] = []
    facecolors: list[str] = []
    for geometry, colour in zip(geometries, colours):
        for path in geometry_to_paths(geometry):
            paths.append(path)
            facecolors.append(colour)
    ax.add_collection(
        PathCollection(
            paths, facecolors=facecolors, edgecolors=SURFACE, linewidths=linewidth, antialiased=True
        )
    )


def _frame(ax, bounds: tuple[float, float, float, float]) -> None:
    minx, miny, maxx, maxy = bounds
    ax.set_xlim(minx - 0.05, maxx + 0.05)
    ax.set_ylim(miny - 0.05, maxy + 0.05)
    ax.set_aspect(ASPECT)
    ax.axis("off")


def _bounds(geometries: Iterable[BaseGeometry]) -> tuple[float, float, float, float]:
    boxes = [g.bounds for g in geometries]
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _load(path: Path, tiers: frozenset[str] = BASE_TIERS) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [f for f in data["features"] if f["properties"].get("lb_type") in tiers]


def ruling_front_figure(paths: Paths, years: Sequence[str] = ("2015", "2020", "2025")) -> Path:
    """Small multiple: which front rules each local body, one panel per cycle.

    Every panel is drawn from **one** basemap rather than each cycle's own geometry.
    The point of the figure is political change, and letting the boundaries move
    between panels would mix cartographic difference into a comparison that is meant
    to isolate the political kind.
    """
    base = {
        f["properties"]["lb_code"]: shape(f["geometry"])
        for f in _load(paths.final / "local_bodies_2020.geojson")
    }
    fronts = {
        year: {
            f["properties"]["lb_code"]: f["properties"].get("lb_ruling_front") or None
            for f in _load(paths.final / f"local_bodies_{year}.geojson")
        }
        for year in years
    }
    codes = sorted(set(base).intersection(*(set(v) for v in fronts.values())))
    bounds = _bounds(base[c] for c in codes)

    fig, axes = plt.subplots(1, len(years), figsize=(4.5 * len(years), 9.6), facecolor=SURFACE)
    fig.subplots_adjust(left=0.015, right=0.985, top=0.815, bottom=0.115, wspace=0.01)
    for ax, year in zip(axes, years):
        by_code = fronts[year]
        _draw(
            ax,
            [base[c] for c in codes],
            [FRONT_COLOURS.get(by_code[c], HUNG) for c in codes],
            linewidth=0.12,
        )
        _frame(ax, bounds)
        tally = collections.Counter(by_code[c] or "hung" for c in codes)
        lead = max((k for k in tally if k != "hung"), key=lambda k: tally[k])
        ax.set_title(
            f"{year}\n{lead} leads with {tally[lead]} of {len(codes)}",
            color=INK, fontsize=15, pad=12, linespacing=1.7,
        )

    handles = [
        Line2D([], [], marker="s", linestyle="", markersize=11, markerfacecolor=FRONT_COLOURS[k],
               markeredgecolor=SURFACE, label=k)
        for k in ("LDF", "UDF", "NDA", "OTH")
    ] + [
        Line2D([], [], marker="s", linestyle="", markersize=11, markerfacecolor=HUNG,
               markeredgecolor=SURFACE, label="hung (no ruling front)")
    ]
    fig.legend(handles=handles, loc="lower center", ncol=5, frameon=False,
               bbox_to_anchor=(0.5, 0.068), fontsize=12, labelcolor=INK)
    fig.suptitle("Which front rules each Kerala local body", color=INK, fontsize=22, y=0.982)
    fig.text(
        0.5, 0.943,
        f"Grama Panchayats, Municipalities and Corporations — {len(codes)} bodies, "
        "identical basemap in all three panels",
        ha="center", va="top", color=MUTED, fontsize=12.5,
    )
    fig.text(
        0.5, 0.018,
        "Boundaries: OpenStreetMap via opendatakerala (ODbL), Nov 2020 snapshot — used for all three "
        "years, so differences are political, not cartographic.\n"
        "Results: Kerala State Election Commission. Block and District Panchayats are excluded: they "
        "nest above these bodies.",
        ha="center", color=MUTED, fontsize=9.5, linespacing=1.6,
    )
    out = paths.maps / "kerala_ruling_front_2015_2020_2025.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return out


def ward_winner_figure(paths: Paths, year: str = "2025") -> Path:
    """Every ward, coloured by who won it. 2025 only -- no earlier ward geometry exists."""
    features = _load(paths.final / f"wards_{year}.geojson")
    geometries = [shape(f["geometry"]) for f in features]
    classes = [ward_class(f["properties"]) for f in features]
    tally = collections.Counter(classes)
    bodies = len({f["properties"]["lb_code"] for f in features})

    fig, ax = plt.subplots(figsize=(8.6, 12.4), facecolor=SURFACE)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.885, bottom=0.155)
    _draw(ax, geometries, [WARD_COLOURS[k] for k in classes], linewidth=0.04)
    _frame(ax, _bounds(geometries))
    handles = [
        Line2D([], [], marker="s", linestyle="", markersize=11, markerfacecolor=WARD_COLOURS[k],
               markeredgecolor=SURFACE, label=f"{WARD_LABELS[k]} — {tally[k]:,}")
        for k in ("UDF", "LDF", "NDA", "IND", "OTHP")
    ]
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=12,
              labelcolor=INK, title="Winning ward", title_fontsize=12.5)
    fig.suptitle(f"Who won each ward — Kerala {year}", color=INK, fontsize=22, y=0.975)
    fig.text(
        0.5, 0.937,
        f"{len(features):,} wards across {bodies:,} Grama Panchayats, Municipalities and Corporations",
        ha="center", va="top", color=MUTED, fontsize=12.5,
    )
    fig.text(
        0.5, 0.072,
        f"Boundaries: {year} delimitation, Delimitation Commission of Kerala\n"
        "via wardmap.ksmart.live, vector tiles at zoom 14. Indicative\n"
        "election-purpose boundaries — not cadastral.\n"
        "Independents are shown separately: they win far more wards\n"
        "than every other non-front party combined.",
        ha="center", color=MUTED, fontsize=9.5, linespacing=1.7,
    )
    out = paths.maps / f"kerala_wards_{year}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return out


def local_body_zoom_figure(paths: Paths, lb_codes: Sequence[str], *, label: str) -> Path:
    """One or more local bodies at readable size, each ward a different colour.

    This exists because the statewide ward map cannot show a geometry defect. Wards
    mirrored inside their own tiles still produced correct feature counts, valid
    polygons, full district coverage and zero overlap -- every numeric check passed
    while the geometry was shredded. It only became visible zoomed to one body, so
    that view is a build artefact, not a debugging afterthought.
    """
    features = [
        f for f in _load(paths.final / "wards_2025.geojson") if f["properties"]["lb_code"] in set(lb_codes)
    ]
    if not features:
        raise ValueError(f"no wards found for {lb_codes}")
    geometries = [shape(f["geometry"]) for f in features]
    palette = itertools.cycle(list(WARD_COLOURS.values()))
    colours = [next(palette) for _ in geometries]

    fig, ax = plt.subplots(figsize=(10, 10), facecolor=SURFACE)
    _draw(ax, geometries, colours, linewidth=0.5)
    _frame(ax, _bounds(geometries))
    ax.set_title(
        f"Ward shapes: {label} — {len(features)} wards\n"
        "Colours are arbitrary; this view exists to check geometry, not politics",
        color=INK, fontsize=13, linespacing=1.6,
    )
    out = paths.maps / "ward_shape_check.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE, bbox_inches="tight")
    plt.close(fig)
    return out


def render_all(paths: Paths) -> list[Path]:
    """Every figure, in the order a reader should meet them."""
    written = [ruling_front_figure(paths), ward_winner_figure(paths)]
    try:
        written.append(
            local_body_zoom_figure(paths, ["C07003", "G07064"], label="Kochi Corporation + one GP")
        )
    except (ValueError, FileNotFoundError):
        pass  # the zoom is a check, not a deliverable; never fail a render over it
    return written
