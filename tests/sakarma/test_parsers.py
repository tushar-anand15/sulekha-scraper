"""Regression tests for sakarma.scraper.parsers.

Fixtures live in tests/sakarma/fixtures/ and are loaded as bytes so the
parsers exercise the ``from_encoding="utf-8"`` path.
"""

from __future__ import annotations

import pathlib

import pytest

from sakarma.db.models import CATEGORY_APPROVED
from sakarma.scraper.parsers import (
    KPISnapshot,
    ManifestRow,
    ParserError,
    detect_grid_pagination,
    parse_attachment_links,
    parse_dropdown_options,
    parse_kpi_cards,
    parse_meeting_grid,
)
from sakarma.scraper.protocol import DDL_DISTRICT

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------
FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Helpers to lazily load fixture bytes
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def oachira_dashboard_bytes() -> bytes:
    return _load("lbwise_dashboard_oachira_2025_gb.html")


@pytest.fixture(scope="module")
def approved_grid_bytes() -> bytes:
    return _load("approved_grid_oachira_2025_gb.html")


@pytest.fixture(scope="module")
def dregister_bytes() -> bytes:
    return _load("public_dregister_with_attachment.html")


@pytest.fixture(scope="module")
def pager_grid_bytes() -> bytes:
    return _load("grid_with_pager.html")


# ===========================================================================
# parse_kpi_cards
# ===========================================================================
class TestParseKpiCards:
    def test_happy_oachira_2025(self, oachira_dashboard_bytes: bytes) -> None:
        snap = parse_kpi_cards(oachira_dashboard_bytes)
        assert snap == KPISnapshot(
            total=34,
            ongoing=0,
            minutes_complete=34,
            minutes_incomplete=0,
            cancelled=0,
        )

    def test_all_zeros(self) -> None:
        """KPI cards with all zeros should parse cleanly."""
        html = """<!DOCTYPE html><html><body>
        <div class="kpi-card"><h3>0</h3><p>ആകെ യോഗങ്ങൾ</p></div>
        <div class="kpi-card"><h3>0</h3><p>ചേരുന്ന യോഗങ്ങള്‍</p></div>
        <div class="kpi-card"><h3>0</h3><p>മിനിറ്റ്സ് പൂര്‍ത്തിയായവ</p></div>
        <div class="kpi-card"><h3>0</h3><p>മിനിറ്റ്സ് പൂര്‍ത്തിയാകാത്തവ</p></div>
        <div class="kpi-card"><h3>0</h3><p>മീറ്റിംഗ് റദ്ദ്‌ ആക്കിയവ</p></div>
        </body></html>"""
        snap = parse_kpi_cards(html)
        assert snap == KPISnapshot(
            total=0, ongoing=0, minutes_complete=0, minutes_incomplete=0, cancelled=0
        )

    def test_raises_on_wrong_page(self) -> None:
        """A completely different page (no KPI labels) raises ParserError."""
        with pytest.raises(ParserError):
            parse_kpi_cards(b"<html><body><p>Hello world</p></body></html>")


# ===========================================================================
# parse_meeting_grid
# ===========================================================================
class TestParseMeetingGrid:
    def test_happy_approved_grid(self, approved_grid_bytes: bytes) -> None:
        rows = parse_meeting_grid(approved_grid_bytes, category=CATEGORY_APPROVED)
        assert len(rows) == 6

        # All rows should have the correct category
        for row in rows:
            assert row.category == CATEGORY_APPROVED

        # Check select indices 0..5 in order
        for i, row in enumerate(rows):
            assert row.dashboard_grid_select_index == i, (
                f"Row {i}: expected select_index={i}, got "
                f"{row.dashboard_grid_select_index}"
            )

    def test_dr_targets(self, approved_grid_bytes: bytes) -> None:
        rows = parse_meeting_grid(approved_grid_bytes, category=CATEGORY_APPROVED)
        # Row 0 → ctl02, row 1 → ctl03, row 2 → ctl04, etc.
        expected_targets = [
            "ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl02$lnkDR",
            "ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl03$lnkDR",
            "ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl04$lnkDR",
            "ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl05$lnkDR",
            "ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl06$lnkDR",
            "ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl07$lnkDR",
        ]
        for i, (row, expected) in enumerate(zip(rows, expected_targets)):
            assert row.dr_postback_target == expected, (
                f"Row {i}: dr_postback_target mismatch"
            )

    def test_meeting_dates(self, approved_grid_bytes: bytes) -> None:
        rows = parse_meeting_grid(approved_grid_bytes, category=CATEGORY_APPROVED)
        assert rows[0].meeting_date == "15/01/2025"
        assert rows[2].meeting_date == "10/03/2025"
        assert rows[5].meeting_date == "18/06/2025"

    def test_meeting_types_and_natures(self, approved_grid_bytes: bytes) -> None:
        rows = parse_meeting_grid(approved_grid_bytes, category=CATEGORY_APPROVED)
        # All rows are ഭരണസമിതി യോഗം
        for row in rows:
            assert row.meeting_type == "ഭരണസമിതി യോഗം"
        # Row 2 and Row 5 are emergency
        assert rows[2].meeting_nature == "അടിയന്തിര യോഗം/പ്രത്യേക യോഗം"
        assert rows[5].meeting_nature == "അടിയന്തിര യോഗം/പ്രത്യേക യോഗം"
        # Row 0 is regular
        assert rows[0].meeting_nature == "സാധാരണ യോഗം"

    def test_raises_when_table_missing(self) -> None:
        """Completely absent GridMeetingDEtails raises ParserError."""
        with pytest.raises(ParserError):
            parse_meeting_grid(b"<html></html>", category=2)

    def test_skips_row_with_empty_date(self) -> None:
        """Rows where the meeting_date cell is empty are silently skipped."""
        html = """<!DOCTYPE html><html><body>
        <table id="ctl00_ContentPlaceHolder1_GridMeetingDEtails">
          <tr><th>No</th><th>Mtg No</th><th>Date</th><th>Type</th>
              <th>Nature</th><th>Venue</th><th>DR</th><th>Min</th></tr>
          <tr>
            <td>1</td><td>01/2025</td><td></td><td>Type A</td>
            <td>Regular</td><td>Hall</td>
            <td><a href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl02$lnkDR','')">DR</a></td>
            <td><a href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$GridMeetingDEtails','Select$0')">Min</a></td>
          </tr>
          <tr>
            <td>2</td><td>02/2025</td><td>20/02/2025</td><td>Type B</td>
            <td>Emergency</td><td>Hall</td>
            <td><a href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl03$lnkDR','')">DR</a></td>
            <td><a href="javascript:__doPostBack('ctl00$ContentPlaceHolder1$GridMeetingDEtails','Select$1')">Min</a></td>
          </tr>
        </table>
        </body></html>"""
        rows = parse_meeting_grid(html, category=CATEGORY_APPROVED)
        # Row with empty date should be skipped; only 1 row returned
        assert len(rows) == 1
        assert rows[0].meeting_date == "20/02/2025"


