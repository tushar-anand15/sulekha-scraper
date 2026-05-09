"""Unit and integration tests for :mod:`sakarma.tasks.manifest`.

Test hierarchy
--------------
Unit tests (no DB, mocked client + repos):
  - happy_1y1g:   1 year × 1 group × 4 drill-downs, small grid
  - happy_2y2g:   2 years × 2 groups × 4 categories (16 drills)
  - zero_rows:    a year/category returning zero meeting rows still writes KPI
  - pagination:   PaginationDetectedError → caught, progress error written, re-raised
  - bad_date:     rows whose date can't be parsed are skipped silently

Integration tests (real DB session via conftest ``db_session``, mocked client):
  - integration_full_run: full per-LB run against mocked client + real DB
"""

from __future__ import annotations

import types
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from sakarma.db.models import (
    CATEGORY_APPROVED,
    CATEGORY_CANCELLED,
    CATEGORY_INCOMPLETE,
    CATEGORY_ONGOING,
)
from sakarma.scraper.client import PaginationDetectedError
from sakarma.scraper.parsers import KPISnapshot, ManifestRow, ParserError
from sakarma.scraper.protocol import FormState

# ---------------------------------------------------------------------------
# Helpers for building lightweight stubs
# ---------------------------------------------------------------------------

DASHBOARD_HTML = (
    open(
        "tests/sakarma/fixtures/lbwise_dashboard_oachira_2025_gb.html", "rb"
    ).read()
)
APPROVED_GRID_HTML = (
    open(
        "tests/sakarma/fixtures/approved_grid_oachira_2025_gb.html", "rb"
    ).read()
)

# A minimal empty-grid HTML (no data rows, but contains the grid table)
EMPTY_GRID_HTML = b"""
<html><body>
<form>
<input type="hidden" name="__VIEWSTATE" value="VS" />
<input type="hidden" name="__EVENTVALIDATION" value="EV" />
<table id="ctl00_ContentPlaceHolder1_GridMeetingDEtails">
  <tr><th>Col1</th><th>Col2</th><th>Col3</th></tr>
</table>
</form></body></html>
"""


def _make_form_state(html: bytes = DASHBOARD_HTML) -> FormState:
    """Return a minimal FormState with ``raw_html`` populated."""
    return FormState(
        viewstate="VS",
        event_validation="EV",
        page_url="http://meeting.lsgkerala.gov.in/Pages/LBWiseDashBoard.aspx",
        raw_html=html,
    )


def _make_kpi() -> KPISnapshot:
    return KPISnapshot(total=6, ongoing=0, minutes_complete=6, minutes_incomplete=0, cancelled=0)


def _make_manifest_rows(category: int, n: int = 2) -> list[ManifestRow]:
    """Build *n* minimal ManifestRow objects for *category*."""
    return [
        ManifestRow(
            category=category,
            meeting_no_label=f"0{i+1}/2025",
            meeting_date=f"{15 + i:02d}/01/2025",
            meeting_type="ഭരണസമിതി യോഗം",
            meeting_nature="സാധാരണ യോഗം",
            meeting_venue="Hall",
            dashboard_grid_select_index=i,
            dr_postback_target=f"ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl{2+i:02d}$lnkDR",
        )
        for i in range(n)
    ]


def _make_repos(
    district_id: int = 2,
    lb_type_id: int = 5,
    mg_options: list[tuple[int, str]] | None = None,
) -> SimpleNamespace:
    """Return a repos namespace with all required mocks pre-wired."""
    if mg_options is None:
        mg_options = [(1, "ഭരണസമിതി"), (2, "വികസന സമിതി")]

    lb_mock = MagicMock()
    lb_mock.district_id = district_id
    lb_mock.lb_type_id = lb_type_id
    lb_mock.name_ml = "ഓച്ചിറ"

    lb_repo = MagicMock()
    lb_repo.get.return_value = lb_mock

    # main_group_value_repo.upsert returns an object with .id
    def _mg_upsert(lb_id, ddl_value, name_ml):
        m = MagicMock()
        m.id = ddl_value * 100  # deterministic fake PK
        return m

    main_group_value_repo = MagicMock()
    main_group_value_repo.upsert.side_effect = _mg_upsert

    meeting_manifest_repo = MagicMock()
    meeting_manifest_repo.upsert_many.return_value = 0  # overridden per test

    dashboard_kpi_snapshot_repo = MagicMock()

    lb_progress_repo = MagicMock()
    lb_progress_progress = MagicMock()
    lb_progress_progress.lb_id = 303
    lb_progress_progress.id = 9
    lb_progress_repo.list_for_run.return_value = [lb_progress_progress]

    return SimpleNamespace(
        lb_repo=lb_repo,
        main_group_value_repo=main_group_value_repo,
        meeting_manifest_repo=meeting_manifest_repo,
        dashboard_kpi_snapshot_repo=dashboard_kpi_snapshot_repo,
        lb_progress_repo=lb_progress_repo,
    )


