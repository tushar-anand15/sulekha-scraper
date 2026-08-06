"""Quadtree descent over the six KSMART tile layers.

**Descend, don't sweep.** A bounding-box sweep at z14 over Kerala's box is ~25,000
tiles regardless of how much of that box is ocean, Tamil Nadu or Karnataka. A
quadtree descent starts at z8 and only recurses into a tile's four children if that
tile came back non-empty -- so an empty ancestor prunes everything beneath it for the
cost of one request instead of ``4**(14-z)``. This rests on a real property of a tile
pyramid (parent-empty implies child-empty) that was tested, not assumed, over the
Kochi/Vypin islands during planning and held with zero violations once the client
stopped mistaking a gzipped tile for an empty one. It is still only one region --
``geo.build.stitch``'s per-district completeness check is the statewide backstop.

**Six layers, not four.** The four geometry layers -- ``wb_kerala`` (wards),
``lb_kerala`` (local bodies), ``kerala_bp`` and ``kerala_dp`` (block/district
panchayat divisions) -- carry the polygons. ``kerala_bp_with_lsgd`` and
``kerala_dp_with_block`` carry no new geometry but are the only source for which GP
ward sits inside which BP/DP division; the crosswalk verification promised for those
tiers has nothing to check against without them.

**The cache file itself is the "already fetched" marker**, so a resumed run costs
nothing for tiles it already has: if ``Paths.tile(...)`` exists, its size alone says
whether it was a real tile (>0 bytes) or a recorded 204 (0 bytes), and no request is
made either way. Writes are atomic -- temp file, then ``os.replace`` -- because a
partially written tile is indistinguishable from a present one on the next run, and
would leave a silent hole in the map rather than a visible gap to re-fetch.
"""

from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import structlog

from geo.config import Paths
from geo.fetch.client import KsmartClient, TileStatus
from geo.tiles import tile_children, tile_range

logger = structlog.get_logger(__name__)

#: The four geometry layers plus the two tier-membership layers needed for the BP/DP
#: crosswalk verification. See the module docstring.
LAYERS: Final[tuple[str, ...]] = (
    "wb_kerala",
    "lb_kerala",
    "kerala_bp",
    "kerala_dp",
    "kerala_bp_with_lsgd",
    "kerala_dp_with_block",
)

#: The server's own floor -- z7 is 404 for every layer.
MIN_ZOOM: Final = 8

#: The scrape depth chosen in the plan: IoU against z12 converges by z14, and z14 to
#: z16 buys under 0.1% area change for 7.6x the requests.
MAX_ZOOM: Final = 14

#: Scrape depth per layer. Absent layers fall back to :data:`MAX_ZOOM`.
#:
#: Ward polygons are small and carry the detail the map is actually made of, so they
#: get the full depth. Block and District Panchayat *divisions* each span several
#: wards, and a level shallower is a quarter of the requests for a boundary no one
#: can tell apart at any plausible view. The two tier-membership layers are never
#: drawn -- they are read for their attributes, to verify which ward sits in which
#: division -- so they are fetched shallower still.
LAYER_ZOOMS: Final[dict[str, int]] = {
    "wb_kerala": 14,
    "lb_kerala": 14,
    "kerala_bp": 13,
    "kerala_dp": 13,
    "kerala_bp_with_lsgd": 12,
    "kerala_dp_with_block": 12,
}

#: A generous bounding box around Kerala, padded well past the coastline so the
#: descent -- not this box -- is what decides where the state actually is. The z8
#: roots derived from it that turn out to be entirely ocean/Tamil Nadu/Karnataka
#: return 204 on the first request and cost nothing further.
KERALA_BOUNDS: Final[tuple[float, float, float, float]] = (7.9, 74.7, 12.9, 77.6)


