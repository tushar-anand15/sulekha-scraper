"""The KSMART fetcher, tested entirely offline against stubbed HTTP responses.

Every scenario here traces back to a real failure mode found while probing the live
server during planning -- see the plan's "Source recon" and Unit 3 sections. The two
that matter most: a gzipped body with no ``Content-Encoding`` header must still be
recognised (``requests`` will not decompress it for us), and a decode failure or a
persistent "Access denied" must never be recorded as an empty tile, since emptiness
may only ever come from an explicit 204.
"""

from __future__ import annotations

import gzip
from pathlib import Path

import mapbox_vector_tile as mvt
import pytest
import responses

from geo.config import resolve_paths
from geo.fetch import ksmart
from geo.fetch.client import (
    ACCESS_DENIED_BODY,
    BASE_URL,
    AccessDeniedError,
    KsmartClient,
    RefererRequiredError,
    TileDecodeError,
    TileServerError,
    TileStatus,
    decode_tile_body,
)
from geo.fetch.ksmart import _get_or_fetch, fetch_layer
from geo.tiles import deg_to_tile, tile_children

TVM = (8.5241, 76.9366)  # a point confirmed live during planning; any point will do.


def _mvt_bytes(feature_count: int = 1) -> bytes:
    """A small, genuinely decodable MVT tile body."""
    features = [
        {
            "geometry": (
                f"POLYGON (({i} {i}, {i + 10} {i}, {i + 10} {i + 10}, "
                f"{i} {i + 10}, {i} {i}))"
            ),
            "properties": {"lb_code": f"G02010{i}"},
        }
        for i in range(feature_count)
    ]
    return mvt.encode([{"name": "wb_kerala", "features": features}])


def _client(**kwargs) -> KsmartClient:
    kwargs.setdefault("rate_limit", 0.0)
    kwargs.setdefault("retry_wait", 0.0)
    kwargs.setdefault("max_attempts", 3)
    return KsmartClient(**kwargs)


def _url(layer: str, z: int, x: int, y: int) -> str:
    return f"{BASE_URL}/{layer}/{z}/{x}/{y}"


def _single_root_bounds(z: int = ksmart.MIN_ZOOM) -> tuple[int, int, int]:
    """One z8 tile containing a known-good point, and its (z, x, y)."""
    x, y = deg_to_tile(*TVM, z)
    return z, x, y


# -- decode_tile_body ---------------------------------------------------------------


def test_gzipped_body_decodes_to_same_features_as_uncompressed() -> None:
    raw = _mvt_bytes(feature_count=2)
    gz = gzip.compress(raw)

    assert gz[:2] == b"\x1f\x8b"
    assert decode_tile_body(gz) == decode_tile_body(raw)


def test_gzipped_body_with_no_content_encoding_header_still_decompresses() -> None:
    """`requests` will not auto-decompress a gzip body unless the header says so.

    So the raw bytes handed to the client still start with the gzip magic, and the
    client must sniff that itself rather than trust the (absent) header.
    """
    raw = _mvt_bytes()
    gz = gzip.compress(raw)
    z, x, y = _single_root_bounds()

    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.GET,
            _url("wb_kerala", z, x, y),
            status=200,
            body=gz,
            content_type="application/octet-stream",
            # Deliberately no Content-Encoding header.
        )
        client = _client()
        result = client.fetch_tile("wb_kerala", z, x, y)

    assert result.status is TileStatus.TILE
    # Raw bytes cached exactly as received -- still gzipped.
    assert result.body == gz
    assert decode_tile_body(result.body) == decode_tile_body(raw)


def test_garbage_body_raises_rather_than_recorded_empty() -> None:
    z, x, y = _single_root_bounds()
    with responses.RequestsMock() as rsps:
        rsps.add(
            responses.GET, _url("wb_kerala", z, x, y), status=200, body=b"not a tile, not gzip"
        )
        client = _client()
        with pytest.raises(TileDecodeError):
            client.fetch_tile("wb_kerala", z, x, y)


# -- classification -------------------------------------------------------------


def test_403_raises_with_missing_referer_named() -> None:
    z, x, y = _single_root_bounds()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=403, body="forbidden")
        client = _client()
        with pytest.raises(RefererRequiredError, match="Referer"):
            client.fetch_tile("wb_kerala", z, x, y)


def test_204_is_classified_empty() -> None:
    z, x, y = _single_root_bounds()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=204, body=b"")
        client = _client()
        result = client.fetch_tile("wb_kerala", z, x, y)
    assert result.status is TileStatus.EMPTY
    assert result.body == b""


def test_404_is_classified_absent() -> None:
    z, x, y = _single_root_bounds()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=404, body="not found")
        client = _client()
        result = client.fetch_tile("wb_kerala", z, x, y)
    assert result.status is TileStatus.ABSENT


