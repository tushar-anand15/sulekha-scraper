"""Path resolution, and the precedence between its three sources."""

from __future__ import annotations

import subprocess
from pathlib import Path

from geo.config import ENV_ROOT, Paths, resolve_paths

REPO = Path(__file__).resolve().parents[2]


def test_defaults_to_the_repo_data_dir(monkeypatch) -> None:
    monkeypatch.delenv(ENV_ROOT, raising=False)
    paths = resolve_paths()
    assert paths.root == REPO / "data"
    for p in (paths.raw, paths.tiles, paths.releases, paths.final, paths.reference):
        assert paths.root in p.parents or p == paths.root


def test_environment_overrides_the_default(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_ROOT, str(tmp_path))
    assert resolve_paths().root == tmp_path.resolve()


def test_explicit_root_beats_the_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv(ENV_ROOT, str(tmp_path / "from-env"))
    other = tmp_path / "from-flag"
    assert resolve_paths(other).root == other.resolve()


def test_every_derived_path_relocates(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path)
    for p in (paths.raw, paths.tiles, paths.releases, paths.final, paths.reference, paths.elections):
        assert str(p).startswith(str(tmp_path.resolve()))


def test_tile_path_layout(tmp_path: Path) -> None:
    paths = resolve_paths(tmp_path)
    assert paths.tile("wb_kerala", 14, 11663, 7737) == (
        paths.tiles / "wb_kerala" / "14" / "11663" / "7737.mvt"
    )


def test_paths_are_frozen(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    try:
        paths.root = tmp_path / "elsewhere"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("Paths should be immutable")


def test_reference_dir_is_committable_but_bulk_data_is_not() -> None:
    """The .gitignore carve-out, asserted where it will be noticed if it regresses.

    Without this, `git add` on a crosswalk silently does nothing and the hand review
    those files exist for never survives a clone. The bulk caches must stay ignored.
    """

    def ignored(rel: str) -> bool:
        return (
            subprocess.run(
                ["git", "check-ignore", "-q", rel], cwd=REPO, capture_output=True
            ).returncode
            == 0
        )

    assert not ignored("data/reference/geo/ksmart_lb_crosswalk.csv")
    assert ignored("data/raw/geo/ksmart/wb_kerala/14/1/1.mvt")
    assert ignored("data/final/geo/wards_2025.geojson")
