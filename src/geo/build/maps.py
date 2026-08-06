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
import csv
import itertools
import json
import logging
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

#: Single-hue ramps for magnitude, light to dark, lightness strictly monotonic.
#: Never a rainbow: a hue sequence has no inherent order, so readers invent one.
SEQUENTIAL_VIOLET: Final[tuple[str, ...]] = ("#EDE9FE", "#C4B5FD", "#A78BFA", "#7C3AED", "#5B21B6")
SEQUENTIAL_CYAN: Final[tuple[str, ...]] = ("#E0F2FE", "#7DD3FC", "#38BDF8", "#0284C7", "#075985")

#: Communities with reserved seats. "None" is the 88% majority and takes a neutral --
#: it is the absence of a community reservation, not a third community.
RESERVATION_COLOURS: Final[dict[str, str]] = {"SC": "#7C3AED", "ST": "#0891B2"}
NO_RESERVATION: Final = "#e4e4e7"

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


def _winners(paths: Paths, year: str) -> list[dict[str, str]]:
    """Winning candidate rows for one cycle, three tiers only.

    Read from ``candidates_<year>.csv`` rather than the ward file because gender,
    age and the candidate's own party live there; the ward file carries only the
    winner's name and party.
    """
    path = paths.elections / year / f"candidates_{year}.csv"
    with path.open(newline="", encoding="utf-8-sig") as fh:
        return [
            row
            for row in csv.DictReader(fh)
            if row.get("status") == "won" and row.get("lb_type") in BASE_TIERS
        ]


def ruling_front_figure(paths: Paths, years: Sequence[str] = ("2015", "2020", "2025")) -> Path:
    """Small multiple: which front rules each local body, one panel per cycle.

    **Each panel uses its own cycle's boundaries**, because we have them: 2015 and
    2020 from the OpenStreetMap snapshot, 2025 from the KSMART delimitation. Drawing
    all three from one source would be quietly overriding real data with a
    convenient assumption -- and the two sources genuinely differ, at a median IoU
    of 0.875 per body, so the substitution is not free.

    2015 and 2020 do share one physical polygon set, because that is the honest
    state of the sources: there is no per-cycle local-body geometry for either year,
    only the single November 2020 snapshot. That is a property of what exists, not a
    choice made here, and the caption says so.
    """
    layers = {
        year: {
            f["properties"]["lb_code"]: (
                shape(f["geometry"]),
                f["properties"].get("lb_ruling_front") or None,
            )
            for f in _load(paths.final / f"local_bodies_{year}.geojson")
        }
        for year in years
    }
    codes = sorted(set.intersection(*(set(v) for v in layers.values())))
    bounds = _bounds(geom for year in years for geom, _ in (layers[year][c] for c in codes))

    fig, axes = plt.subplots(1, len(years), figsize=(4.5 * len(years), 9.6), facecolor=SURFACE)
    fig.subplots_adjust(left=0.015, right=0.985, top=0.815, bottom=0.115, wspace=0.01)
    for ax, year in zip(axes, years):
        cycle = layers[year]
        _draw(
            ax,
            [cycle[c][0] for c in codes],
            [FRONT_COLOURS.get(cycle[c][1], HUNG) for c in codes],
            linewidth=0.12,
        )
        _frame(ax, bounds)
        tally = collections.Counter(cycle[c][1] or "hung" for c in codes)
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
        "each panel drawn from its own cycle's boundaries",
        ha="center", va="top", color=MUTED, fontsize=12.5,
    )
    fig.text(
        0.5, 0.018,
        "Boundaries — 2015 and 2020: OpenStreetMap via opendatakerala (ODbL). Those two cycles share one "
        "Nov 2020 snapshot because no per-cycle local-body geometry exists for either.\n"
        "2025: Delimitation Commission of Kerala via wardmap.ksmart.live. Results: Kerala State Election "
        "Commission. Block and District Panchayats are excluded — they nest above these bodies.",
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


def _quantile_bins(values: Sequence[float], n: int) -> list[float]:
    """``n - 1`` cut points at equal quantiles of the observed values.

    Quantiles rather than equal-width bins: these distributions are skewed (most
    local bodies elect few or no women to unreserved seats), and equal-width bins
    would put almost everything in the first class and show a flat map.
    """
    ordered = sorted(values)
    if not ordered or n < 2:
        return []
    return [ordered[int(len(ordered) * i / n)] for i in range(1, n)]


def _bin_index(value: float, cuts: Sequence[float]) -> int:
    for i, cut in enumerate(cuts):
        if value < cut:
            return i
    return len(cuts)


def _sequential_legend(ax, ramp: Sequence[str], labels: Sequence[str], title: str) -> None:
    handles = [
        Line2D([], [], marker="s", linestyle="", markersize=11, markerfacecolor=colour,
               markeredgecolor=SURFACE, label=label)
        for colour, label in zip(ramp, labels)
    ]
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=11,
              labelcolor=INK, title=title, title_fontsize=12)


