"""Text out of the SEC candidate reports.

Two engines, deliberately:

* **pypdf** is primary -- pure Python, no binary dependency, so a fresh clone
  can always build.
* **pdftotext -layout** runs as a cross-check where the binary exists. During
  the 2010 build the two engines agreed on the ward set exactly, at 21,648.
  Disagreement means one of them is mis-reading a fixed-width report, which is
  a hard failure rather than a warning.

Extraction is the slow step -- ~35 s for the 1,740-page 2010 report -- so
results are cached on disk under ``<root>/interim/pdf_text``, keyed by the
PDF's own SHA-256. A changed PDF therefore misses the cache automatically;
there is no stale-cache failure mode to remember.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from data_merge.io.manifest import sha256_file

PAGE_BREAK = "\f"

_PDFTOTEXT_TIMEOUT = 600


def have_pdftotext() -> bool:
    """Whether the cross-check engine is available on this machine."""
    return shutil.which("pdftotext") is not None


@dataclass(frozen=True, slots=True)
class PdfText:
    """Extracted text for one report, with the engine that produced it named."""

    path: Path
    engine: str
    pages: tuple[str, ...]

    @property
    def text(self) -> str:
        return PAGE_BREAK.join(self.pages)

    def lines(self) -> Iterator[str]:
        """Every non-blank line, in document order, stripped of trailing space.

        Leading space is preserved: these are fixed-width reports and column
        position carries meaning.
        """
        for page in self.pages:
            for line in page.splitlines():
                stripped = line.rstrip()
                if stripped.strip():
                    yield stripped


def extract(
    path: str | Path,
    *,
    engine: str = "pypdf",
    cache_dir: str | Path | None = None,
) -> PdfText:
    """Extract ``path`` with ``engine``, using the on-disk text cache if given.

    ``engine`` is ``"pypdf"`` or ``"pdftotext"``.
    """
    source = Path(path)
    if engine not in ("pypdf", "pdftotext"):
        raise ValueError(f"unknown pdf engine {engine!r}")

    cached_at = _cache_path(source, engine, cache_dir)
    if cached_at is not None and cached_at.exists():
        return PdfText(
            path=source,
            engine=engine,
            pages=tuple(cached_at.read_text(encoding="utf-8").split(PAGE_BREAK)),
        )

    pages = _extract_pypdf(source) if engine == "pypdf" else _extract_pdftotext(source)
    if cached_at is not None:
        cached_at.parent.mkdir(parents=True, exist_ok=True)
        cached_at.write_text(PAGE_BREAK.join(pages), encoding="utf-8")
    return PdfText(path=source, engine=engine, pages=tuple(pages))


def _extract_pypdf(path: Path) -> list[str]:
    reader = PdfReader(str(path))
    return [page.extract_text() or "" for page in reader.pages]


def _extract_pdftotext(path: Path) -> list[str]:
    if not have_pdftotext():
        raise FileNotFoundError(
            "pdftotext is not on PATH; it is the optional cross-check engine, "
            "so callers should check have_pdftotext() first"
        )
    result = subprocess.run(  # noqa: S603 -- fixed argv, no shell, path from our config
        ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
        capture_output=True,
        timeout=_PDFTOTEXT_TIMEOUT,
        check=True,
    )
    return result.stdout.decode("utf-8", errors="replace").split(PAGE_BREAK)


def _cache_path(source: Path, engine: str, cache_dir: str | Path | None) -> Path | None:
    """Where extracted text for this exact PDF lives, or ``None`` if uncached.

    Keyed by content hash, not by name: two runs pointed at different copies of
    the same report share a cache entry, and an edited report never reuses one.
    """
    if cache_dir is None:
        return None
    return Path(cache_dir) / f"{source.stem}.{sha256_file(source)[:16]}.{engine}.txt"
