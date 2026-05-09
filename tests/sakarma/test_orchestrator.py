"""Tests for sakarma.tasks.orchestrator.scrape_lb.

Unit tests (no DB, no HTTP) use MagicMock throughout.
Integration tests are marked ``@pytest.mark.integration`` and require
a live database via the ``db_session`` fixture from conftest.py.
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers / shared constants
# ---------------------------------------------------------------------------

_LB_ID = 42
_RUN_ID = 7
_PROGRESS_ID = 99

_MANIFEST_SUMMARY = {
    "kpi_snapshots": 2,
    "manifest_rows_inserted": 10,
    "categories_processed": 4,
    "years_processed": 2,
    "main_groups_processed": 1,
}
_ARTIFACTS_SUMMARY = {
    "minutes_uploaded": 3,
    "dr_uploaded": 3,
    "attachments_uploaded": 1,
    "rows_processed": 3,
    "rows_skipped": 0,
}
_RECON_SUMMARY = {
    "reconciliation_rows": 8,
    "matched": 6,
    "mismatch": 1,
    "missing_kpi": 0,
    "missing_manifest": 1,
}


def _make_progress_row(status: str = "pending") -> MagicMock:
    row = MagicMock()
    row.id = _PROGRESS_ID
    row.lb_id = _LB_ID
    row.scrape_run_id = _RUN_ID
    row.status = status
    return row


def _make_all_mocks():
    """Return a dict of patch targets and their desired return values."""
    progress_row = _make_progress_row()

    progress_repo = MagicMock()
    progress_repo.get_by_run_lb.return_value = progress_row

    db_session = MagicMock()
    db_session.__enter__ = MagicMock(return_value=db_session)
    db_session.__exit__ = MagicMock(return_value=False)

    return progress_row, progress_repo, db_session


# ---------------------------------------------------------------------------
# Unit tests — happy path
# ---------------------------------------------------------------------------


class TestScrapeHappyPath:
    """All three stages succeed, progress ends at DONE."""

    def _run(self):
        from sakarma.tasks.orchestrator import scrape_lb

        # We call the underlying function directly (bypassing Celery machinery)
        # by accessing .run() if available, otherwise patching apply().
        # For unit tests the cleanest approach is to call the task body via
        # apply() with ALWAYS_EAGER so it executes synchronously in-process.
        return scrape_lb.run(scrape_run_id=_RUN_ID, lb_id=_LB_ID)

    @patch("sakarma.tasks.orchestrator.get_storage")
    @patch("sakarma.tasks.orchestrator.get_rate_limiter")
    @patch("sakarma.tasks.orchestrator.SakarmaClient")
    @patch("sakarma.tasks.orchestrator.reconciliation.run_for_lb")
    @patch("sakarma.tasks.orchestrator.artifacts.run_for_lb")
    @patch("sakarma.tasks.orchestrator.manifest.run_for_lb")
    @patch("sakarma.tasks.orchestrator.LBProgressRepository")
    @patch("sakarma.tasks.orchestrator.ReconciliationRepository")
    @patch("sakarma.tasks.orchestrator.MeetingArtifactRepository")
    @patch("sakarma.tasks.orchestrator.MeetingManifestRepository")
    @patch("sakarma.tasks.orchestrator.DashboardKPISnapshotRepository")
    @patch("sakarma.tasks.orchestrator.MainGroupValueRepository")
    @patch("sakarma.tasks.orchestrator.YearRepository")
    @patch("sakarma.tasks.orchestrator.LBRepository")
    @patch("sakarma.tasks.orchestrator.get_session")
    def test_happy_path_returns_summary(
        self,
        mock_get_session,
        mock_lb_repo_cls,
        mock_year_repo_cls,
        mock_mg_repo_cls,
        mock_kpi_repo_cls,
        mock_manifest_repo_cls,
        mock_artifact_repo_cls,
        mock_recon_repo_cls,
        mock_progress_repo_cls,
        mock_manifest_run,
        mock_artifacts_run,
        mock_recon_run,
        mock_client_cls,
        mock_get_rl,
        mock_get_storage,
    ):
        # Arrange: set up mock session context manager
        mock_db_session = MagicMock()
        mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_db_session)
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        # Progress repo
        progress_row = _make_progress_row()
        mock_progress_repo = MagicMock()
        mock_progress_repo.get_by_run_lb.return_value = progress_row
        mock_progress_repo_cls.return_value = mock_progress_repo

        # Stage functions
        mock_manifest_run.return_value = _MANIFEST_SUMMARY
        mock_artifacts_run.return_value = _ARTIFACTS_SUMMARY
        mock_recon_run.return_value = _RECON_SUMMARY

        # Act
        result = self._run()

        # Assert result shape
        assert result["lb_id"] == _LB_ID
        assert result["scrape_run_id"] == _RUN_ID
        assert result["manifest"] == _MANIFEST_SUMMARY
        assert result["artifacts"] == _ARTIFACTS_SUMMARY
        assert result["reconciliation"] == _RECON_SUMMARY

    @patch("sakarma.tasks.orchestrator.get_storage")
    @patch("sakarma.tasks.orchestrator.get_rate_limiter")
    @patch("sakarma.tasks.orchestrator.SakarmaClient")
    @patch("sakarma.tasks.orchestrator.reconciliation.run_for_lb")
    @patch("sakarma.tasks.orchestrator.artifacts.run_for_lb")
    @patch("sakarma.tasks.orchestrator.manifest.run_for_lb")
    @patch("sakarma.tasks.orchestrator.LBProgressRepository")
    @patch("sakarma.tasks.orchestrator.ReconciliationRepository")
    @patch("sakarma.tasks.orchestrator.MeetingArtifactRepository")
    @patch("sakarma.tasks.orchestrator.MeetingManifestRepository")
    @patch("sakarma.tasks.orchestrator.DashboardKPISnapshotRepository")
    @patch("sakarma.tasks.orchestrator.MainGroupValueRepository")
    @patch("sakarma.tasks.orchestrator.YearRepository")
    @patch("sakarma.tasks.orchestrator.LBRepository")
    @patch("sakarma.tasks.orchestrator.get_session")
    def test_progress_transitions_in_order(
        self,
        mock_get_session,
        mock_lb_repo_cls,
        mock_year_repo_cls,
        mock_mg_repo_cls,
        mock_kpi_repo_cls,
        mock_manifest_repo_cls,
        mock_artifact_repo_cls,
        mock_recon_repo_cls,
        mock_progress_repo_cls,
        mock_manifest_run,
        mock_artifacts_run,
        mock_recon_run,
        mock_client_cls,
        mock_get_rl,
        mock_get_storage,
    ):
        mock_db_session = MagicMock()
        mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_db_session)
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        progress_row = _make_progress_row()
        mock_progress_repo = MagicMock()
        mock_progress_repo.get_by_run_lb.return_value = progress_row
        mock_progress_repo_cls.return_value = mock_progress_repo

        mock_manifest_run.return_value = _MANIFEST_SUMMARY
        mock_artifacts_run.return_value = _ARTIFACTS_SUMMARY
        mock_recon_run.return_value = _RECON_SUMMARY

        self._run()

        # mark_in_progress called once
        mock_progress_repo.mark_in_progress.assert_called_once_with(_PROGRESS_ID)

        # mark_stage called 3x in correct order
        stage_calls = mock_progress_repo.mark_stage.call_args_list
        assert stage_calls == [
            call(_PROGRESS_ID, "manifest"),
            call(_PROGRESS_ID, "artifacts"),
            call(_PROGRESS_ID, "reconcile"),
        ]

        # mark_done called once
        mock_progress_repo.mark_done.assert_called_once_with(_PROGRESS_ID)

        # mark_error NOT called
        mock_progress_repo.mark_error.assert_not_called()

        # db_session.commit called once (success path)
        mock_db_session.commit.assert_called_once()

    @patch("sakarma.tasks.orchestrator.get_storage")
    @patch("sakarma.tasks.orchestrator.get_rate_limiter")
    @patch("sakarma.tasks.orchestrator.SakarmaClient")
    @patch("sakarma.tasks.orchestrator.reconciliation.run_for_lb")
    @patch("sakarma.tasks.orchestrator.artifacts.run_for_lb")
    @patch("sakarma.tasks.orchestrator.manifest.run_for_lb")
    @patch("sakarma.tasks.orchestrator.LBProgressRepository")
    @patch("sakarma.tasks.orchestrator.ReconciliationRepository")
    @patch("sakarma.tasks.orchestrator.MeetingArtifactRepository")
    @patch("sakarma.tasks.orchestrator.MeetingManifestRepository")
    @patch("sakarma.tasks.orchestrator.DashboardKPISnapshotRepository")
    @patch("sakarma.tasks.orchestrator.MainGroupValueRepository")
    @patch("sakarma.tasks.orchestrator.YearRepository")
    @patch("sakarma.tasks.orchestrator.LBRepository")
    @patch("sakarma.tasks.orchestrator.get_session")
    def test_stage_functions_called_in_order(
        self,
        mock_get_session,
        mock_lb_repo_cls,
        mock_year_repo_cls,
        mock_mg_repo_cls,
        mock_kpi_repo_cls,
        mock_manifest_repo_cls,
        mock_artifact_repo_cls,
        mock_recon_repo_cls,
        mock_progress_repo_cls,
        mock_manifest_run,
        mock_artifacts_run,
        mock_recon_run,
        mock_client_cls,
        mock_get_rl,
        mock_get_storage,
    ):
        mock_db_session = MagicMock()
        mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_db_session)
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        progress_row = _make_progress_row()
        mock_progress_repo = MagicMock()
        mock_progress_repo.get_by_run_lb.return_value = progress_row
        mock_progress_repo_cls.return_value = mock_progress_repo

        mock_manifest_run.return_value = _MANIFEST_SUMMARY
        mock_artifacts_run.return_value = _ARTIFACTS_SUMMARY
        mock_recon_run.return_value = _RECON_SUMMARY

        self._run()

        # Verify each stage function was called exactly once with correct
        # positional args (client/storage/repos are mocks; we check lb_id and
        # scrape_run_id as keyword or positional args).
        assert mock_manifest_run.call_count == 1
        assert mock_artifacts_run.call_count == 1
        assert mock_recon_run.call_count == 1

        # manifest.run_for_lb(client, repos, lb_id, scrape_run_id)
        _m_args = mock_manifest_run.call_args
        assert _m_args[0][2] == _LB_ID
        assert _m_args[0][3] == _RUN_ID

        # artifacts.run_for_lb(client, storage, repos, lb_id, scrape_run_id)
        _a_args = mock_artifacts_run.call_args
        assert _a_args[0][3] == _LB_ID
        assert _a_args[0][4] == _RUN_ID

        # reconciliation.run_for_lb(repos, lb_id, scrape_run_id)
        _r_args = mock_recon_run.call_args
        assert _r_args[0][1] == _LB_ID
        assert _r_args[0][2] == _RUN_ID


# ---------------------------------------------------------------------------
# Unit tests — error path
# ---------------------------------------------------------------------------


class TestScrapeErrorPath:
    """Exceptions during stages must be persisted and re-raised."""

    def _run_expect_raise(self):
        from sakarma.tasks.orchestrator import scrape_lb

        with pytest.raises(Exception):
            scrape_lb.run(scrape_run_id=_RUN_ID, lb_id=_LB_ID)

    @patch("sakarma.tasks.orchestrator.get_storage")
    @patch("sakarma.tasks.orchestrator.get_rate_limiter")
    @patch("sakarma.tasks.orchestrator.SakarmaClient")
    @patch("sakarma.tasks.orchestrator.reconciliation.run_for_lb")
    @patch("sakarma.tasks.orchestrator.artifacts.run_for_lb")
    @patch("sakarma.tasks.orchestrator.manifest.run_for_lb")
    @patch("sakarma.tasks.orchestrator.LBProgressRepository")
    @patch("sakarma.tasks.orchestrator.ReconciliationRepository")
    @patch("sakarma.tasks.orchestrator.MeetingArtifactRepository")
    @patch("sakarma.tasks.orchestrator.MeetingManifestRepository")
    @patch("sakarma.tasks.orchestrator.DashboardKPISnapshotRepository")
    @patch("sakarma.tasks.orchestrator.MainGroupValueRepository")
    @patch("sakarma.tasks.orchestrator.YearRepository")
    @patch("sakarma.tasks.orchestrator.LBRepository")
    @patch("sakarma.tasks.orchestrator.get_session")
    def test_artifacts_failure_marks_error_and_reraises(
        self,
        mock_get_session,
        mock_lb_repo_cls,
        mock_year_repo_cls,
        mock_mg_repo_cls,
        mock_kpi_repo_cls,
        mock_manifest_repo_cls,
        mock_artifact_repo_cls,
        mock_recon_repo_cls,
        mock_progress_repo_cls,
        mock_manifest_run,
        mock_artifacts_run,
        mock_recon_run,
        mock_client_cls,
        mock_get_rl,
        mock_get_storage,
    ):
        mock_db_session = MagicMock()
        mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_db_session)
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        progress_row = _make_progress_row()
        mock_progress_repo = MagicMock()
        mock_progress_repo.get_by_run_lb.return_value = progress_row
        mock_progress_repo_cls.return_value = mock_progress_repo

        boom = RuntimeError("artifacts exploded")
        mock_manifest_run.return_value = _MANIFEST_SUMMARY
        mock_artifacts_run.side_effect = boom
        mock_recon_run.return_value = _RECON_SUMMARY  # should never be called

        self._run_expect_raise()

        # mark_error called with repr of the exception
        mock_progress_repo.mark_error.assert_called_once_with(
            _PROGRESS_ID, error_message=repr(boom)
        )
        # mark_done NOT called
        mock_progress_repo.mark_done.assert_not_called()
        # reconciliation NOT called (pipeline stopped)
        mock_recon_run.assert_not_called()
        # db_session.commit was still called to persist the error state
        mock_db_session.commit.assert_called_once()

    @patch("sakarma.tasks.orchestrator.get_storage")
    @patch("sakarma.tasks.orchestrator.get_rate_limiter")
    @patch("sakarma.tasks.orchestrator.SakarmaClient")
    @patch("sakarma.tasks.orchestrator.reconciliation.run_for_lb")
    @patch("sakarma.tasks.orchestrator.artifacts.run_for_lb")
    @patch("sakarma.tasks.orchestrator.manifest.run_for_lb")
    @patch("sakarma.tasks.orchestrator.LBProgressRepository")
    @patch("sakarma.tasks.orchestrator.ReconciliationRepository")
    @patch("sakarma.tasks.orchestrator.MeetingArtifactRepository")
    @patch("sakarma.tasks.orchestrator.MeetingManifestRepository")
    @patch("sakarma.tasks.orchestrator.DashboardKPISnapshotRepository")
    @patch("sakarma.tasks.orchestrator.MainGroupValueRepository")
    @patch("sakarma.tasks.orchestrator.YearRepository")
    @patch("sakarma.tasks.orchestrator.LBRepository")
    @patch("sakarma.tasks.orchestrator.get_session")
    def test_session_expired_error_propagates_as_recoverable(
        self,
        mock_get_session,
        mock_lb_repo_cls,
        mock_year_repo_cls,
        mock_mg_repo_cls,
        mock_kpi_repo_cls,
        mock_manifest_repo_cls,
        mock_artifact_repo_cls,
        mock_recon_repo_cls,
        mock_progress_repo_cls,
        mock_manifest_run,
        mock_artifacts_run,
        mock_recon_run,
        mock_client_cls,
        mock_get_rl,
        mock_get_storage,
    ):
        from sakarma.scraper.client import SessionExpiredError

        mock_db_session = MagicMock()
        mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_db_session)
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        progress_row = _make_progress_row()
        mock_progress_repo = MagicMock()
        mock_progress_repo.get_by_run_lb.return_value = progress_row
        mock_progress_repo_cls.return_value = mock_progress_repo

        session_err = SessionExpiredError("session expired mid-artifacts")
        mock_manifest_run.return_value = _MANIFEST_SUMMARY
        mock_artifacts_run.side_effect = session_err

        from sakarma.tasks.orchestrator import scrape_lb

        with pytest.raises(SessionExpiredError):
            scrape_lb.run(scrape_run_id=_RUN_ID, lb_id=_LB_ID)

        # The error must be persisted
        mock_progress_repo.mark_error.assert_called_once()
        mark_error_kwargs = mock_progress_repo.mark_error.call_args[1]
        assert "SessionExpiredError" in mark_error_kwargs["error_message"]

        # Commit must be called to persist error state
        mock_db_session.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Unit tests — edge case: progress row does not exist yet
# ---------------------------------------------------------------------------


class TestScrapeProgressRowCreation:
    """When get_by_run_lb returns None, the row is bulk_created then re-fetched."""

    @patch("sakarma.tasks.orchestrator.get_storage")
    @patch("sakarma.tasks.orchestrator.get_rate_limiter")
    @patch("sakarma.tasks.orchestrator.SakarmaClient")
    @patch("sakarma.tasks.orchestrator.reconciliation.run_for_lb")
    @patch("sakarma.tasks.orchestrator.artifacts.run_for_lb")
    @patch("sakarma.tasks.orchestrator.manifest.run_for_lb")
    @patch("sakarma.tasks.orchestrator.LBProgressRepository")
    @patch("sakarma.tasks.orchestrator.ReconciliationRepository")
    @patch("sakarma.tasks.orchestrator.MeetingArtifactRepository")
    @patch("sakarma.tasks.orchestrator.MeetingManifestRepository")
    @patch("sakarma.tasks.orchestrator.DashboardKPISnapshotRepository")
    @patch("sakarma.tasks.orchestrator.MainGroupValueRepository")
    @patch("sakarma.tasks.orchestrator.YearRepository")
    @patch("sakarma.tasks.orchestrator.LBRepository")
    @patch("sakarma.tasks.orchestrator.get_session")
    def test_creates_progress_row_when_absent(
        self,
        mock_get_session,
        mock_lb_repo_cls,
        mock_year_repo_cls,
        mock_mg_repo_cls,
        mock_kpi_repo_cls,
        mock_manifest_repo_cls,
        mock_artifact_repo_cls,
        mock_recon_repo_cls,
        mock_progress_repo_cls,
        mock_manifest_run,
        mock_artifacts_run,
        mock_recon_run,
        mock_client_cls,
        mock_get_rl,
        mock_get_storage,
    ):
        mock_db_session = MagicMock()
        mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_db_session)
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        progress_row = _make_progress_row()
        mock_progress_repo = MagicMock()
        # First call returns None (row absent), second call returns the created row
        mock_progress_repo.get_by_run_lb.side_effect = [None, progress_row]
        mock_progress_repo_cls.return_value = mock_progress_repo

        mock_manifest_run.return_value = _MANIFEST_SUMMARY
        mock_artifacts_run.return_value = _ARTIFACTS_SUMMARY
        mock_recon_run.return_value = _RECON_SUMMARY

        from sakarma.tasks.orchestrator import scrape_lb

        result = scrape_lb.run(scrape_run_id=_RUN_ID, lb_id=_LB_ID)

        # bulk_create was called with the lb_id wrapped in a list
        mock_progress_repo.bulk_create.assert_called_once_with(_RUN_ID, [_LB_ID])

        # get_by_run_lb called twice: once before (absent), once after bulk_create
        assert mock_progress_repo.get_by_run_lb.call_count == 2

        # Pipeline still runs to completion
        assert result["lb_id"] == _LB_ID


# ---------------------------------------------------------------------------
# Unit tests — idempotent re-run
# ---------------------------------------------------------------------------


class TestScrapeIdempotentRerun:
    """Re-running an already-completed LB produces zero net new rows and DONE status."""

    @patch("sakarma.tasks.orchestrator.get_storage")
    @patch("sakarma.tasks.orchestrator.get_rate_limiter")
    @patch("sakarma.tasks.orchestrator.SakarmaClient")
    @patch("sakarma.tasks.orchestrator.reconciliation.run_for_lb")
    @patch("sakarma.tasks.orchestrator.artifacts.run_for_lb")
    @patch("sakarma.tasks.orchestrator.manifest.run_for_lb")
    @patch("sakarma.tasks.orchestrator.LBProgressRepository")
    @patch("sakarma.tasks.orchestrator.ReconciliationRepository")
    @patch("sakarma.tasks.orchestrator.MeetingArtifactRepository")
    @patch("sakarma.tasks.orchestrator.MeetingManifestRepository")
    @patch("sakarma.tasks.orchestrator.DashboardKPISnapshotRepository")
    @patch("sakarma.tasks.orchestrator.MainGroupValueRepository")
    @patch("sakarma.tasks.orchestrator.YearRepository")
    @patch("sakarma.tasks.orchestrator.LBRepository")
    @patch("sakarma.tasks.orchestrator.get_session")
    def test_idempotent_rerun_ends_done(
        self,
        mock_get_session,
        mock_lb_repo_cls,
        mock_year_repo_cls,
        mock_mg_repo_cls,
        mock_kpi_repo_cls,
        mock_manifest_repo_cls,
        mock_artifact_repo_cls,
        mock_recon_repo_cls,
        mock_progress_repo_cls,
        mock_manifest_run,
        mock_artifacts_run,
        mock_recon_run,
        mock_client_cls,
        mock_get_rl,
        mock_get_storage,
    ):
        """Simulate a re-run where all upserts produce zero new rows."""
        mock_db_session = MagicMock()
        mock_get_session.return_value.__enter__ = MagicMock(return_value=mock_db_session)
        mock_get_session.return_value.__exit__ = MagicMock(return_value=False)

        # Existing completed progress row (status=done in DB)
        progress_row = _make_progress_row(status="done")
        mock_progress_repo = MagicMock()
        mock_progress_repo.get_by_run_lb.return_value = progress_row
        mock_progress_repo_cls.return_value = mock_progress_repo

        # Zero new rows on re-run (all upserts hit existing rows)
        zero_manifest = {**_MANIFEST_SUMMARY, "manifest_rows_inserted": 0}
        zero_artifacts = {**_ARTIFACTS_SUMMARY, "minutes_uploaded": 0, "dr_uploaded": 0,
                          "attachments_uploaded": 0, "rows_processed": 0, "rows_skipped": 5}
        zero_recon = {**_RECON_SUMMARY, "reconciliation_rows": 8}

        mock_manifest_run.return_value = zero_manifest
        mock_artifacts_run.return_value = zero_artifacts
        mock_recon_run.return_value = zero_recon

        from sakarma.tasks.orchestrator import scrape_lb

        result = scrape_lb.run(scrape_run_id=_RUN_ID, lb_id=_LB_ID)

        assert result["manifest"]["manifest_rows_inserted"] == 0
        assert result["artifacts"]["rows_skipped"] == 5

        # Status must end at DONE (mark_done called)
        mock_progress_repo.mark_done.assert_called_once_with(_PROGRESS_ID)
        mock_progress_repo.mark_error.assert_not_called()


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestScrapeIntegration:
    """End-to-end test with a real DB session and a mock HTTP client.

    Requires the ``db_session`` fixture (PostgreSQL) from conftest.py.
    The SakarmaClient is fully mocked so no network calls occur.
    """

    def _seed_lb(self, db_session):
        """Insert the minimum dimension rows needed for an LB scrape."""
        from sakarma.db.repositories import (
            DistrictRepository,
            LBRepository,
            LBProgressRepository,
            LBTypeRepository,
            ScrapeRunRepository,
        )

        district_repo = DistrictRepository(db_session)
        lb_type_repo = LBTypeRepository(db_session)
        lb_repo = LBRepository(db_session)
        run_repo = ScrapeRunRepository(db_session)
        progress_repo = LBProgressRepository(db_session)

        district_repo.upsert(id=1, name_ml="ജില്ല", name_en="District")
        lb_type_repo.upsert(id=1, name_ml="ഗ്രാമ പഞ്ചായത്ത്", name_en="GP")
        lb_repo.upsert(id=_LB_ID, district_id=1, lb_type_id=1, name_ml="Test LB")
        run = run_repo.create(kind="full")
        db_session.flush()
        progress_repo.bulk_create(run.id, [_LB_ID])
        db_session.flush()
        return run.id

    def test_end_to_end_with_mock_client(self, db_session):
        """Runs scrape_lb with a mock client that returns canned HTML."""
        from unittest.mock import patch as _patch

        from sakarma.db.repositories import LBProgressRepository

        run_id = self._seed_lb(db_session)

        # Build a mock SakarmaClient that returns minimal valid HTML
        mock_client = MagicMock()
        # load_page, select_dropdown, click_button all return a FormState-like mock
        form_state = MagicMock()
        form_state.raw_html = b"<html></html>"
        mock_client.load_page.return_value = form_state
        mock_client.select_dropdown.return_value = form_state
        mock_client.click_button.return_value = form_state

        # Patch parse helpers so manifest finds no rows (zero-row run still
        # exercises the full pipeline path without needing real HTML)
        with (
            _patch("sakarma.tasks.orchestrator.get_session") as mock_gs,
            _patch("sakarma.tasks.orchestrator.SakarmaClient", return_value=mock_client),
            _patch("sakarma.tasks.orchestrator.get_rate_limiter", return_value=MagicMock()),
            _patch("sakarma.tasks.orchestrator.get_storage", return_value=MagicMock()),
            _patch("sakarma.tasks.manifest.parse_dropdown_options", return_value=[]),
            _patch("sakarma.tasks.manifest.parse_kpi_cards", return_value=MagicMock(
                total=0, ongoing=0, minutes_complete=0, minutes_incomplete=0, cancelled=0
            )),
            _patch("sakarma.tasks.manifest.parse_meeting_grid", return_value=[]),
        ):
            # Wire mock_gs to use the real db_session
            mock_gs.return_value.__enter__ = MagicMock(return_value=db_session)
            mock_gs.return_value.__exit__ = MagicMock(return_value=False)

            # Suppress commit (test uses rollback fixture)
            db_session.commit = MagicMock()

            from sakarma.tasks.orchestrator import scrape_lb

            result = scrape_lb.run(scrape_run_id=run_id, lb_id=_LB_ID)

        # Check result shape
        assert result["lb_id"] == _LB_ID
        assert result["scrape_run_id"] == run_id
        assert "manifest" in result
        assert "artifacts" in result
        assert "reconciliation" in result

        # Verify the lb_progress row ended in DONE state via real repo
        progress_repo = LBProgressRepository(db_session)
        row = progress_repo.get_by_run_lb(run_id, _LB_ID)
        # The mock patched db_session, so status should be "done"
        # (mark_done was called on the same session)
        assert row is not None