def women_in_open_seats_figure(
    paths: Paths, years: Sequence[str] = ("2015", "2020", "2025")
) -> Path:
    """Share of *unreserved* seats won by women, per local body.

    Deliberately not a map of winner gender. Kerala reserves half its seats for
    women, so ~55% of all winners are women -- and mapping that mostly redraws the
    reservation. Worse, in this dataset about half the winner genders were *derived
    from* the ward being woman-reserved (``gender_source == "reserved_ward"``), so a
    raw gender map would partly be the reservation map wearing a different label.

    Restricting to General, SC and ST wards -- seats no woman was guaranteed --
    removes both the tautology and the circularity. Statewide that share is 6.2%,
    7.7% and 7.5% across the three cycles; the map shows where it is not.
    """
    figures = {}
    for year in years:
        geoms = {
            f["properties"]["lb_code"]: shape(f["geometry"])
            for f in _load(paths.final / f"local_bodies_{year}.geojson")
        }
        won = collections.defaultdict(lambda: [0, 0])
        for row in _winners(paths, year):
            if (row.get("ward_reservation") or "").strip() in ("General", "SC", "ST"):
                tally = won[row["lb_code"]]
                tally[1] += 1
                if (row.get("candidate_gender") or "").upper().startswith("F"):
                    tally[0] += 1
        figures[year] = (geoms, won)

    codes = sorted(set.intersection(*(set(g) for g, _ in figures.values())))
    shares = {
        year: {c: (won[c][0] / won[c][1] if won.get(c) and won[c][1] else None) for c in codes}
        for year, (_, won) in figures.items()
    }
    observed = [v for by_code in shares.values() for v in by_code.values() if v is not None]
    # Zero gets its own class rather than a bin edge. This distribution is
    # zero-inflated -- a large share of local bodies elect no women at all to an
    # unreserved seat -- so quantile cuts land on 0 twice and produce a legend
    # reading "under 0%" and "0-0%". "None at all" is also the single most
    # meaningful value here, not a low end of a range.
    positive = sorted(v for v in observed if v > 0)
    cuts = _quantile_bins(positive, len(SEQUENTIAL_VIOLET) - 1)
    zero_count = sum(1 for v in observed if v == 0)
    if not cuts:
        raise ValueError("no local body elected a woman to an unreserved seat; nothing to class")

    fig, axes = plt.subplots(1, len(years), figsize=(4.5 * len(years), 9.6), facecolor=SURFACE)
    fig.subplots_adjust(left=0.015, right=0.985, top=0.815, bottom=0.115, wspace=0.01)
    bounds = _bounds(figures[years[-1]][0][c] for c in codes)
    for ax, year in zip(axes, years):
        geoms, _ = figures[year]
        colours = [
            NO_RESERVATION
            if shares[year][c] is None
            else SEQUENTIAL_VIOLET[0]
            if shares[year][c] == 0
            else SEQUENTIAL_VIOLET[1 + _bin_index(shares[year][c], cuts)]
            for c in codes
        ]
        _draw(ax, [geoms[c] for c in codes], colours, linewidth=0.12)
        _frame(ax, bounds)
        vals = [v for v in shares[year].values() if v is not None]
        ax.set_title(
            f"{year}\n{100 * sum(vals) / len(vals):.1f}% average across bodies",
            color=INK, fontsize=15, pad=12, linespacing=1.7,
        )

    labels = (
        ["none"]
        + [f"up to {100 * cuts[0]:.0f}%"]
        + [f"{100 * cuts[i]:.0f}–{100 * cuts[i + 1]:.0f}%" for i in range(len(cuts) - 1)]
        + [f"over {100 * cuts[-1]:.0f}%"]
    )
    _sequential_legend(axes[-1], SEQUENTIAL_VIOLET, labels, "Women's share")
    fig.suptitle("Where women win seats that were not reserved for them",
                 color=INK, fontsize=22, y=0.982)
    fig.text(
        0.5, 0.943,
        "Share of General, SC and ST ward seats won by women — the half of the council "
        "no woman was guaranteed",
        ha="center", va="top", color=MUTED, fontsize=12.5,
    )
    fig.text(
        0.5, 0.018,
        "Woman-reserved wards are excluded: they are 100% women by construction, and in this dataset "
        "roughly half of all winner genders were inferred\n"
        "from that reservation rather than observed. Statewide the unreserved share is 6.2% (2015), "
        "7.7% (2020) and 7.5% (2025).\n"
        f"The palest class is not a low share but zero — {zero_count:,} of {3 * len(codes):,} body-cycles "
        "elected no woman to any unreserved seat. Results: Kerala State Election Commission.",
        ha="center", color=MUTED, fontsize=9.5, linespacing=1.6,
    )
    out = paths.maps / "kerala_women_unreserved_seats.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return out