@dataclass
class FetchStats:
    """Per-layer counters for one descent. Thread-safe: workers update concurrently."""

    layer: str
    tiles: int = 0
    empty: int = 0
    absent: int = 0
    cached: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, status: TileStatus, *, cached: bool) -> None:
        with self._lock:
            if cached:
                self.cached += 1
                return
            if status is TileStatus.TILE:
                self.tiles += 1
            elif status is TileStatus.EMPTY:
                self.empty += 1
            else:
                self.absent += 1

    @property
    def requested(self) -> int:
        """Tiles that actually hit the network, as opposed to a cache hit."""
        return self.tiles + self.empty + self.absent


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` such that ``path`` never exists half-written.

    A crash or Ctrl-C between opening ``path`` and finishing the write would leave a
    truncated ``.mvt`` file that a resumed run treats as a legitimate cache hit --
    silently punching a hole in the map. Writing to a temp file in the same
    directory and renaming makes the file appear atomically, fully formed or not
    at all; same-directory matters so the rename is same-filesystem.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def _get_or_fetch(
    client: KsmartClient, paths: Paths, layer: str, z: int, x: int, y: int, stats: FetchStats
) -> TileStatus:
    """Serve one tile from cache, or fetch, classify and cache it.

    A 204-empty is recorded as a zero-byte file, which is what lets a resumed run
    skip it without re-requesting -- the file's mere existence is the "already
    fetched" signal; its size distinguishes tile from empty. A 404 (outside the
    z8-z16 range, and not expected within z8-z14) is neither cached nor descended
    into.
    """
    path = paths.tile(layer, z, x, y)
    if path.exists():
        status = TileStatus.TILE if path.stat().st_size > 0 else TileStatus.EMPTY
        stats.record(status, cached=True)
        return status

    result = client.fetch_tile(layer, z, x, y)
    if result.status is TileStatus.TILE:
        _atomic_write(path, result.body)
    elif result.status is TileStatus.EMPTY:
        _atomic_write(path, b"")
    else:
        logger.warning("tile.absent", layer=layer, z=z, x=x, y=y)
    stats.record(result.status, cached=False)
    return result.status


def fetch_layer(
    client: KsmartClient,
    paths: Paths,
    layer: str,
    *,
    bounds: tuple[float, float, float, float] = KERALA_BOUNDS,
    max_workers: int = 4,
    max_zoom: int | None = None,
) -> FetchStats:
    """Quadtree-descend one layer from :data:`MIN_ZOOM` to ``max_zoom``.

    Processes tiles one zoom level at a time: every tile at the current level is
    requested (or served from cache) before any of the next level's tiles are
    queued, since which children exist at all depends on which tiles in this level
    came back non-empty.

    ``max_zoom`` is per-layer because the layers are not alike. Ward polygons are
    small and need :data:`MAX_ZOOM`; a Block Panchayat division spans several wards,
    so a zoom shallower costs a quarter of the requests for detail no one can see.
    The tier-membership layers are read for their attributes and never drawn at all.
    Since each level costs 4x the one above it, this is the difference between a
    ten-minute fetch and an hour of load on someone else's server.
    """
    # Resolved here rather than as a default argument: Python binds defaults once at
    # definition time, so `max_zoom=MAX_ZOOM` would freeze the value and silently
    # ignore anyone -- tests included -- who rebinds the module constant.
    if max_zoom is None:
        max_zoom = MAX_ZOOM
    if not MIN_ZOOM <= max_zoom <= MAX_ZOOM:
        raise ValueError(f"max_zoom must be within {MIN_ZOOM}..{MAX_ZOOM}, got {max_zoom}")
    stats = FetchStats(layer=layer)
    lat0, lon0, lat1, lon1 = bounds
    xs, ys = tile_range(lat0, lon0, lat1, lon1, MIN_ZOOM)
    frontier: list[tuple[int, int, int]] = [(MIN_ZOOM, x, y) for x in xs for y in ys]

    logger.info("layer.start", layer=layer, roots=len(frontier))
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        while frontier:
            futures = {
                pool.submit(_get_or_fetch, client, paths, layer, z, x, y, stats): (z, x, y)
                for (z, x, y) in frontier
            }
            frontier = []
            for future in as_completed(futures):
                z, x, y = futures[future]
                status = future.result()
                if status is TileStatus.TILE and z < max_zoom:
                    frontier.extend(tile_children(x, y, z))

    logger.info(
        "layer.done",
        layer=layer,
        tiles=stats.tiles,
        empty=stats.empty,
        absent=stats.absent,
        cached=stats.cached,
    )
    return stats


def fetch_all_layers(
    client: KsmartClient,
    paths: Paths,
    *,
    layers: Sequence[str] = LAYERS,
    bounds: tuple[float, float, float, float] = KERALA_BOUNDS,
    max_workers: int = 4,
    zooms: Mapping[str, int] | None = None,
) -> dict[str, FetchStats]:
    """Descend every layer in turn, returning per-layer stats.

    Sequential across layers (each is already a five-figure request count on its
    own); concurrency is bounded within a layer via ``max_workers``.

    ``zooms`` overrides the depth per layer; anything absent from it uses
    :data:`MAX_ZOOM`. See :data:`LAYER_ZOOMS` for the defaults and why they differ.
    """
    zooms = dict(zooms or {})
    results: dict[str, FetchStats] = {}
    for layer in layers:
        results[layer] = fetch_layer(
            client,
            paths,
            layer,
            bounds=bounds,
            max_workers=max_workers,
            max_zoom=zooms.get(layer),
        )
    return results
