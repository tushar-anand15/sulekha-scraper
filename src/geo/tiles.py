"""Web Mercator tile arithmetic, and the affine that lifts tile-local MVT
coordinates into real ones.

Kept pure and dependency-free on purpose: both halves of the package rely on these
formulas, and an off-by-one here would not fail loudly -- it would place a ward a few
hundred metres from where it belongs, which no test of the fetcher or the stitcher
would catch. So they live alone, with exact known answers.

Conventions, all verified against the live KSMART server:

* Tiles are **XYZ**, y increasing southward. The TMS convention (y flipped) returns
  204 from that server, which is a silent wrong answer rather than an error.
* Tile-local coordinates run 0..``EXTENT`` with **y increasing upward**, the MVT
  spec's convention and the opposite of the tile grid's own.
* Every tile's local grid is a subdivision of the same global grid at that zoom, so
  a vertex on a shared edge lands in the same place from either neighbour. **Tile
  *bounds* agree exactly** -- both sides evaluate ``-R + x * span`` for the shared
  index, which is the same expression and therefore the same float. Coordinates
  produced through the *affine*, however, agree only to about a nanometre: the west
  tile reaches its right edge as ``minx + span`` while the east tile starts at
  ``-R + (x+1) * span``, and floating-point addition is not associative.

  That residual is ~2e-9 m against a z14 quantisation floor of ~0.6 m -- nine orders
  of magnitude below the data's own precision -- so unioning stitched fragments is
  safe. It is not, however, *exact*, so stitching must tolerate nanometre-scale
  disagreement rather than assume vertices coincide bitwise.
"""

from __future__ import annotations

import math
from typing import Final

#: MVT coordinate extent used by the KSMART layers.
EXTENT: Final = 4096

#: Semi-circumference of the Web Mercator plane, in metres (EPSG:3857).
R: Final = 20037508.342789244


def deg_to_tile(lat: float, lon: float, z: int) -> tuple[int, int]:
    """The XYZ tile containing a lat/lon at zoom ``z``."""
    n = 1 << z
    lat = max(min(lat, 85.0511287798066), -85.0511287798066)
    x = int((lon + 180.0) / 360.0 * n)
    rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(rad)) / math.pi) / 2.0 * n)
    # A point exactly on the eastern/southern edge of the world would index one
    # tile past the end.
    return min(x, n - 1), min(max(y, 0), n - 1)


def tile_bounds_3857(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """``(minx, miny, maxx, maxy)`` of a tile in EPSG:3857 metres.

    Each edge is computed from its own tile index rather than by offsetting the
    opposite edge, so neighbouring tiles derive their shared boundary from the
    identical expression and get the identical float. Writing ``minx + span`` for
    ``maxx`` would be algebraically the same and numerically different, leaving a
    sub-nanometre gap between every pair of tiles.
    """
    n = 1 << z
    span = 2.0 * R / n
    return -R + x * span, R - (y + 1) * span, -R + (x + 1) * span, R - y * span


def tile_children(x: int, y: int, z: int) -> tuple[tuple[int, int, int], ...]:
    """The four tiles at ``z + 1`` that exactly cover this one."""
    return (
        (z + 1, x * 2, y * 2),
        (z + 1, x * 2 + 1, y * 2),
        (z + 1, x * 2, y * 2 + 1),
        (z + 1, x * 2 + 1, y * 2 + 1),
    )


def tile_range(
    lat0: float, lon0: float, lat1: float, lon1: float, z: int
) -> tuple[range, range]:
    """Inclusive x and y ranges covering a bounding box at zoom ``z``."""
    xa, ya = deg_to_tile(lat0, lon0, z)
    xb, yb = deg_to_tile(lat1, lon1, z)
    return range(min(xa, xb), max(xa, xb) + 1), range(min(ya, yb), max(ya, yb) + 1)


def tile_affine(x: int, y: int, z: int) -> tuple[float, float, float, float, float, float]:
    """Shapely-style affine mapping tile-local 0..EXTENT coordinates to EPSG:3857.

    Returned as ``(a, b, d, e, xoff, yoff)`` for ``shapely.affinity.affine_transform``,
    which applies ``x' = a*x + b*y + xoff`` and ``y' = d*x + e*y + yoff``.

    **The y scale is positive, and that is a statement about the decoder, not about
    the MVT spec.** The spec puts the tile origin at the top-left with y increasing
    downward, which would want a negative scale. But ``mapbox_vector_tile.decode``
    defaults to ``y_coord_down=False`` and applies ``y = extent - y`` on the way out,
    so what actually reaches this function is bottom-left origin with y increasing
    upward -- the same direction as EPSG:3857. Matching the spec instead of the
    decoder mirrors every fragment about its own tile's horizontal axis. That failure
    is nasty precisely because it looks fine: at state scale it reads as noise, and it
    only resolves into visibly shredded, banded polygons when you zoom to a single
    local body.

    Scaling is exact: ``EXTENT`` is a power of two, so dividing the tile span by it
    and multiplying back loses nothing. The tile *origin* is still an addition, which
    is why a vertex on a shared edge can disagree with its neighbour by ~1 ULP. See
    the module docstring.
    """
    minx, miny, maxx, maxy = tile_bounds_3857(x, y, z)
    sx = (maxx - minx) / EXTENT
    sy = (maxy - miny) / EXTENT
    return sx, 0.0, 0.0, sy, minx, miny


def mercator_to_wgs84(mx: float, my: float) -> tuple[float, float]:
    """EPSG:3857 metres to ``(lon, lat)`` degrees."""
    lon = mx / R * 180.0
    lat = math.degrees(2.0 * math.atan(math.exp(my / R * math.pi)) - math.pi / 2.0)
    return lon, lat


def metres_per_unit(z: int) -> float:
    """Ground size of one tile-local coordinate unit -- the quantisation floor.

    At z14 this is ~0.6 m, which bounds how far apart two renderings of the same
    shared edge can be, and therefore any seam-closing tolerance.
    """
    return (2.0 * R / (1 << z)) / EXTENT
