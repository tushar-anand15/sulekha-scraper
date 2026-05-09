"""Tests for sakarma.cli — Unit 13.

Unit tests use Click's CliRunner and mock all DB/Celery I/O so they run
without a real Postgres or Redis.  Integration tests (marked
``@pytest.mark.integration``) exercise the commands against the test DB.
"""

from __future__ import annotations

import csv
import io
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from sakarma.cli import main


# ---------------------------------------------------------------------------
# Patch targets — all names live in sakarma.cli after module-level imports.
# ---------------------------------------------------------------------------

_GET_SESSION = "sakarma.cli.get_session"
_LB_REPO = "sakarma.cli.LBRepository"
_SCRAPE_RUN_REPO = "sakarma.cli.ScrapeRunRepository"
_LB_PROGRESS_REPO = "sakarma.cli.LBProgressRepository"
_RECON_REPO = "sakarma.cli.ReconciliationRepository"


# ---------------------------------------------------------------------------
# Helpers / shared factories
# ---------------------------------------------------------------------------

def _make_lb(lb_id: int, district_id: int = 1, name_ml: str = "Test LB") -> MagicMock:
    lb = MagicMock()
    lb.id = lb_id
    lb.district_id = district_id
    lb.lb_type_id = 1
    lb.name_ml = name_ml
    return lb


def _make_run(run_id: int = 1, kind: str = "backfill", status: str = "running") -> MagicMock:
    run = MagicMock()
    run.id = run_id
    run.kind = kind
    run.status = status
    run.started_at = datetime(2026, 5, 9, 10, 0, 0, tzinfo=timezone.utc)
    run.completed_at = None
    run.error_summary = None
    return run


def _make_progress(lb_id: int, status: str = "done") -> MagicMock:
    p = MagicMock()
    p.lb_id = lb_id
    p.status = status
    return p


def _make_recon_row(
    run_id: int = 1,
    lb_id: int = 101,
    year_id: int = 27,
    mg_id: int = 5,
    category: int = 2,
    kpi: int = 10,
    manifest: int = 10,
    delta: int = 0,
    status: str = "matched",
) -> MagicMock:
    r = MagicMock()
    r.scrape_run_id = run_id
    r.lb_id = lb_id
    r.year_id = year_id
    r.main_group_value_id = mg_id
    r.category = category
    r.dashboard_kpi_count = kpi
    r.manifest_row_count = manifest
    r.delta = delta
    r.status = status
    r.computed_at = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    return r


def _noop_session():
    """Context manager that yields a MagicMock session."""
    @contextmanager
    def _ctx():
        yield MagicMock()
    return _ctx


# ---------------------------------------------------------------------------
# 1. Happy path: --help lists every subcommand
# ---------------------------------------------------------------------------

class TestHelp:
    def test_main_help_lists_all_commands(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == 0
        for cmd in ["init-db", "backfill", "diff", "status", "reconcile-report", "worker", "flower"]:
            assert cmd in result.output, f"Expected '{cmd}' in --help output"

    def test_backfill_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["backfill", "--help"])
        assert result.exit_code == 0
        assert "--lb" in result.output
        assert "--district" in result.output
        assert "--dry-run" in result.output

    def test_diff_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["diff", "--help"])
        assert result.exit_code == 0
        assert "--lb" in result.output

    def test_status_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["status", "--help"])
        assert result.exit_code == 0
        assert "--detail" in result.output
        assert "--scrape-run" in result.output
        assert "--reconciliation" in result.output

    def test_reconcile_report_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["reconcile-report", "--help"])
        assert result.exit_code == 0
        assert "--csv" in result.output
        assert "--status" in result.output


# ---------------------------------------------------------------------------
# 2. backfill --dry-run: no DB inserts, prints LB count, exits 0
# ---------------------------------------------------------------------------

class TestBackfillDryRun:
    def test_dry_run_all_lbs_exits_0(self) -> None:
        lbs = [_make_lb(i, district_id=1) for i in range(1, 4)]

        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = lbs
        mock_run_repo = MagicMock()

        with patch(_GET_SESSION, _noop_session()):
            with patch(_LB_REPO, return_value=mock_lb_repo):
                with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                    runner = CliRunner()
                    result = runner.invoke(main, ["backfill", "--dry-run"])

        assert result.exit_code == 0
        assert "dry" in result.output.lower()
        # No scrape run created.
        mock_run_repo.create_backfill.assert_not_called()

    def test_dry_run_prints_lb_count(self) -> None:
        lbs = [_make_lb(i) for i in [10, 20, 30]]
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = lbs
        mock_run_repo = MagicMock()

        with patch(_GET_SESSION, _noop_session()):
            with patch(_LB_REPO, return_value=mock_lb_repo):
                with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                    runner = CliRunner()
                    result = runner.invoke(main, ["backfill", "--dry-run"])

        assert result.exit_code == 0
        assert "3" in result.output  # 3 LBs planned

    def test_dry_run_with_district_filter(self) -> None:
        lbs = [_make_lb(101, district_id=2), _make_lb(102, district_id=2)]
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_by_district.return_value = lbs
        mock_run_repo = MagicMock()

        with patch(_GET_SESSION, _noop_session()):
            with patch(_LB_REPO, return_value=mock_lb_repo):
                with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                    runner = CliRunner()
                    result = runner.invoke(main, ["backfill", "--district", "2", "--dry-run"])

        assert result.exit_code == 0
        mock_run_repo.create_backfill.assert_not_called()

    def test_diff_dry_run_exits_0(self) -> None:
        lbs = [_make_lb(99)]
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = lbs
        mock_run_repo = MagicMock()

        with patch(_GET_SESSION, _noop_session()):
            with patch(_LB_REPO, return_value=mock_lb_repo):
                with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                    runner = CliRunner()
                    result = runner.invoke(main, ["diff", "--dry-run"])

        assert result.exit_code == 0
        mock_run_repo.create_diff.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Edge case: --lb with non-existent id exits non-zero with a clear message