def community_reservation_figure(paths: Paths, year: str = "2025") -> Path:
    """Which wards are reserved for Scheduled Castes or Scheduled Tribes.

    Separated from the woman-reservation because they are orthogonal facts about a
    ward, and a single six-way legend (General/Woman/SC/SC Woman/ST/ST Woman) makes
    a reader decode two dimensions from one colour. ST reservation in particular is
    tightly clustered -- Wayanad, Idukki, Attappady -- and that shape is the point.
    """
    features = _load(paths.final / f"wards_{year}.geojson")
    geoms = [shape(f["geometry"]) for f in features]

    def community(res: str | None) -> str | None:
        text = (res or "").upper()
        if text.startswith("SC"):
            return "SC"
        return "ST" if text.startswith("ST") else None

    classes = [community(f["properties"].get("reservation")) for f in features]
    tally = collections.Counter(c for c in classes if c)

    fig, ax = plt.subplots(figsize=(8.6, 12.4), facecolor=SURFACE)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.885, bottom=0.115)
    _draw(ax, geoms, [RESERVATION_COLOURS.get(c, NO_RESERVATION) for c in classes], linewidth=0.04)
    _frame(ax, _bounds(geoms))
    handles = [
        Line2D([], [], marker="s", linestyle="", markersize=11,
               markerfacecolor=RESERVATION_COLOURS[k], markeredgecolor=SURFACE,
               label=f"{k} reserved — {tally[k]:,}")
        for k in ("SC", "ST")
    ] + [
        Line2D([], [], marker="s", linestyle="", markersize=11, markerfacecolor=NO_RESERVATION,
               markeredgecolor=SURFACE, label=f"no community reservation — {len(classes) - sum(tally.values()):,}")
    ]
    ax.legend(handles=handles, loc="upper right", frameon=False, fontsize=12,
              labelcolor=INK, title="Ward reservation", title_fontsize=12.5)
    fig.suptitle(f"Seats reserved for SC and ST — {year}", color=INK, fontsize=22, y=0.975)
    fig.text(
        0.5, 0.937,
        f"{len(features):,} wards; SC and ST reservation tracks where those communities live",
        ha="center", va="top", color=MUTED, fontsize=12.5,
    )
    fig.text(
        0.5, 0.045,
        "Woman-reservation is mapped separately: it is an orthogonal fact about a ward, and roughly "
        "half of all wards carry it.\nBoundaries: 2025 delimitation via wardmap.ksmart.live. "
        "Results: Kerala State Election Commission.",
        ha="center", color=MUTED, fontsize=9.5, linespacing=1.6,
    )
    out = paths.maps / f"kerala_community_reservation_{year}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return out