# ===========================================================================
# parse_attachment_links
# ===========================================================================
class TestParseAttachmentLinks:
    def test_happy_three_decisions(self, dregister_bytes: bytes) -> None:
        links = parse_attachment_links(dregister_bytes)
        # Decisions 1 has no link; decisions 2 (ctl03) and 3 (ctl04) have links
        assert len(links) == 2

    def test_ctl04_target(self, dregister_bytes: bytes) -> None:
        """The third decision's attachment has the correct postback target."""
        links = parse_attachment_links(dregister_bytes)
        targets = [target for _, target in links]
        assert "GrdDecision$ctl04$lnkFileView" in targets

    def test_empty_link_text_included(self, dregister_bytes: bytes) -> None:
        """Empty-text attachment link (ctl03) is included due to lenient policy."""
        links = parse_attachment_links(dregister_bytes)
        indices = [idx for idx, _ in links]
        assert 3 in indices  # ctl03 = index 3

    def test_no_links_returns_empty(self) -> None:
        html = b"<html><body><table id='GrdDecision'></table></body></html>"
        assert parse_attachment_links(html) == []


# ===========================================================================
# parse_dropdown_options
# ===========================================================================
class TestParseDropdownOptions:
    def test_district_count(self, oachira_dashboard_bytes: bytes) -> None:
        """14 district options (placeholder excluded)."""
        options = parse_dropdown_options(oachira_dashboard_bytes, DDL_DISTRICT)
        assert len(options) == 14

    def test_kollam_is_present(self, oachira_dashboard_bytes: bytes) -> None:
        options = parse_dropdown_options(oachira_dashboard_bytes, DDL_DISTRICT)
        values = [v for v, _ in options]
        assert 2 in values  # Kollam = 2

    def test_placeholder_excluded(self, oachira_dashboard_bytes: bytes) -> None:
        options = parse_dropdown_options(oachira_dashboard_bytes, DDL_DISTRICT)
        values = [v for v, _ in options]
        assert 0 not in values

    def test_missing_dropdown_returns_empty(
        self, oachira_dashboard_bytes: bytes
    ) -> None:
        result = parse_dropdown_options(
            oachira_dashboard_bytes, "ctl00$ContentPlaceHolder1$ddlNonExistent"
        )
        assert result == []


# ===========================================================================
# detect_grid_pagination
# ===========================================================================
class TestDetectGridPagination:
    def test_pager_grid_returns_true(self, pager_grid_bytes: bytes) -> None:
        assert detect_grid_pagination(pager_grid_bytes) is True

    def test_normal_grid_returns_false(self, approved_grid_bytes: bytes) -> None:
        assert detect_grid_pagination(approved_grid_bytes) is False

    def test_empty_html_returns_false(self) -> None:
        assert detect_grid_pagination(b"<html></html>") is False

    def test_page_n_postback_triggers_true(self) -> None:
        """Any Page$N __doPostBack anywhere on the page triggers pagination."""
        html = (
            b"<html><body>"
            b"<a href=\"javascript:__doPostBack('SomeGrid','Page$2')\">2</a>"
            b"</body></html>"
        )
        assert detect_grid_pagination(html) is True

    def test_string_input_works(self, pager_grid_bytes: bytes) -> None:
        """detect_grid_pagination should also accept str input."""
        html_str = pager_grid_bytes.decode("utf-8")
        assert detect_grid_pagination(html_str) is True