# ---------------------------------------------------------------------------

class TestBackfillNonExistentLB:
    def test_exits_nonzero_for_unknown_lb(self) -> None:
        mock_lb_repo = MagicMock()
        mock_lb_repo.get.return_value = None  # LB not found

        with patch(_GET_SESSION, _noop_session()):
            with patch(_LB_REPO, return_value=mock_lb_repo):
                runner = CliRunner()
                result = runner.invoke(main, ["backfill", "--lb", "99999"])

        assert result.exit_code != 0
        output = result.output.lower()
        assert "99999" in output or "not found" in output or "error" in output


# ---------------------------------------------------------------------------
# 4. status command returns mocked counts
# ---------------------------------------------------------------------------

class TestStatusCommand:
    def test_status_shows_counts(self) -> None:
        run = _make_run(run_id=7, kind="backfill", status="running")
        progress = [
            _make_progress(1, "done"),
            _make_progress(2, "done"),
            _make_progress(3, "in_progress"),
            _make_progress(4, "error"),
            _make_progress(5, "pending"),
        ]

        mock_run_repo = MagicMock()
        mock_run_repo.list_recent.return_value = [run]
        mock_progress_repo = MagicMock()
        mock_progress_repo.list_for_run.return_value = progress
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = []
        mock_recon_repo = MagicMock()
        mock_recon_repo.list_for_run.return_value = []

        with patch(_GET_SESSION, _noop_session()):
            with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                with patch(_LB_PROGRESS_REPO, return_value=mock_progress_repo):
                    with patch(_LB_REPO, return_value=mock_lb_repo):
                        with patch(_RECON_REPO, return_value=mock_recon_repo):
                            runner = CliRunner()
                            result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        output = result.output
        assert "7" in output          # run id
        assert "backfill" in output
        assert "running" in output
        assert "5" in output          # total LB count

    def test_status_with_specific_run_id(self) -> None:
        run = _make_run(run_id=42, kind="diff", status="done")
        mock_run_repo = MagicMock()
        mock_run_repo.get.return_value = run
        mock_progress_repo = MagicMock()
        mock_progress_repo.list_for_run.return_value = []
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = []
        mock_recon_repo = MagicMock()
        mock_recon_repo.list_for_run.return_value = []

        with patch(_GET_SESSION, _noop_session()):
            with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                with patch(_LB_PROGRESS_REPO, return_value=mock_progress_repo):
                    with patch(_LB_REPO, return_value=mock_lb_repo):
                        with patch(_RECON_REPO, return_value=mock_recon_repo):
                            runner = CliRunner()
                            result = runner.invoke(main, ["status", "--scrape-run", "42"])

        assert result.exit_code == 0
        assert "42" in result.output
        mock_run_repo.get.assert_called_once_with(42)

    def test_status_no_runs(self) -> None:
        mock_run_repo = MagicMock()
        mock_run_repo.list_recent.return_value = []

        with patch(_GET_SESSION, _noop_session()):
            with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                runner = CliRunner()
                result = runner.invoke(main, ["status"])

        assert result.exit_code == 0
        assert "no scrape runs" in result.output.lower()

    def test_status_with_reconciliation_flag(self) -> None:
        run = _make_run(run_id=3, kind="backfill", status="done")
        recon = [
            _make_recon_row(status="matched"),
            _make_recon_row(status="mismatch"),
        ]
        mock_run_repo = MagicMock()
        mock_run_repo.list_recent.return_value = [run]
        mock_progress_repo = MagicMock()
        mock_progress_repo.list_for_run.return_value = []
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = []
        mock_recon_repo = MagicMock()
        mock_recon_repo.list_for_run.return_value = recon

        with patch(_GET_SESSION, _noop_session()):
            with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                with patch(_LB_PROGRESS_REPO, return_value=mock_progress_repo):
                    with patch(_LB_REPO, return_value=mock_lb_repo):
                        with patch(_RECON_REPO, return_value=mock_recon_repo):
                            runner = CliRunner()
                            result = runner.invoke(main, ["status", "--reconciliation"])

        assert result.exit_code == 0
        assert "matched" in result.output
        assert "mismatch" in result.output