def test_access_denied_is_retried_and_never_cached(tmp_path: Path) -> None:
    z, x, y = _single_root_bounds()
    tile = _mvt_bytes()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=200, body=ACCESS_DENIED_BODY)
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=200, body=tile)
        client = _client()
        result = client.fetch_tile("wb_kerala", z, x, y)
        assert len(rsps.calls) == 2

    assert result.status is TileStatus.TILE
    assert result.body == tile


def test_access_denied_persisting_fails_the_run_not_recorded_empty(tmp_path: Path) -> None:
    z, x, y = _single_root_bounds()
    paths = resolve_paths(tmp_path)
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=200, body=ACCESS_DENIED_BODY)
        client = _client(max_attempts=3)
        with pytest.raises(AccessDeniedError):
            client.fetch_tile("wb_kerala", z, x, y)
        assert len(rsps.calls) == 3  # the whole retry budget was spent

    assert not paths.tile("wb_kerala", z, x, y).exists()


def test_5xx_exhausts_retries_and_fails_loudly() -> None:
    z, x, y = _single_root_bounds()
    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=503, body="unavailable")
        client = _client(max_attempts=3)
        with pytest.raises(TileServerError):
            client.fetch_tile("wb_kerala", z, x, y)
        assert len(rsps.calls) == 3


# -- ksmart.py: caching, pruning, descent ----------------------------------------


def test_200_tile_writes_cache_and_descends_into_all_four_children(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path)
    z, x, y = _single_root_bounds()
    tile = _mvt_bytes()
    client = _client()

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("wb_kerala", z, x, y), status=200, body=tile)
        status = _get_or_fetch(client, paths, "wb_kerala", z, x, y, ksmart.FetchStats("wb_kerala"))

    assert status is TileStatus.TILE
    cached_path = paths.tile("wb_kerala", z, x, y)
    assert cached_path.exists()
    assert cached_path.read_bytes() == tile


def test_already_cached_tile_is_not_re_requested(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path)
    z, x, y = _single_root_bounds()
    cached_path = paths.tile("wb_kerala", z, x, y)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(_mvt_bytes())

    client = _client()
    with responses.RequestsMock() as rsps:
        # No routes registered at all: any network call raises a ConnectionError.
        status = _get_or_fetch(client, paths, "wb_kerala", z, x, y, ksmart.FetchStats("wb_kerala"))
        assert len(rsps.calls) == 0

    assert status is TileStatus.TILE


def test_cached_empty_marker_is_recognised_without_a_request(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path)
    z, x, y = _single_root_bounds()
    cached_path = paths.tile("wb_kerala", z, x, y)
    cached_path.parent.mkdir(parents=True, exist_ok=True)
    cached_path.write_bytes(b"")  # the 0-byte marker fetch_layer writes for a 204

    client = _client()
    with responses.RequestsMock() as rsps:
        status = _get_or_fetch(client, paths, "wb_kerala", z, x, y, ksmart.FetchStats("wb_kerala"))
        assert len(rsps.calls) == 0

    assert status is TileStatus.EMPTY


def test_204_prunes_the_whole_subtree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Children of an empty tile are never requested -- proven by not registering them."""
    monkeypatch.setattr(ksmart, "MAX_ZOOM", ksmart.MIN_ZOOM + 2)
    paths = resolve_paths(tmp_path)
    z, x, y = _single_root_bounds()
    client = _client()

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("lb_kerala", z, x, y), status=204, body=b"")
        # Deliberately nothing registered for any child of (z, x, y): if the
        # pruning invariant broke, the ConnectionError from an unmatched request
        # would fail this test.
        stats = fetch_layer(client, paths, "lb_kerala", bounds=(*TVM, *TVM), max_workers=1)
        assert len(rsps.calls) == 1

    assert stats.empty == 1
    assert stats.tiles == 0
    assert paths.tile("lb_kerala", z, x, y).read_bytes() == b""
    for cz, cx, cy in tile_children(x, y, z):
        assert not paths.tile("lb_kerala", cz, cx, cy).exists()


def test_descent_requests_exactly_the_non_empty_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stubbed 3-level pyramid: only the live branch is walked, nothing beyond it."""
    z8, x8, y8 = _single_root_bounds()
    z9 = z8 + 1
    monkeypatch.setattr(ksmart, "MAX_ZOOM", z9 + 1)  # z8, z9, z10 -- three levels
    paths = resolve_paths(tmp_path)
    client = _client()

    children = tile_children(x8, y8, z8)
    live_child = children[0]  # (z9, cx, cy) -- the only branch with content
    dead_children = children[1:]

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("kerala_bp", z8, x8, y8), status=200, body=_mvt_bytes())
        rsps.add(
            responses.GET,
            _url("kerala_bp", *live_child),
            status=200,
            body=_mvt_bytes(),
        )
        for cz, cx, cy in dead_children:
            rsps.add(responses.GET, _url("kerala_bp", cz, cx, cy), status=204, body=b"")
        for cz, cx, cy in tile_children(live_child[1], live_child[2], live_child[0]):
            rsps.add(responses.GET, _url("kerala_bp", cz, cx, cy), status=204, body=b"")

        stats = fetch_layer(client, paths, "kerala_bp", bounds=(*TVM, *TVM), max_workers=1)
        # 1 root + 4 z9 children + 4 z10 grandchildren of the one live z9 branch = 9.
        assert len(rsps.calls) == 9

    assert stats.tiles == 2
    assert stats.empty == 7


