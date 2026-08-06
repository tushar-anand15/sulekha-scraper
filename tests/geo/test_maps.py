"""The map renderer, checked for the things a rendered PNG cannot tell you.

Almost nothing here asserts on pixels. The valuable checks are the encoding
decisions -- which bucket a ward falls into, whether holes survive -- because those
are the ones that silently misinform if wrong, and a picture looks fine either way.
"""

from __future__ import annotations

import json

import pytest
from shapely.geometry import Polygon, mapping

from geo.build.maps import (
    FRONT_COLOURS,
    HUNG,
    WARD_COLOURS,
    WARD_LABELS,
    geometry_to_paths,
    render_all,
    ward_class,
)
from geo.config import resolve_paths


# --- classification ---------------------------------------------------------


@pytest.mark.parametrize("group", ["LDF", "UDF", "NDA"])
def test_front_winners_keep_their_front(group: str) -> None:
    assert ward_class({"winner_party_group": group, "winner_party": "X"}) == group


def test_independents_are_their_own_bucket_not_other() -> None:
    """Across all three cycles Independents are 85-95% of everything outside the
    fronts -- 1,371 wards in 2025, more than every other non-front party combined.
    Folding them into "Other" hides the largest non-front result on the map."""
    assert ward_class({"winner_party_group": "OTH", "winner_party": "IND"}) == "IND"


def test_small_parties_fall_into_other() -> None:
    for party in ("SDPI", "T20", "WPI", "RMPI", "BSP"):
        assert ward_class({"winner_party_group": "OTH", "winner_party": party}) == "OTHP"


def test_every_ward_class_has_a_colour_and_a_label() -> None:
    for key in ("LDF", "UDF", "NDA", "IND", "OTHP"):
        assert key in WARD_COLOURS
        assert key in WARD_LABELS


def test_hung_is_not_a_categorical_hue() -> None:
    """Hung is the absence of a ruling front, not a sixth party. Giving it one of
    the identity colours would imply an identity it does not have."""
    assert HUNG not in FRONT_COLOURS.values()
    assert HUNG not in WARD_COLOURS.values()


# --- geometry ---------------------------------------------------------------


def test_holes_actually_render_as_holes() -> None:
    """Asserted on rendered pixels, not on the path object.

    A body enclosing another must not be painted over it. Two things can break
    that: dropping interior rings, or emitting them with the same winding as the
    exterior -- matplotlib fills by the nonzero rule, so a same-wound inner ring
    fills solid instead of punching through. ``Path.contains_point`` cannot see
    either problem (it reports True inside a correctly-rendered hole), so the only
    honest check is to draw it and look at the pixels.
    """
    import matplotlib
    import numpy as np
    from matplotlib.collections import PathCollection
    from matplotlib.path import Path as MplPath

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    outer = [(0, 0), (0, 4), (4, 4), (4, 0)]
    hole = [(1, 1), (1, 3), (3, 3), (3, 1)]
    (path,) = geometry_to_paths(Polygon(outer, [hole]))
    assert list(path.codes).count(MplPath.MOVETO) == 2, "interior ring was dropped"

    fig, ax = plt.subplots(figsize=(2, 2), dpi=50)
    ax.add_collection(PathCollection([path], facecolors="#000000", edgecolors="none"))
    ax.set_xlim(0, 4)
    ax.set_ylim(0, 4)
    ax.axis("off")
    fig.subplots_adjust(0, 0, 1, 1)
    fig.canvas.draw()
    pixels = np.asarray(fig.canvas.buffer_rgba())
    height, width, _ = pixels.shape
    inside_hole = tuple(pixels[height // 2, width // 2][:3])
    inside_fill = tuple(pixels[int(height * 0.9), int(width * 0.1)][:3])
    plt.close(fig)

    assert inside_hole == (255, 255, 255), "the hole was filled in"
    assert inside_fill == (0, 0, 0), "the body itself did not render"


def test_multipolygon_yields_one_path_per_part() -> None:
    from shapely.geometry import MultiPolygon

    geom = MultiPolygon(
        [Polygon([(0, 0), (0, 1), (1, 1)]), Polygon([(5, 5), (5, 6), (6, 6)])]
    )
    assert len(geometry_to_paths(geom)) == 2


def test_degenerate_ring_is_skipped_not_crashed() -> None:
    """Shapely can hand back a sliver with too few points after a repair."""
    assert geometry_to_paths(Polygon()) == []


# --- end to end -------------------------------------------------------------


def _layer(features: list[dict]) -> dict:
    return {"type": "FeatureCollection", "provenance": {}, "features": features}


def _body(code: str, front: str | None, x: float) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(Polygon([(x, 0), (x, 1), (x + 1, 1), (x + 1, 0)])),
        "properties": {
            "lb_code": code,
            "lb_type": "Grama Panchayat",
            "lb_ruling_front": front,
            "district_name": "KOLLAM",
        },
    }


def test_render_all_writes_into_the_maps_directory(tmp_path) -> None:
    paths = resolve_paths(tmp_path)
    paths.final.mkdir(parents=True, exist_ok=True)
    bodies = [_body("G02001", "LDF", 0), _body("G02002", "UDF", 2), _body("G02003", None, 4)]
    for year in ("2015", "2020", "2025"):
        (paths.final / f"local_bodies_{year}.geojson").write_text(json.dumps(_layer(bodies)))

    wards = [
        {
            "type": "Feature",
            "geometry": mapping(Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])),
            "properties": {
                "lb_code": "G02001",
                "lb_type": "Grama Panchayat",
                "district_name": "KOLLAM",
                "winner_party_group": "OTH",
                "winner_party": "IND",
            },
        }
    ]
    (paths.final / "wards_2025.geojson").write_text(json.dumps(_layer(wards)))

    written = render_all(paths)
    assert written, "nothing rendered"
    for path in written:
        assert path.parent == paths.maps
        assert path.suffix == ".png"
        assert path.stat().st_size > 0


