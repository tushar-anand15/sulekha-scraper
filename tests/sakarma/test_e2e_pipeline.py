"""End-to-end integration test for the SAKARMA per-LB pipeline.

This module contains a single integration test that exercises the full
pipeline path — from seeding dimension rows through
``manifest → artifacts → reconciliation`` — against canned client responses
backed by the HTML fixtures committed alongside these tests.

The test is marked ``@pytest.mark.integration`` and will be skipped
automatically when PostgreSQL is not available (the ``db_session`` fixture
from conftest.py handles that skip via ``pytest.skip``).

No network calls are made.  The ``SakarmaClient`` and storage backend are
both replaced with ``MagicMock`` objects that return fixture-backed responses.
Celery is bypassed: ``scrape_lb.run(...)`` is called directly.
"""

from __future__ import annotations

import pathlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def _load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# Constants matching the seeded dimension rows
# ---------------------------------------------------------------------------

_LB_ID = 303        # Oachira GP (matches ddlLBName value in fixture HTML)
_DISTRICT_ID = 2    # Kollam
_LB_TYPE_ID = 5     # Grama Panchayath
_YEAR_ID = 36       # 2025 (ddlYear value in fixture HTML)
_YEAR_INT = 2025
_MAIN_GROUP_DDL = 2


# ---------------------------------------------------------------------------
# Helper: build a fake FormState-like object backed by a fixture file
# ---------------------------------------------------------------------------

def _form_state(filename: str) -> MagicMock:
    """Return a MagicMock whose ``raw_html`` attribute is fixture bytes."""
    fs = MagicMock()
    fs.raw_html = _load(filename)
    return fs


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestE2EOneLBPipeline:
    """Full per-LB pipeline with real DB rows and mocked HTTP + storage."""

    def _seed_dimensions(self, db_session: Any) -> int:
        """Insert the minimum dimension rows and return the scrape_run id.

        Skips live discovery — the focus here is the orchestrator pipeline.
        """
        from sakarma.db.repositories import (
            DistrictRepository,
            LBProgressRepository,
            LBRepository,
            LBTypeRepository,
            MainGroupValueRepository,
            ScrapeRunRepository,
            YearRepository,
        )

        district_repo = DistrictRepository(db_session)
        lb_type_repo = LBTypeRepository(db_session)
        year_repo = YearRepository(db_session)
        lb_repo = LBRepository(db_session)
        mg_repo = MainGroupValueRepository(db_session)
        run_repo = ScrapeRunRepository(db_session)
        progress_repo = LBProgressRepository(db_session)

        district_repo.upsert(id=_DISTRICT_ID, name_ml="കൊല്ലം", name_en="Kollam")
        lb_type_repo.upsert(id=_LB_TYPE_ID, name_ml="ഗ്രാമ പഞ്ചായത്ത്", name_en="GP")
        year_repo.upsert(id=_YEAR_ID, year_int=_YEAR_INT)
        lb_repo.upsert(
            id=_LB_ID,
            district_id=_DISTRICT_ID,
            lb_type_id=_LB_TYPE_ID,
            name_ml="ഓച്ചിറ",
        )
        mg_repo.upsert(lb_id=_LB_ID, ddl_value=_MAIN_GROUP_DDL, name_ml="ഭരണസമിതി")

        run = run_repo.create_backfill()
        db_session.flush()

        progress_repo.bulk_create(run.id, [_LB_ID])
        db_session.flush()

        return run.id

    def _build_mock_client(self) -> MagicMock:
        """Return a mock SakarmaClient backed by canned fixture HTML.

        The dashboard fixture drives parse_kpi_cards and parse_meeting_grid.
        The minutes and dregister fixtures drive the per-meeting artifact fetch.
        """
        mock_client = MagicMock()

        dashboard_state = _form_state("lbwise_dashboard_oachira_2025_gb.html")
        minutes_state = _form_state("public_minutes_oachira_2025_001.html")
        dregister_state = _form_state("public_dregister_with_attachment.html")

        # load_page, select_dropdown, and click_button all return the dashboard
        # state by default; per-meeting navigation returns minutes/dregister.
        mock_client.load_page.return_value = dashboard_state
        mock_client.select_dropdown.return_value = dashboard_state
        mock_client.click_button.return_value = dregister_state

        # navigate_to_minutes and navigate_to_dregister are used by the
        # artifacts stage when clicking per-row links.
        mock_client.navigate_to_minutes.return_value = minutes_state
        mock_client.navigate_to_dregister.return_value = dregister_state

        return mock_client

    def test_e2e_one_lb_pipeline(self, db_session: Any) -> None:
        """Full per-LB pipeline: discovery seeds dimensions, orchestrator runs
        manifest → artifacts → reconciliation against canned client responses,
        final state has lb_progress row in DONE status.

        This test deliberately suppresses ``db_session.commit`` so that the
        transactional rollback fixture in conftest.py can still clean up after
        the test.  We verify state via direct repo queries on the same session.
        """
        from sakarma.db.repositories import LBProgressRepository

        run_id = self._seed_dimensions(db_session)

        mock_client = self._build_mock_client()
        mock_storage = MagicMock()
        # Storage upload returns (gcs_path, content_hash, byte_size) tuples.
        mock_storage.upload_document.return_value = (
            "artifacts/oachira/2025/test.html",
            "abc123def456" * 4,  # 48-char hex-like hash
            4096,
        )
        mock_storage.bucket_name = "sakarma-test"

        with (
            patch("sakarma.tasks.orchestrator.get_session") as mock_gs,
            patch(
                "sakarma.tasks.orchestrator.SakarmaClient",
                return_value=mock_client,
            ),
            patch(
                "sakarma.tasks.orchestrator.get_rate_limiter",
                return_value=MagicMock(),
            ),
            patch(
                "sakarma.tasks.orchestrator.get_storage",
                return_value=mock_storage,
            ),
            # Suppress parse helpers that need exact grid structure so the test
            # focuses on pipeline plumbing rather than parser correctness (those
            # are covered by test_parsers.py).  Return minimal valid responses
            # that produce one manifest row and one KPI snapshot.
            patch(
                "sakarma.tasks.manifest.parse_kpi_cards",
                return_value=MagicMock(
                    total=34,
                    ongoing=0,
                    minutes_complete=34,
                    minutes_incomplete=0,
                    cancelled=0,
                ),
            ),
            patch(
                "sakarma.tasks.manifest.parse_dropdown_options",
                side_effect=_dropdown_side_effect,
            ),
            patch(
                "sakarma.tasks.manifest.parse_meeting_grid",
                return_value=_one_manifest_row(),
            ),
            patch(
                "sakarma.tasks.artifacts.parse_attachment_links",
                return_value=[],
            ),
        ):
            # Wire the mock session context manager to the real db_session.
            mock_gs.return_value.__enter__ = MagicMock(return_value=db_session)
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            # Suppress commit so the rollback fixture can clean up.
            db_session.commit = MagicMock()

            from sakarma.tasks.orchestrator import scrape_lb

            result = scrape_lb.run(scrape_run_id=run_id, lb_id=_LB_ID)

        # ------------------------------------------------------------------
        # Assert result shape
        # ------------------------------------------------------------------
        assert result["lb_id"] == _LB_ID, (
            f"Expected lb_id={_LB_ID!r} in result, got {result['lb_id']!r}"
        )
        assert result["scrape_run_id"] == run_id, (
            f"Expected scrape_run_id={run_id!r}, got {result['scrape_run_id']!r}"
        )
        for stage in ("manifest", "artifacts", "reconciliation"):
            assert stage in result, (
                f"Expected '{stage}' key in result dict, got: {list(result.keys())}"
            )

        # ------------------------------------------------------------------
        # Assert lb_progress ended in DONE state (verified via real repo query)
        # ------------------------------------------------------------------
        progress_repo = LBProgressRepository(db_session)
        row = progress_repo.get_by_run_lb(run_id, _LB_ID)

        assert row is not None, (
            f"No lb_progress row found for run_id={run_id}, lb_id={_LB_ID}. "
            "Seeding or bulk_create may have failed."
        )
        assert row.status == "done", (
            f"Expected lb_progress.status='done', got {row.status!r}. "
            "Pipeline may have errored — check result dict and logs."
        )

        # ------------------------------------------------------------------
        # Assert manifest stage returned a non-negative row count
        # ------------------------------------------------------------------
        manifest_summary = result["manifest"]
        assert isinstance(manifest_summary, dict), (
            f"manifest stage result should be a dict, got {type(manifest_summary)}"
        )
        assert manifest_summary.get("manifest_rows_inserted", -1) >= 0, (
            "manifest_rows_inserted should be >= 0 (upsert semantics)"
        )

        # ------------------------------------------------------------------
        # Assert reconciliation stage ran (dict with expected keys)
        # ------------------------------------------------------------------
        recon_summary = result["reconciliation"]
        assert isinstance(recon_summary, dict), (
            f"reconciliation stage result should be a dict, got {type(recon_summary)}"
        )
        assert "reconciliation_rows" in recon_summary, (
            f"Expected 'reconciliation_rows' key in recon summary: {recon_summary}"
        )