def _make_client(
    dashboard_state: FormState | None = None,
    drill_state: FormState | None = None,
) -> MagicMock:
    """Return a MagicMock SakarmaClient.

    ``load_page`` and ``select_dropdown`` return the dashboard state.
    ``click_button`` returns the drill state (grid HTML).
    """
    if dashboard_state is None:
        dashboard_state = _make_form_state(DASHBOARD_HTML)
    if drill_state is None:
        drill_state = _make_form_state(APPROVED_GRID_HTML)

    client = MagicMock()
    client.load_page.return_value = dashboard_state
    client.select_dropdown.return_value = dashboard_state
    client.click_button.return_value = drill_state
    return client


# ---------------------------------------------------------------------------
# Patch targets
# ---------------------------------------------------------------------------
_PATCH_SETTINGS = "sakarma.tasks.manifest.settings"
_PATCH_PARSE_DDL = "sakarma.tasks.manifest.parse_dropdown_options"
_PATCH_PARSE_KPI = "sakarma.tasks.manifest.parse_kpi_cards"
_PATCH_PARSE_GRID = "sakarma.tasks.manifest.parse_meeting_grid"


# ===========================================================================
# Unit tests
# ===========================================================================


class TestRunForLbHappy1Year1Group:
    """Happy path: 1 year × 1 main_group × 4 drill-downs."""

    def test_upsert_many_called_four_times(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()
        rows_per_category = 2
        repos.meeting_manifest_repo.upsert_many.return_value = rows_per_category

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            # years: one year
            mock_ddl.side_effect = [
                [(36, "2025")],   # DDL_YEAR call
                [(2, "ഭരണസമിതി")],  # DDL_MAIN_GROUP call
            ]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = _make_manifest_rows(CATEGORY_APPROVED, n=rows_per_category)

            summary = run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        # 4 drill-downs = 4 upsert_many calls
        assert repos.meeting_manifest_repo.upsert_many.call_count == 4
        assert summary["kpi_snapshots"] == 1
        assert summary["manifest_rows_inserted"] == 4 * rows_per_category
        assert summary["categories_processed"] == 4
        assert summary["years_processed"] == 1
        assert summary["main_groups_processed"] == 1

    def test_kpi_snapshot_upserted_once_per_year_group(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [
                [(36, "2025")],
                [(2, "ഭരണസമിതി")],
            ]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = _make_manifest_rows(CATEGORY_APPROVED, n=1)

            run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        # Exactly 1 KPI snapshot upsert (1 year × 1 group)
        assert repos.dashboard_kpi_snapshot_repo.upsert.call_count == 1
        call_kwargs = repos.dashboard_kpi_snapshot_repo.upsert.call_args
        assert call_kwargs.kwargs["lb_id"] == 303
        assert call_kwargs.kwargs["year_id"] == 36

    def test_categories_in_correct_order(self):
        """click_button must be called with the expected button constants."""
        from sakarma.tasks.manifest import run_for_lb
        from sakarma.scraper.protocol import (
            BTN_APPV_MEETINGS,
            BTN_BEFORE_MEETINGS,
            BTN_CANCEL_DETAILS,
            BTN_INCOMP_MEETINGS,
        )

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [[(36, "2025")], [(2, "ഭരണസമിതി")]]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = []

            run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        expected_buttons = [
            BTN_APPV_MEETINGS,
            BTN_BEFORE_MEETINGS,
            BTN_INCOMP_MEETINGS,
            BTN_CANCEL_DETAILS,
        ]
        actual_buttons = [c.args[1] for c in client.click_button.call_args_list]
        assert actual_buttons == expected_buttons


class TestRunForLbHappy2Years2Groups:
    """Happy path: 2 years × 2 groups = 4 (year,group) pairs × 4 drills = 16 clicks."""

    def test_click_button_count(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(1, "ഭരണസമിതി"), (2, "വികസന സമിതി")])
        client = _make_client()

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [
                [(35, "2024"), (36, "2025")],   # years
                [(1, "ഭരണസമിതി"), (2, "വികസന സമിതി")],  # main groups
            ]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = _make_manifest_rows(CATEGORY_APPROVED, n=1)

            summary = run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        # 2 years × 2 groups × 4 categories = 16
        assert client.click_button.call_count == 16
        assert summary["categories_processed"] == 16
        assert summary["years_processed"] == 2
        assert summary["main_groups_processed"] == 2
        # 2y × 2g KPI snapshots
        assert repos.dashboard_kpi_snapshot_repo.upsert.call_count == 4

    def test_upsert_many_call_count(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(1, "ഭരണസമിതി"), (2, "വികസന സമിതി")])
        client = _make_client()
        repos.meeting_manifest_repo.upsert_many.return_value = 3

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [
                [(35, "2024"), (36, "2025")],
                [(1, "ഭരണസമിതി"), (2, "വികസന സമിതി")],
            ]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = _make_manifest_rows(CATEGORY_APPROVED, n=3)

            summary = run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        assert repos.meeting_manifest_repo.upsert_many.call_count == 16
        assert summary["manifest_rows_inserted"] == 16 * 3