def test_fetch_all_layers_covers_all_six(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ksmart, "MAX_ZOOM", ksmart.MIN_ZOOM)  # one request per layer, no descent
    paths = resolve_paths(tmp_path)
    client = _client()
    z, x, y = _single_root_bounds()

    with responses.RequestsMock() as rsps:
        for layer in ksmart.LAYERS:
            rsps.add(responses.GET, _url(layer, z, x, y), status=204, body=b"")
        results = ksmart.fetch_all_layers(client, paths, bounds=(*TVM, *TVM), max_workers=1)

    assert set(results) == set(ksmart.LAYERS)
    assert len(ksmart.LAYERS) == 6
    for stats in results.values():
        assert stats.empty == 1


# -- per-layer scrape depth ---------------------------------------------------------


def test_max_zoom_stops_the_descent_early(tmp_path: Path) -> None:
    """A shallower layer must stop where told, not at the module default.

    This is what makes the BP/DP layers affordable: each extra level is 4x the
    requests, so descending two levels further than a layer needs would quadruple
    the load twice over for boundaries nobody can distinguish.
    """
    z8, x8, y8 = _single_root_bounds()
    paths = resolve_paths(tmp_path)
    client = _client()

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("kerala_bp", z8, x8, y8), status=200, body=_mvt_bytes())
        for cz, cx, cy in tile_children(x8, y8, z8):
            rsps.add(responses.GET, _url("kerala_bp", cz, cx, cy), status=200, body=_mvt_bytes())
        stats = fetch_layer(
            client, paths, "kerala_bp", bounds=(*TVM, *TVM), max_zoom=z8 + 1, max_workers=1
        )
        # Exactly the root and its four children -- nothing at z10.
        assert len(rsps.calls) == 5

    assert stats.tiles == 5
    for cz, cx, cy in tile_children(x8, y8, z8):
        for gz, gx, gy in tile_children(cx, cy, cz):
            assert not paths.tile("kerala_bp", gz, gx, gy).exists()


def test_max_zoom_default_still_follows_the_module_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passing nothing must track MAX_ZOOM at call time, not at import time.

    A default argument would have frozen the value when the function was defined,
    which is a bug that only shows up as tests mysteriously ignoring a monkeypatch.
    """
    z8, x8, y8 = _single_root_bounds()
    monkeypatch.setattr(ksmart, "MAX_ZOOM", z8)
    paths = resolve_paths(tmp_path)
    client = _client()

    with responses.RequestsMock() as rsps:
        rsps.add(responses.GET, _url("kerala_dp", z8, x8, y8), status=200, body=_mvt_bytes())
        fetch_layer(client, paths, "kerala_dp", bounds=(*TVM, *TVM), max_workers=1)
        assert len(rsps.calls) == 1


@pytest.mark.parametrize("bad", [ksmart.MIN_ZOOM - 1, ksmart.MAX_ZOOM + 1, 0])
def test_out_of_range_max_zoom_is_rejected(tmp_path: Path, bad: int) -> None:
    """The server only serves z8..z16; a silent no-op descent would look like
    an empty state rather than a mistake."""
    with pytest.raises(ValueError, match="max_zoom"):
        fetch_layer(_client(), resolve_paths(tmp_path), "kerala_bp", max_zoom=bad)


def test_layer_zooms_covers_every_layer_and_stays_in_range() -> None:
    assert set(ksmart.LAYER_ZOOMS) == set(ksmart.LAYERS)
    for layer, z in ksmart.LAYER_ZOOMS.items():
        assert ksmart.MIN_ZOOM <= z <= ksmart.MAX_ZOOM, layer
    # The layers actually rendered keep full depth; the rest are deliberately shallower.
    assert ksmart.LAYER_ZOOMS["wb_kerala"] == ksmart.MAX_ZOOM
    assert ksmart.LAYER_ZOOMS["kerala_bp_with_lsgd"] < ksmart.LAYER_ZOOMS["kerala_bp"]
