"""Tile arithmetic, checked against answers the live server confirmed.

The coordinates in ``test_known_kerala_tiles`` are not invented: each returned HTTP
200 with ward geometry from the KSMART server during planning. That makes them a
regression test against the one bug class this module can produce silently -- a
formula that is self-consistent but disagrees with the server's grid.
"""

from __future__ import annotations

import math

import pytest

from geo.tiles import (
    EXTENT,
    R,
    deg_to_tile,
    metres_per_unit,
    tile_affine,
    tile_bounds_3857,
    tile_children,
    tile_range,
)

# (lat, lon, z) -> (x, y), all confirmed 200-with-content against the live server.
KNOWN = [
    ((8.5241, 76.9366), 10, (730, 487)),   # Thiruvananthapuram
    ((9.9312, 76.2673), 10, (728, 483)),   # Kochi
    ((11.2588, 75.7804), 10, (727, 479)),  # Kozhikode
    ((8.5241, 76.9366), 14, (11693, 7802)),
    ((9.9312, 76.2673), 14, (11663, 7737)),
    ((11.2588, 75.7804), 14, (11640, 7676)),
]


@pytest.mark.parametrize("lonlat,z,expected", KNOWN)
def test_known_kerala_tiles(lonlat: tuple[float, float], z: int, expected: tuple[int, int]) -> None:
    lat, lon = lonlat
    assert deg_to_tile(lat, lon, z) == expected


def test_zero_zoom_covers_the_world() -> None:
    assert tile_bounds_3857(0, 0, 0) == pytest.approx((-R, -R, R, R))


def test_tile_bounds_are_contiguous() -> None:
    """Neighbouring tiles share an edge *exactly*.

    Exact equality is the assertion, not approximate: each edge is derived from its
    own index, so both tiles evaluate the same expression. An implementation that
    computed ``maxx`` as ``minx + span`` would pass an approx check and fail here,
    which is the point.
    """
    left = tile_bounds_3857(728, 483, 10)
    right = tile_bounds_3857(729, 483, 10)
    below = tile_bounds_3857(728, 484, 10)
    assert left[2] == right[0]
    assert left[1] == below[3]


@pytest.mark.parametrize("z", [8, 10, 12, 14, 16])
def test_tile_centre_round_trips(z: int) -> None:
    """A tile's own centre must map back to that tile at every zoom we scrape."""
    shift = z - 14
    x0 = 11693 << shift if shift >= 0 else 11693 >> -shift
    y0 = 7802 << shift if shift >= 0 else 7802 >> -shift
    minx, miny, maxx, maxy = tile_bounds_3857(x0, y0, z)
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    lon = cx / R * 180.0
    lat = math.degrees(2.0 * math.atan(math.exp(cy / R * math.pi)) - math.pi / 2.0)
    assert deg_to_tile(lat, lon, z) == (x0, y0)


def test_children_tile_the_parent_exactly() -> None:
    parent = tile_bounds_3857(728, 483, 10)
    kids = tile_children(728, 483, 10)
    assert len(kids) == 4
    assert all(z == 11 for z, _, _ in kids)
    boxes = [tile_bounds_3857(x, y, z) for z, x, y in kids]
    assert min(b[0] for b in boxes) == parent[0]
    assert min(b[1] for b in boxes) == parent[1]
    assert max(b[2] for b in boxes) == parent[2]
    assert max(b[3] for b in boxes) == parent[3]
    # And they partition it: four quarter-areas summing to the whole.
    area = lambda b: (b[2] - b[0]) * (b[3] - b[1])  # noqa: E731
    assert sum(area(b) for b in boxes) == pytest.approx(area(parent))


def test_affine_maps_tile_corners_onto_tile_bounds() -> None:
    """Local (0,0) is the tile's top-left; (EXTENT,EXTENT) its bottom-right.

    MVT y grows upward, the tile grid's y grows downward. If the sign is wrong this
    still produces well-formed polygons, just mirrored -- so it is asserted directly.
    """
    a, b, d, e, xoff, yoff = tile_affine(728, 483, 10)
    minx, miny, maxx, maxy = tile_bounds_3857(728, 483, 10)
    top_left = (a * 0 + b * 0 + xoff, d * 0 + e * 0 + yoff)
    bottom_right = (a * EXTENT + b * EXTENT + xoff, d * EXTENT + e * EXTENT + yoff)
    assert top_left == pytest.approx((minx, maxy))
    assert bottom_right == pytest.approx((maxx, miny))


def test_shared_edge_agrees_far_below_the_quantisation_floor() -> None:
    """The seam guarantee, stated honestly.

    Vertices on a shared edge do *not* land on bit-identical floats: the western
    tile arrives via ``minx + span`` and the eastern starts at ``-R + (x+1)*span``,
    and floating-point addition is not associative. What matters is the size of the
    disagreement, so that is what is asserted -- it must stay negligible against the
    z-level quantisation floor, which is the real precision of the source data.

    Stitching therefore has to tolerate nanometre-scale disagreement. It must not
    assume exact coincidence, and equally must not reach for a metre-scale snapping
    buffer to fix a nanometre-scale problem.
    """
    for z in (10, 14, 16):
        a1, _, _, _, xoff1, _ = tile_affine(728 << (z - 10), 483 << (z - 10), z)
        a2, _, _, _, xoff2, _ = tile_affine((728 << (z - 10)) + 1, 483 << (z - 10), z)
        from_west = a1 * EXTENT + xoff1
        from_east = a2 * 0 + xoff2
        drift = abs(from_west - from_east)
        assert drift < metres_per_unit(z) * 1e-6, f"z{z}: {drift} m is not negligible"
        assert drift < 1e-6, f"z{z}: {drift} m exceeds a micrometre"


def test_tile_range_covers_a_bbox() -> None:
    xs, ys = tile_range(9.93, 76.22, 10.03, 76.30, 14)
    assert len(xs) >= 1 and len(ys) >= 1
    for lat, lon in [(9.93, 76.22), (10.03, 76.30), (9.98, 76.26)]:
        x, y = deg_to_tile(lat, lon, 14)
        assert x in xs and y in ys


def test_metres_per_unit_matches_the_z14_quantum() -> None:
    """~0.6 m at z14 -- the floor on any seam-closing tolerance."""
    assert metres_per_unit(14) == pytest.approx(0.6, abs=0.05)
    assert metres_per_unit(15) == pytest.approx(metres_per_unit(14) / 2)


def test_latitude_is_clamped_to_the_mercator_limit() -> None:
    """Beyond ~85.05 degrees the projection diverges; indices must stay in range."""
    for z in (8, 14):
        n = 1 << z
        for lat in (89.9, -89.9, 85.0511287798066, -85.0511287798066):
            x, y = deg_to_tile(lat, 76.0, z)
            assert 0 <= x < n
            assert 0 <= y < n


def test_equator_and_prime_meridian_are_in_range() -> None:
    x, y = deg_to_tile(0.0, 0.0, 10)
    assert (x, y) == (512, 512)


def test_eastern_edge_does_not_overflow() -> None:
    n = 1 << 10
    x, _ = deg_to_tile(0.0, 180.0, 10)
    assert x == n - 1