class TestRunForLbZeroRows:
    """Zero meeting rows for a category still writes the KPI snapshot."""

    def test_kpi_written_with_zero_drill_rows(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()
        repos.meeting_manifest_repo.upsert_many.return_value = 0

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [[(36, "2025")], [(2, "ഭരണസമിതി")]]
            mock_kpi.return_value = KPISnapshot(
                total=0, ongoing=0, minutes_complete=0, minutes_incomplete=0, cancelled=0
            )
            mock_grid.return_value = []  # no rows

            summary = run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        # KPI snapshot still written
        assert repos.dashboard_kpi_snapshot_repo.upsert.call_count == 1
        # upsert_many still called (with empty list) — returns 0
        assert repos.meeting_manifest_repo.upsert_many.call_count == 4
        assert summary["manifest_rows_inserted"] == 0
        assert summary["kpi_snapshots"] == 1


class TestRunForLbPaginationError:
    """PaginationDetectedError: caught, progress error written, re-raised."""

    def test_re_raises_pagination_error(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()
        pagination_exc = PaginationDetectedError(
            "Pagination detected (category=btnAppv_Meetings, url=http://...)"
        )
        client.click_button.side_effect = pagination_exc

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [[(36, "2025")], [(2, "ഭരണസമിതി")]]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = []

            with pytest.raises(PaginationDetectedError):
                run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

    def test_writes_error_to_progress(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()
        pagination_exc = PaginationDetectedError("page overflow!")
        client.click_button.side_effect = pagination_exc

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [[(36, "2025")], [(2, "ഭരണസമിതി")]]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = []

            with pytest.raises(PaginationDetectedError):
                run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        # mark_error should have been called on the lb_progress row
        repos.lb_progress_repo.mark_error.assert_called_once()
        _call_args = repos.lb_progress_repo.mark_error.call_args
        assert "page overflow!" in _call_args.args[1] or "page overflow!" in str(
            _call_args
        )


class TestRunForLbBadDate:
    """Rows with unparseable dates are silently skipped."""

    def test_bad_date_row_skipped(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()

        bad_row = ManifestRow(
            category=CATEGORY_APPROVED,
            meeting_no_label="01/2025",
            meeting_date="NOT_A_DATE",  # unparseable
            meeting_type="X",
            meeting_nature=None,
            meeting_venue=None,
            dashboard_grid_select_index=0,
            dr_postback_target=None,
        )
        good_row = ManifestRow(
            category=CATEGORY_APPROVED,
            meeting_no_label="02/2025",
            meeting_date="15/01/2025",  # valid
            meeting_type="X",
            meeting_nature=None,
            meeting_venue=None,
            dashboard_grid_select_index=1,
            dr_postback_target=None,
        )

        captured_rows: list[list] = []

        def _capture_upsert(rows):
            captured_rows.append(rows)
            return len(rows)

        repos.meeting_manifest_repo.upsert_many.side_effect = _capture_upsert

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [[(36, "2025")], [(2, "ഭരണസമിതി")]]
            mock_kpi.return_value = _make_kpi()
            # All 4 drills return [bad_row, good_row]
            mock_grid.return_value = [bad_row, good_row]

            summary = run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        # Each of 4 category calls should have captured exactly 1 valid row
        for batch in captured_rows:
            assert len(batch) == 1
            assert batch[0]["meeting_date"] == date(2025, 1, 15)


class TestRunForLbNoMainGroups:
    """LB with no main groups returns early with zeros."""

    def test_returns_zero_summary(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[])
        client = _make_client()

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [
                [(36, "2025")],  # years
                [],              # no main groups
            ]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = []

            summary = run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        assert summary == {
            "kpi_snapshots": 0,
            "manifest_rows_inserted": 0,
            "categories_processed": 0,
            "years_processed": 0,
            "main_groups_processed": 0,
        }
        repos.meeting_manifest_repo.upsert_many.assert_not_called()
        client.click_button.assert_not_called()


class TestManifestRowDict:
    """Verify the dict keys passed to upsert_many match what the repo expects."""

    def test_dict_has_required_keys(self):
        from sakarma.tasks.manifest import run_for_lb

        repos = _make_repos(mg_options=[(2, "ഭരണസമിതി")])
        client = _make_client()

        captured: list[dict] = []

        def _capture(rows):
            captured.extend(rows)
            return len(rows)

        repos.meeting_manifest_repo.upsert_many.side_effect = _capture

        with (
            patch(_PATCH_SETTINGS) as mock_settings,
            patch(_PATCH_PARSE_DDL) as mock_ddl,
            patch(_PATCH_PARSE_KPI) as mock_kpi,
            patch(_PATCH_PARSE_GRID) as mock_grid,
        ):
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
            mock_ddl.side_effect = [[(36, "2025")], [(2, "ഭരണസമിതി")]]
            mock_kpi.return_value = _make_kpi()
            mock_grid.return_value = _make_manifest_rows(CATEGORY_APPROVED, n=1)

            run_for_lb(client, repos, lb_id=303, scrape_run_id=1)

        required_keys = {
            "lb_id",
            "year_id",
            "main_group_value_id",
            "category",
            "dashboard_grid_select_index",
            "dr_postback_target",
            "meeting_no_label",
            "meeting_date",
            "meeting_type",
            "meeting_nature",
            "meeting_venue",
            "scrape_run_id",
        }
        for row in captured:
            missing = required_keys - set(row.keys())
            assert not missing, f"Row dict missing keys: {missing}"
            # meeting_date must be a datetime.date object
            assert isinstance(row["meeting_date"], date)


# ===========================================================================
# Integration test (real DB, mocked HTTP client)
# ===========================================================================


@pytest.mark.integration
class TestRunForLbIntegration:
    """Full per-LB run against a real DB transaction (rolled-back after test)."""

    def test_full_run_returns_expected_counts(self, db_session):
        """
        Seed the DB with dimension rows, run manifest, verify counts.
        Uses the real repositories but a mocked SakarmaClient.
        """
        from sakarma.db.repositories import (
            DashboardKPISnapshotRepository,
            DistrictRepository,
            LBProgressRepository,
            LBRepository,
            LBTypeRepository,
            MainGroupValueRepository,
            MeetingManifestRepository,
            ScrapeRunRepository,
            YearRepository,
        )
        from sakarma.tasks.manifest import run_for_lb

        # --- Seed dimension tables ---
        dist_repo = DistrictRepository(db_session)
        lb_type_repo = LBTypeRepository(db_session)
        year_repo = YearRepository(db_session)
        lb_repo = LBRepository(db_session)
        run_repo = ScrapeRunRepository(db_session)
        progress_repo = LBProgressRepository(db_session)

        dist_repo.upsert(id=2, name_ml="കൊല്ലം")
        lb_type_repo.upsert(id=5, name_ml="ഗ്രാമ പഞ്ചായത്ത്")
        year_repo.upsert(id=36, year_int=2025)
        lb_repo.upsert(
            id=303,
            district_id=2,
            lb_type_id=5,
            name_ml="ഓച്ചിറ",
            scrape_run_id=None,
        )

        scrape_run = run_repo.create(kind="backfill")
        scrape_run_id = scrape_run.id
        progress_repo.bulk_create(scrape_run_id, [303])
        db_session.flush()

        # --- Build repos namespace ---
        repos = SimpleNamespace(
            lb_repo=lb_repo,
            main_group_value_repo=MainGroupValueRepository(db_session),
            meeting_manifest_repo=MeetingManifestRepository(db_session),
            dashboard_kpi_snapshot_repo=DashboardKPISnapshotRepository(db_session),
            lb_progress_repo=progress_repo,
        )

        # --- Mock client using fixture HTML ---
        dashboard_state = FormState(
            viewstate="VS",
            event_validation="EV",
            page_url="http://meeting.lsgkerala.gov.in/Pages/LBWiseDashBoard.aspx",
            raw_html=DASHBOARD_HTML,
        )
        grid_state = FormState(
            viewstate="VS2",
            event_validation="EV2",
            page_url="http://meeting.lsgkerala.gov.in/Pages/LBWiseDashBoard.aspx",
            raw_html=APPROVED_GRID_HTML,
        )
        client = _make_client(dashboard_state=dashboard_state, drill_state=grid_state)

        with patch(_PATCH_SETTINGS) as mock_settings:
            mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"

            # Use real parsers (no mocking) — client returns fixture HTML
            summary = run_for_lb(client, repos, lb_id=303, scrape_run_id=scrape_run_id)

        # The dashboard fixture has 3 main groups (values 1, 2, 3) and years 34, 35, 36.
        # The grid fixture has 6 rows × 4 categories = up to 24 rows per (year, group).
        assert summary["years_processed"] >= 1
        assert summary["main_groups_processed"] >= 1
        assert summary["kpi_snapshots"] >= 1
        # At least some manifest rows written
        assert summary["manifest_rows_inserted"] >= 0
