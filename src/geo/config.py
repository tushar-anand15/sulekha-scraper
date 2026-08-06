"""Path resolution for a geometry run.

Mirrors ``data_merge.config``: one frozen dataclass, one environment override, so a
run can be pointed at a copy without editing code::

    GEO_ROOT=/scratch/geo uv run geo paths
    uv run geo --root /scratch/geo paths

The split that matters here is not raw-vs-final but *who may write what*. Tiles under
``raw/`` are fetched once and then treated as the source of record -- a build reads
them and must never re-fetch, because a fetch that succeeds is indistinguishable from
a cache hit and would quietly destroy reproducibility. ``reference/`` is the odd one
out: hand-maintained input, checked into git (see the carve-out in .gitignore), because
the crosswalks there are only worth anything if a human reviewed them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ENV_ROOT: Final = "GEO_ROOT"

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Paths:
    """Resolved locations for one run."""

    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw" / "geo"

    @property
    def tiles(self) -> Path:
        """Cached vector tiles, one file per ``{layer}/{z}/{x}/{y}``."""
        return self.raw / "ksmart"

    @property
    def releases(self) -> Path:
        """Downloaded release assets (the opendatakerala GeoJSON and friends)."""
        return self.raw / "opendatakerala"

    @property
    def final(self) -> Path:
        return self.root / "final" / "geo"

    @property
    def reference(self) -> Path:
        """Hand-maintained crosswalks and overrides. Committed, unlike everything else."""
        return self.root / "reference" / "geo"

    @property
    def elections(self) -> Path:
        """``data_merge``'s outputs -- read-only input to this package."""
        return self.root / "final"

    def tile(self, layer: str, z: int, x: int, y: int) -> Path:
        """Where one tile lives on disk.

        The ``.mvt`` suffix is a label for humans; the bytes may be gzipped, since
        the server compresses intermittently and the cache stores what it was given.
        """
        return self.tiles / layer / str(z) / str(x) / f"{y}.mvt"


def resolve_paths(root: str | Path | None = None) -> Paths:
    """CLI flag beats environment beats the repo's own ``data/``."""
    if root is not None:
        return Paths(Path(root).expanduser().resolve())
    env = os.environ.get(ENV_ROOT)
    if env:
        return Paths(Path(env).expanduser().resolve())
    return Paths(_REPO_ROOT / "data")