def margin_figure(paths: Paths, year: str = "2025") -> Path:
    """How close each ward was, as the winner's lead over the runner-up.

    Uncontested wards are drawn in the neutral, not as a maximal margin: nobody
    stood against the winner, so there is no contest to be lopsided.
    """
    features = _load(paths.final / f"wards_{year}.geojson")
    geoms = [shape(f["geometry"]) for f in features]
    margins = [f["properties"].get("margin") for f in features]
    observed = [m for m in margins if m is not None]
    cuts = _quantile_bins(observed, len(SEQUENTIAL_CYAN))
    if not cuts:
        raise ValueError("no contested ward carries a margin; nothing to class")

    fig, ax = plt.subplots(figsize=(8.6, 12.4), facecolor=SURFACE)
    fig.subplots_adjust(left=0.02, right=0.98, top=0.885, bottom=0.115)
    _draw(
        ax, geoms,
        [NO_RESERVATION if m is None else SEQUENTIAL_CYAN[_bin_index(m, cuts)] for m in margins],
        linewidth=0.04,
    )
    _frame(ax, _bounds(geoms))
    labels = [f"under {cuts[0]:,.0f}"] + [
        f"{cuts[i]:,.0f}–{cuts[i + 1]:,.0f}" for i in range(len(cuts) - 1)
    ] + [f"over {cuts[-1]:,.0f}"]
    _sequential_legend(ax, SEQUENTIAL_CYAN, labels, "Winning margin (votes)")
    fig.suptitle(f"How close each ward was — {year}", color=INK, fontsize=22, y=0.975)
    fig.text(
        0.5, 0.937,
        f"Winner's lead over the runner-up, across {len(observed):,} contested wards",
        ha="center", va="top", color=MUTED, fontsize=12.5,
    )
    fig.text(
        0.5, 0.045,
        "Classed at equal quantiles, not equal width: margins are heavily\n"
        "skewed and equal-width bins would put almost every ward in one class.\n"
        f"{len(margins) - len(observed):,} uncontested wards are drawn neutral —\n"
        "no opponent stood, so there is no margin to report.",
        ha="center", color=MUTED, fontsize=9.5, linespacing=1.6,
    )
    out = paths.maps / f"kerala_margin_{year}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=170, facecolor=SURFACE)
    plt.close(fig)
    return out


def render_all(paths: Paths) -> list[Path]:
    """Every figure, in the order a reader should meet them.

    A figure whose inputs are missing is skipped rather than fatal -- a partial
    dataset should still render what it can, and the ones that fail say why.
    """
    figures = (
        ("ruling front", lambda: ruling_front_figure(paths)),
        ("ward winners", lambda: ward_winner_figure(paths)),
        ("women in unreserved seats", lambda: women_in_open_seats_figure(paths)),
        ("community reservation", lambda: community_reservation_figure(paths)),
        ("winning margin", lambda: margin_figure(paths)),
        (
            "ward shape check",
            lambda: local_body_zoom_figure(
                paths, ["C07003", "G07064"], label="Kochi Corporation + one GP"
            ),
        ),
    )
    written: list[Path] = []
    for label, build in figures:
        try:
            written.append(build())
        except (ValueError, KeyError, FileNotFoundError, OSError) as exc:
            logging.getLogger(__name__).warning("skipped %s figure: %s", label, exc)
    return written
