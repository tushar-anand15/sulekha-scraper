"""Data-root resolution.

Inputs are read-only and live under ``data/raw`` and ``data/reference``;
everything the pipeline produces lands under ``data/final``. The root is
overridable so a run can point at a copy without editing code:

    DATA_MERGE_ROOT=/scratch/elections uv run data-merge paths
    uv run data-merge --root /scratch/elections paths
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

ENV_ROOT: Final = "DATA_MERGE_ROOT"

_REPO_ROOT: Final = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Paths:
    """Resolved locations for one run."""

    root: Path

    @property
    def raw(self) -> Path:
        return self.root / "raw"

    @property
    def caches(self) -> Path:
        return self.raw / "caches"

    @property
    def sec_pdfs(self) -> Path:
        return self.raw / "sec_pdfs"

    @property
    def raw_html_2010(self) -> Path:
        return self.raw / "raw_html_2010"

    @property
    def sec2010_archive(self) -> Path:
        return self.raw / "sec2010_archive"

    @property
    def reference(self) -> Path:
        return self.root / "reference"

    @property
    def final(self) -> Path:
        return self.root / "final"

    def year_dir(self, year: int) -> Path:
        return self.final / str(year)

    def candidates_csv(self, year: int) -> Path:
        return self.year_dir(year) / f"candidates_{year}.csv"

    def wards_csv(self, year: int) -> Path:
        return self.year_dir(year) / f"wards_{year}.csv"

    def local_bodies_csv(self, year: int) -> Path:
        return self.year_dir(year) / f"local_bodies_{year}.csv"

    def manifest_json(self, year: int) -> Path:
        return self.year_dir(year) / f"manifest_{year}.json"

    def missing_inputs(self) -> list[Path]:
        """Input directories that do not exist. Empty means the run can start."""
        return [p for p in (self.raw, self.caches, self.sec_pdfs, self.reference) if not p.is_dir()]


def resolve_paths(root: str | os.PathLike[str] | None = None) -> Paths:
    """Resolve the data root: explicit argument, then env var, then ``<repo>/data``."""
    chosen = root if root is not None else os.environ.get(ENV_ROOT)
    return Paths(root=Path(chosen).expanduser().resolve() if chosen else _REPO_ROOT / "data")
