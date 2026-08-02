"""PDF text extraction, both engines, and the on-disk text cache."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from data_merge.sources import pdf as pdf_source
from data_merge.sources.pdf import PAGE_BREAK, PdfText, extract, have_pdftotext

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))
from tiny_pdf import build_pdf  # noqa: E402

PAGES = [
    ["THIRUVANANTHAPURAM G01001 AMBOORI", "  1 MEENANKAL   INC   1204"],
    ["  2 KOTTOOR      CPI(M) 998", "BLOCK OFFICE WARDInvalid Vote 227"],
]

needs_pdftotext = pytest.mark.skipif(
    not have_pdftotext(), reason="pdftotext is the optional cross-check engine"
)


@pytest.fixture
def report(tmp_path: Path) -> Path:
    path = tmp_path / "candidates_GE9999.pdf"
    path.write_bytes(build_pdf(PAGES))
    return path


class TestExtraction:
    def test_pypdf_returns_one_entry_per_page(self, report: Path) -> None:
        text = extract(report)
        assert text.engine == "pypdf"
        assert len(text.pages) == 2
        assert "AMBOORI" in text.pages[0]
        assert "KOTTOOR" in text.pages[1]

    def test_lines_skips_blanks_and_preserves_leading_space(self, report: Path) -> None:
        lines = list(extract(report).lines())
        assert lines[0] == "THIRUVANANTHAPURAM G01001 AMBOORI"
        assert lines[1].startswith("  1 MEENANKAL"), "column position carries meaning"
        assert "" not in lines

    def test_text_joins_pages_on_a_form_feed(self, report: Path) -> None:
        assert extract(report).text.count(PAGE_BREAK) == 1

    @needs_pdftotext
    def test_both_engines_recover_the_same_content_lines(self, report: Path) -> None:
        """The cross-check that caught nothing in 2010 only because it agreed."""
        by_pypdf = {line.strip() for line in extract(report, engine="pypdf").lines()}
        by_binary = {line.strip() for line in extract(report, engine="pdftotext").lines()}
        assert by_pypdf == by_binary

    def test_an_unknown_engine_is_rejected(self, report: Path) -> None:
        with pytest.raises(ValueError, match="unknown pdf engine"):
            extract(report, engine="ghostscript")

    def test_pdftotext_absent_is_reported_clearly(
        self, report: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pdf_source, "have_pdftotext", lambda: False)
        with pytest.raises(FileNotFoundError, match="have_pdftotext"):
            extract(report, engine="pdftotext")


class TestTextCache:
    def test_a_second_extraction_does_not_re_read_the_pdf(
        self, report: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cache_dir = tmp_path / "pdf_text"
        first = extract(report, cache_dir=cache_dir)

        def fail(_: Path) -> list[str]:
            raise AssertionError("extraction ran again instead of using the cache")

        monkeypatch.setattr(pdf_source, "_extract_pypdf", fail)
        assert extract(report, cache_dir=cache_dir).pages == first.pages

    def test_the_cache_is_keyed_by_content_so_an_edited_pdf_misses_it(
        self, report: Path, tmp_path: Path
    ) -> None:
        cache_dir = tmp_path / "pdf_text"
        extract(report, cache_dir=cache_dir)

        report.write_bytes(build_pdf([["CORRECTED LINE 1"]]))
        assert list(extract(report, cache_dir=cache_dir).lines()) == ["CORRECTED LINE 1"]

    def test_each_engine_caches_separately(self, report: Path, tmp_path: Path) -> None:
        cache_dir = tmp_path / "pdf_text"
        extract(report, engine="pypdf", cache_dir=cache_dir)
        assert len(list(cache_dir.glob("*.pypdf.txt"))) == 1
        assert list(cache_dir.glob("*.pdftotext.txt")) == []

    def test_no_cache_directory_means_no_files_are_written(
        self, report: Path, tmp_path: Path
    ) -> None:
        extract(report)
        assert list(tmp_path.glob("*.txt")) == []


def test_pdftext_is_immutable() -> None:
    """Extraction results are shared between stages; none may edit another's copy."""
    text = PdfText(path=Path("x.pdf"), engine="pypdf", pages=("a",))
    with pytest.raises(AttributeError):
        text.engine = "pdftotext"  # type: ignore[misc]
