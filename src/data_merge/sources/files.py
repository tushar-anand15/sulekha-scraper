"""Loose HTML on disk.

Two collections, both read-only:

* ``raw_html_2010/`` -- 22,863 files mirroring ``lsgd_cache_2010.sqlite``, kept
  so a human can open a page in a browser and see what the parser saw.
* ``sec2010_archive/`` -- three archived 2010 SEC aggregate pages. **Reference
  only, never a validator**: a careful per-district parse gives 9,882 wards,
  46% coverage ranging 12%-98% by district, because the capture is a
  mid-counting "Lead / Won" snapshot rather than a final result.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


def read_html(path: str | Path) -> str:
    """Read an HTML file, tolerating the mixed encodings on disk.

    These pages were saved as fetched. Replacing an undecodable byte loses one
    character; refusing to read loses the page.
    """
    return Path(path).read_text(encoding="utf-8", errors="replace")


@dataclass(frozen=True, slots=True)
class HtmlPage:
    """One file from an HTML directory."""

    path: Path
    html: str

    @property
    def name(self) -> str:
        return self.path.name


class HtmlDirectory:
    """A directory of saved HTML pages, streamed in sorted order."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if not self.root.is_dir():
            raise FileNotFoundError(f"no HTML directory at {self.root}")

    def paths(self, pattern: str = "*.html") -> list[Path]:
        return sorted(self.root.glob(pattern))

    def count(self, pattern: str = "*.html") -> int:
        return sum(1 for _ in self.root.glob(pattern))

    def pages(self, pattern: str = "*.html") -> Iterator[HtmlPage]:
        """Stream pages one at a time -- the 2010 mirror is 813 MB."""
        for path in self.paths(pattern):
            yield HtmlPage(path=path, html=read_html(path))

    def read(self, name: str) -> str:
        return read_html(self.root / name)