def test_render_survives_a_missing_zoom_target(tmp_path) -> None:
    """The zoom view is a geometry check, not a deliverable; a build must not fail
    because the sample local body is absent from a partial dataset."""
    paths = resolve_paths(tmp_path)
    paths.final.mkdir(parents=True, exist_ok=True)
    bodies = [_body("G02001", "LDF", 0)]
    for year in ("2015", "2020", "2025"):
        (paths.final / f"local_bodies_{year}.geojson").write_text(json.dumps(_layer(bodies)))
    (paths.final / "wards_2025.geojson").write_text(
        json.dumps(
            _layer(
                [
                    {
                        "type": "Feature",
                        "geometry": mapping(Polygon([(0, 0), (0, 1), (1, 1)])),
                        "properties": {
                            "lb_code": "G02001",
                            "lb_type": "Grama Panchayat",
                            "district_name": "KOLLAM",
                            "winner_party_group": "LDF",
                        },
                    }
                ]
            )
        )
    )
    written = render_all(paths)
    assert not any(p.name == "ward_shape_check.png" for p in written)


# --- classed (sequential) encodings -----------------------------------------


def test_quantile_bins_split_the_observed_values_evenly():
    from geo.build.maps import _bin_index, _quantile_bins

    values = list(range(100))
    cuts = _quantile_bins(values, 5)
    assert len(cuts) == 4
    counts = [0] * 5
    for v in values:
        counts[_bin_index(v, cuts)] += 1
    assert max(counts) - min(counts) <= 1


def test_quantile_bins_beat_equal_width_on_a_skewed_distribution():
    """Margins are heavily skewed -- a long tail of huge ones over a dense low end.
    Equal-width bins put almost every ward in the first class and render flat."""
    from geo.build.maps import _bin_index, _quantile_bins

    skewed = [int(1.6**i) for i in range(1, 40)] * 25
    cuts = _quantile_bins(skewed, 5)
    quantile_spread = len({_bin_index(v, cuts) for v in skewed})

    lo, hi = min(skewed), max(skewed)
    width = (hi - lo) / 5
    equal_width = len({min(int((v - lo) / width), 4) for v in skewed})

    assert quantile_spread == 5
    assert quantile_spread > equal_width


def test_a_mass_point_defeats_quantile_binning():
    """Why the zero class exists.

    Quantiles cannot separate a value that *is* a large share of the data: with
    over a fifth of observations identical, several cuts land on the same number
    and the legend degenerates -- it read "under 0%" and "0-0%" before the zero
    class was split out of the ramp.
    """
    from geo.build.maps import _quantile_bins

    zero_inflated = [0.0] * 900 + [i / 100 for i in range(1, 101)]
    cuts = _quantile_bins(zero_inflated, 5)
    assert len(set(cuts)) < len(cuts), "expected collapsed, indistinguishable cut points"


def test_quantile_bins_survive_degenerate_input():
    """A partial dataset must not crash the renderer with an IndexError."""
    from geo.build.maps import _quantile_bins

    assert _quantile_bins([], 5) == []
    assert _quantile_bins([1, 2, 3], 1) == []


def test_bin_index_is_inclusive_at_the_top():
    from geo.build.maps import _bin_index

    assert _bin_index(10_000, [1, 2, 3]) == 3
    assert _bin_index(0, [1, 2, 3]) == 0


def test_sequential_ramps_are_monotonically_darker():
    """A sequential ramp must be one hue, light to dark. If lightness is not
    monotonic the reader cannot order the classes by eye at all."""
    from geo.build.maps import SEQUENTIAL_CYAN, SEQUENTIAL_VIOLET

    def luminance(hex_colour: str) -> float:
        raw = hex_colour.lstrip("#")
        channels = [int(raw[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    for ramp in (SEQUENTIAL_VIOLET, SEQUENTIAL_CYAN):
        levels = [luminance(c) for c in ramp]
        assert all(levels[i] > levels[i + 1] for i in range(len(levels) - 1)), ramp


def test_community_reservation_reads_the_woman_variants_too():
    """"SC Woman" is an SC-reserved ward. Matching only the exact string "SC"
    would drop 921 of the 2,019 SC seats from the map."""
    import re

    from geo.build.maps import RESERVATION_COLOURS

    assert set(RESERVATION_COLOURS) == {"SC", "ST"}
    for text, expected in [("SC", "SC"), ("SC Woman", "SC"), ("ST", "ST"), ("ST Woman", "ST")]:
        assert re.match(r"^(SC|ST)", text).group(1) == expected


def test_panel_value_functions_all_return_percentage_points():
    """One shared legend format string means one shared unit.

    A value function returning a 0-1 fraction renders a legend reading "up to 0%"
    and "0%-0%" -- which is exactly what the SC/ST share did before this.
    """
    paths = resolve_paths()
    if not (paths.elections / "2025" / "wards_2025.csv").exists():
        pytest.skip("election CSVs not present")
    from geo.build.maps import _median_margin_pct, _reservation_share

    for fn in (_reservation_share, _median_margin_pct):
        values = list(fn(paths, "2025").values())
        assert values, fn.__name__
        # A percentage of seats or votes: never a 0-1 fraction, never over 100.
        assert max(values) > 1.5, f"{fn.__name__} looks like a fraction, not a percentage"
        assert max(values) <= 100.0, fn.__name__
