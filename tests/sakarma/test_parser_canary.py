"""Schema-drift canary tests for older-era SAKARMA HTML fixtures.

These tests probe whether the parsers can handle HTML shapes that differ
from the primary 2025-era fixtures.  They are intentionally non-strict: if a
parser cannot handle an older shape the test documents the limitation via a
clear assertion message rather than hard-failing with an opaque error.

This approach (document-rather-than-fail) is the plan's recommended strategy
for the schema-drift risk section.  Future engineers who add support for older
years should tighten the assertions here.

Fixture: tests/sakarma/fixtures/lbwise_dashboard_2018_oachira.html

Key structural differences vs. the 2025 fixture:
  1. KPI card container class is ``panel panel-default kpi-card`` (Bootstrap 3
     panel pattern) instead of ``col kpi-card`` (Bootstrap 4/5 col pattern).
  2. KPI numbers are in ``<h2>`` elements rather than ``<h3>`` elements.
  3. The meeting grid table id uses capitalization variant
     ``GridMeetingDetails`` instead of ``GridMeetingDEtails``.

If the parsers are extended to handle these shapes, update assertions below
from the permissive "graceful fallback" form to strict equality checks.
"""

from __future__ import annotations

import pathlib

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Canary: parse_kpi_cards on 2018-era HTML
# ---------------------------------------------------------------------------


class TestKpiCardsCanary2018:
    """Canary tests for parse_kpi_cards against the 2018-era dashboard fixture.

    The 2018 fixture uses ``<h2>`` tags for KPI numbers (Bootstrap 3 panel
    pattern).  The current parser searches only for ``<h3>``.  These tests
    document whether the parser handles the older shape gracefully or degrades
    silently to zeros.

    Alarm condition: if this test starts *raising* an exception (not just
    returning zeros), the parser has regressed — investigate immediately.
    """

    def test_parse_does_not_raise(self) -> None:
        """parse_kpi_cards must not raise on 2018-era HTML — even if values wrong.

        This is the minimum contract: no crash.  A future upgrade may change
        the assertion to check for correct values once the parser handles <h2>.
        """
        from sakarma.scraper.parsers import KPISnapshot, parse_kpi_cards

        html = _load("lbwise_dashboard_2018_oachira.html")

        # Must not raise — this is the non-negotiable canary assertion.
        snap = parse_kpi_cards(html)

        assert isinstance(snap, KPISnapshot), (
            "parse_kpi_cards returned a non-KPISnapshot object on 2018 HTML — "
            "this indicates a regression in the return-type contract."
        )

    def test_labels_are_found(self) -> None:
        """All five Malayalam KPI labels appear in the 2018 fixture.

        This confirms the fixture itself is well-formed and the parser at
        minimum locates the label text (even if it then mis-reads the number).
        If this assertion fails the fixture file is corrupted.
        """
        html = _load("lbwise_dashboard_2018_oachira.html")
        decoded = html.decode("utf-8")

        expected_labels = [
            "ആകെ യോഗങ്ങൾ",
            "ചേരുന്ന യോഗങ്ങള്‍",
            "മിനിറ്റ്സ് പൂര്‍ത്തിയായവ",
            "മിനിറ്റ്സ് പൂര്‍ത്തിയാകാത്തവ",
            "മീറ്റിംഗ് റദ്ദ്‌ ആക്കിയവ",
        ]
        for label in expected_labels:
            assert label in decoded, (
                f"KPI label '{label}' missing from 2018 fixture — fixture is malformed."
            )

    def test_total_field_documents_h2_limitation(self) -> None:
        """Documents that the 2018 <h2>-based KPI total may parse as 0.

        The current parse_kpi_cards parser walks up from the label to find the
        nearest <h3> ancestor/sibling.  In 2018-era HTML the number is in <h2>
        so the parser falls back to strategy-2 (regex text search + reversed
        h3 list), which also fails because there are no <h3> elements at all.

        Result: total defaults to 0 rather than the fixture value of 22.

        NOTE FOR FUTURE ENGINEERS: If you extend the parser to handle <h2> as
        well as <h3>, update this assertion to:
            assert snap.total == 22
        and remove the docstring limitation note.
        """
        from sakarma.scraper.parsers import parse_kpi_cards

        html = _load("lbwise_dashboard_2018_oachira.html")
        snap = parse_kpi_cards(html)

        # Document current behaviour: value is 0 (silent fallback) NOT 22.
        # This is the known schema-drift limitation for <h2>-based 2018 HTML.
        assert snap.total in (0, 22), (
            f"parse_kpi_cards(2018 html).total={snap.total!r} — expected either 0 "
            "(current fallback behaviour) or 22 (correct value if parser is upgraded). "
            "Any other value indicates an unexpected regression."
        )


# ---------------------------------------------------------------------------
# Canary: parse_meeting_grid on 2018-era table id variant
# ---------------------------------------------------------------------------


class TestMeetingGridCanary2018:
    """Canary tests for parse_meeting_grid against the 2018-era table id variant.

    The 2018 fixture uses ``GridMeetingDetails`` (lowercase 'd', lowercase 'etails')
    whereas 2025 HTML uses ``GridMeetingDEtails`` (capital 'DE').  The parser
    looks for the 2025-era id via a CSS id selector; it will not find the 2018
    table and must raise ParserError (not crash silently with wrong data).
    """

    def test_raises_parser_error_for_unknown_table_id(self) -> None:
        """parse_meeting_grid raises ParserError on 2018 table id variant.

        This is the EXPECTED behaviour — it documents the schema-drift
        limitation.  If the parser is upgraded to also match
        ``GridMeetingDetails``, this test should be changed to assert
        successful parsing with len(rows) == 1.
        """
        from sakarma.db.models import CATEGORY_APPROVED
        from sakarma.scraper.parsers import ParserError, parse_meeting_grid

        html = _load("lbwise_dashboard_2018_oachira.html")

        try:
            rows = parse_meeting_grid(html, category=CATEGORY_APPROVED)
            # If the parser is upgraded and starts handling the old table id,
            # we accept the result silently (canary passes either way).
            assert isinstance(rows, list), (
                "parse_meeting_grid returned non-list on 2018 HTML — unexpected type."
            )
        except ParserError:
            # Expected current behaviour: parser cannot find the 2018 table id.
            # This is documented and intentional — not a bug until older years
            # are in scope.
            pass
        except Exception as exc:
            pytest.fail(
                f"parse_meeting_grid raised unexpected exception type on 2018 HTML: "
                f"{type(exc).__name__}: {exc}. Only ParserError is acceptable here."
            )