# ---------------------------------------------------------------------------
# 5. reconcile-report --csv emits valid CSV
# ---------------------------------------------------------------------------

class TestReconcileReportCSV:
    def test_csv_is_parseable(self) -> None:
        run = _make_run(run_id=5, kind="backfill", status="done")
        recon_rows = [
            _make_recon_row(run_id=5, lb_id=101, status="matched"),
            _make_recon_row(run_id=5, lb_id=102, status="mismatch", kpi=8, manifest=10, delta=-2),
        ]

        mock_run_repo = MagicMock()
        mock_run_repo.list_recent.return_value = [run]
        mock_recon_repo = MagicMock()
        mock_recon_repo.list_for_run.return_value = recon_rows
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = [_make_lb(101, name_ml="Some LB"), _make_lb(102, name_ml="Other LB")]

        with patch(_GET_SESSION, _noop_session()):
            with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                with patch(_RECON_REPO, return_value=mock_recon_repo):
                    with patch(_LB_REPO, return_value=mock_lb_repo):
                        runner = CliRunner()
                        result = runner.invoke(main, ["reconcile-report", "--csv"])

        assert result.exit_code == 0

        reader = csv.reader(io.StringIO(result.output))
        parsed = list(reader)
        header = parsed[0]
        assert "scrape_run_id" in header
        assert "lb_id" in header
        assert "status" in header
        assert "delta" in header
        # header + 2 data rows
        assert len(parsed) == 3

    def test_csv_with_status_filter(self) -> None:
        run = _make_run(run_id=5)
        mock_run_repo = MagicMock()
        mock_run_repo.list_recent.return_value = [run]
        mock_recon_repo = MagicMock()
        mock_recon_repo.list_for_run.return_value = [
            _make_recon_row(run_id=5, lb_id=200, status="mismatch"),
        ]
        mock_lb_repo = MagicMock()
        mock_lb_repo.list_all.return_value = [_make_lb(200)]

        with patch(_GET_SESSION, _noop_session()):
            with patch(_SCRAPE_RUN_REPO, return_value=mock_run_repo):
                with patch(_RECON_REPO, return_value=mock_recon_repo):
                    with patch(_LB_REPO, return_value=mock_lb_repo):
                        runner = CliRunner()
                        result = runner.invoke(
                            main, ["reconcile-report", "--csv", "--status", "mismatch"]
                        )

        assert result.exit_code == 0
        # Repo must have been called with the filter.
        mock_recon_repo.list_for_run.assert_called_once_with(run.id, status_filter="mismatch")


# ---------------------------------------------------------------------------
# 6. worker and flower mock subprocess
# ---------------------------------------------------------------------------

class TestWorkerFlower:
    def test_worker_invokes_subprocess(self) -> None:
        runner = CliRunner()
        with patch("sakarma.cli.subprocess.run") as mock_run:
            result = runner.invoke(main, ["worker"])
        assert result.exit_code == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "celery"
        assert "worker" in cmd
        # Must include all required SAKARMA queues.
        queue_arg = " ".join(cmd)
        assert "sakarma_orchestrate" in queue_arg
        assert "sakarma_discovery" in queue_arg

    def test_flower_invokes_subprocess(self) -> None:
        runner = CliRunner()
        with patch("sakarma.cli.subprocess.run") as mock_run:
            result = runner.invoke(main, ["flower"])
        assert result.exit_code == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "flower" in cmd
        assert "--port=5556" in cmd
        # Broker URL must be included.
        assert any("--broker=" in arg for arg in cmd)


# ---------------------------------------------------------------------------
# 7. Integration tests (marked, require real Postgres)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestBackfillIntegration:
    """Integration tests against the seeded test database."""

    def test_backfill_dry_run_district_shows_lbs(self, db_session) -> None:
        """backfill --district 2 --dry-run against a populated DB shows expected LBs."""
        from sakarma.db.repositories import (
            DistrictRepository,
            LBRepository,
            LBTypeRepository,
        )

        # Seed the DB.
        DistrictRepository(db_session).upsert(id=2, name_ml="Ernakulam", name_en="Ernakulam")
        LBTypeRepository(db_session).upsert(id=1, name_ml="Municipality", name_en="Municipality")
        LBRepository(db_session).upsert(id=501, district_id=2, lb_type_id=1, name_ml="LB Five-O-One")
        LBRepository(db_session).upsert(id=502, district_id=2, lb_type_id=1, name_ml="LB Five-O-Two")
        db_session.flush()

        # Provide a context manager that yields the real test session.
        @contextmanager
        def _test_session_ctx():
            yield db_session

        runner = CliRunner()
        with patch(_GET_SESSION, _test_session_ctx):
            result = runner.invoke(main, ["backfill", "--district", "2", "--dry-run"])

        assert result.exit_code == 0
        assert "501" in result.output
        assert "502" in result.output
