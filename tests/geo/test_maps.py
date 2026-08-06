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