# ---------------------------------------------------------------------------
# Helpers used by the mock patches above
# ---------------------------------------------------------------------------


def _one_manifest_row() -> list:
    """Return a single synthetic ManifestRow for category=APPROVED.

    The row drives one full pass through the artifacts stage without requiring
    real parse_meeting_grid HTML.
    """
    from sakarma.db.models import CATEGORY_APPROVED
    from sakarma.scraper.parsers import ManifestRow

    return [
        ManifestRow(
            category=CATEGORY_APPROVED,
            dashboard_grid_select_index=0,
            meeting_date="15/01/2025",
            meeting_no_label="01/2025",
            meeting_type="ഭരണസമിതി യോഗം",
            meeting_nature="സാധാരണ യോഗം",
            meeting_venue="ഓച്ചിറ പഞ്ചായത്ത് ഹാൾ",
            dr_postback_target=(
                "ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl02$lnkDR"
            ),
        )
    ]


def _dropdown_side_effect(html: bytes, ddl_id: str) -> list:
    """Return realistic dropdown option lists keyed by dropdown id.

    The manifest stage calls parse_dropdown_options for each cascaded
    dropdown.  This helper returns just enough data to drive one iteration
    of year × main_group without crashing on missing options.
    """
    from sakarma.scraper.protocol import (
        DDL_YEAR,
        DDL_MAIN_GROUP,
        DDL_LB_NAME,
    )

    if DDL_YEAR in ddl_id:
        return [(_YEAR_ID, str(_YEAR_INT))]
    if DDL_MAIN_GROUP in ddl_id:
        return [(_MAIN_GROUP_DDL, "ഭരണസമിതി")]
    if DDL_LB_NAME in ddl_id:
        return [(_LB_ID, "ഓച്ചിറ")]
    return []
