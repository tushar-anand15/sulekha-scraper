"""Tests for sakarma.tasks.reconciliation.

Unit tests are pure in-memory (no HTTP). Integration tests use the
``db_session`` fixture and are marked ``@pytest.mark.integration``.
"""

from __future__ import annotations

from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from sakarma.db.models import (
    CATEGORY_APPROVED,
    CATEGORY_CANCELLED,
    CATEGORY_INCOMPLETE,
    CATEGORY_ONGOING,
)
from sakarma.db.repositories import (
    DashboardKPISnapshotRepository,
    DistrictRepository,
    LBRepository,
    LBTypeRepository,
    MainGroupValueRepository,
    MeetingManifestRepository,
    ReconciliationRepository,
    ScrapeRunRepository,
    YearRepository,
)
from sakarma.tasks.reconciliation import _Repos, run_for_lb

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LB_ID = 1001
_YEAR_ID = 27
_MGV_ID = 1  # set properly in integration seeded fixture
_RUN_ID = 99


def _make_snapshot(
    *,
    lb_id: int = _LB_ID,
    year_id: int = _YEAR_ID,
    main_group_value_id: int = _MGV_ID,
    ongoing: int = 0,
    minutes_complete: int = 0,
    minutes_incomplete: int = 0,
    cancelled: int = 0,
) -> MagicMock:
    snap = MagicMock()
    snap.lb_id = lb_id
    snap.year_id = year_id
    snap.main_group_value_id = main_group_value_id
    snap.ongoing = ongoing
    snap.minutes_complete = minutes_complete
    snap.minutes_incomplete = minutes_incomplete
    snap.cancelled = cancelled
    return snap


def _make_repos(
    *,
    snapshots: list[MagicMock] | None = None,
    manifest_counts: dict[int, int] | None = None,
    manifest_groups: list[tuple[int, int, int]] | None = None,
    upserted_rows: list[dict[str, Any]] | None = None,
) -> _Repos:
    """Build a _Repos with mocked repositories.

    Args:
        snapshots: List of mock snapshots returned by list_for_lb_run.
        manifest_counts: Mapping {category: count} returned by
            count_by_lb_year_group_category (same for all calls).
        manifest_groups: List of (year_id, mgv_id, category) tuples
            returned by list_groups_for_lb_run.
        upserted_rows: If provided, a list that receives the rows passed
            to upsert_many so callers can inspect them.
    """
    snapshots = snapshots or []
    manifest_counts = manifest_counts or {}
    manifest_groups = manifest_groups or []

    kpi_repo = MagicMock()
    kpi_repo.list_for_lb_run.return_value = snapshots

    manifest_repo = MagicMock()
    manifest_repo.count_by_lb_year_group_category.side_effect = (
        lambda lb_id, year_id, main_group_value_id, category: manifest_counts.get(
            category, 0
        )
    )
    manifest_repo.list_groups_for_lb_run.return_value = manifest_groups

    recon_repo = MagicMock()
    if upserted_rows is not None:
        captured: list[dict[str, Any]] = upserted_rows

        def _capture(rows: list[dict[str, Any]]) -> int:
            captured.extend(rows)
            return len(rows)

        recon_repo.upsert_many.side_effect = _capture
    else:
        recon_repo.upsert_many.return_value = 0

    return _Repos(
        dashboard_kpi_snapshot_repo=kpi_repo,
        meeting_manifest_repo=manifest_repo,
        reconciliation_repo=recon_repo,
    )


# ---------------------------------------------------------------------------
# Unit tests — no DB required
# ---------------------------------------------------------------------------


class TestRunForLbUnit:
    """Pure unit tests using mocked repositories."""

    def test_happy_matched_all_equal(self) -> None:
        """KPI = 34 across all categories, manifest = 34 → all matched."""
        snap = _make_snapshot(
            ongoing=34,
            minutes_complete=34,
            minutes_incomplete=34,
            cancelled=34,
        )
        rows: list[dict[str, Any]] = []
        repos = _make_repos(
            snapshots=[snap],
            manifest_counts={
                CATEGORY_ONGOING: 34,
                CATEGORY_APPROVED: 34,
                CATEGORY_INCOMPLETE: 34,
                CATEGORY_CANCELLED: 34,
            },
            upserted_rows=rows,
        )
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)

        assert summary["matched"] == 4
        assert summary["mismatch"] == 0
        assert summary["missing_kpi"] == 0
        assert summary["missing_manifest"] == 0
        assert summary["reconciliation_rows"] == 4
        for row in rows:
            assert row["delta"] == 0
            assert row["status"] == "matched"

    def test_happy_mismatch_under_collected(self) -> None:
        """KPI = 34, manifest = 30 for CATEGORY_ONGOING → mismatch, delta = 4."""
        snap = _make_snapshot(ongoing=34)
        rows: list[dict[str, Any]] = []
        repos = _make_repos(
            snapshots=[snap],
            manifest_counts={CATEGORY_ONGOING: 30},
            upserted_rows=rows,
        )
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)

        ongoing_row = next(r for r in rows if r["category"] == CATEGORY_ONGOING)
        assert ongoing_row["dashboard_kpi_count"] == 34
        assert ongoing_row["manifest_row_count"] == 30
        assert ongoing_row["delta"] == 4
        assert ongoing_row["status"] == "mismatch"
        assert summary["mismatch"] == 1

    def test_edge_both_zero_matched(self) -> None:
        """KPI = 0, manifest = 0 → matched with delta = 0 (empty cell still gets a row)."""
        snap = _make_snapshot()  # all zeros by default
        rows: list[dict[str, Any]] = []
        repos = _make_repos(
            snapshots=[snap],
            manifest_counts={},  # all will default to 0
            upserted_rows=rows,
        )
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)

        assert summary["matched"] == 4
        assert summary["reconciliation_rows"] == 4
        for row in rows:
            assert row["delta"] == 0
            assert row["status"] == "matched"

    def test_edge_kpi_zero_manifest_rows_exist_mismatch(self) -> None:
        """KPI = 0, manifest = 2 → mismatch, delta = -2."""
        snap = _make_snapshot(ongoing=0)
        rows: list[dict[str, Any]] = []
        repos = _make_repos(
            snapshots=[snap],
            manifest_counts={CATEGORY_ONGOING: 2},
            upserted_rows=rows,
        )
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)

        ongoing_row = next(r for r in rows if r["category"] == CATEGORY_ONGOING)
        assert ongoing_row["dashboard_kpi_count"] == 0
        assert ongoing_row["manifest_row_count"] == 2
        assert ongoing_row["delta"] == -2
        assert ongoing_row["status"] == "mismatch"
        assert summary["mismatch"] == 1

    def test_edge_kpi_exists_no_manifest_rows_missing_manifest(self) -> None:
        """KPI snapshot exists but manifest enumeration produced zero rows → MISSING_MANIFEST."""
        snap = _make_snapshot(ongoing=10)
        rows: list[dict[str, Any]] = []
        repos = _make_repos(
            snapshots=[snap],
            manifest_counts={},  # 0 for all categories
            upserted_rows=rows,
        )
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)

        ongoing_row = next(r for r in rows if r["category"] == CATEGORY_ONGOING)
        assert ongoing_row["status"] == "missing_manifest"
        assert ongoing_row["manifest_row_count"] == 0
        assert ongoing_row["dashboard_kpi_count"] == 10
        assert summary["missing_manifest"] == 1

    def test_edge_manifest_rows_exist_no_kpi_snapshot(self) -> None:
        """Manifest rows exist for a cell with no KPI snapshot → MISSING_KPI row."""
        # No KPI snapshots at all, but manifest groups exist.
        rows: list[dict[str, Any]] = []
        repos = _make_repos(
            snapshots=[],
            manifest_counts={CATEGORY_APPROVED: 5},
            manifest_groups=[(_YEAR_ID, _MGV_ID, CATEGORY_APPROVED)],
            upserted_rows=rows,
        )
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)

        assert summary["missing_kpi"] == 1
        assert summary["reconciliation_rows"] == 1
        assert rows[0]["status"] == "missing_kpi"
        assert rows[0]["dashboard_kpi_count"] == 0
        assert rows[0]["manifest_row_count"] == 5
        assert rows[0]["delta"] == -5

    def test_manifest_groups_covered_by_snapshot_not_duplicated(self) -> None:
        """If a manifest group (year, mgv, cat) is already covered by a KPI snapshot,
        it must NOT generate an additional MISSING_KPI row."""
        snap = _make_snapshot(
            year_id=_YEAR_ID,
            main_group_value_id=_MGV_ID,
            ongoing=5,
        )
        # list_groups_for_lb_run returns the same (year, mgv) that the snapshot covers
        rows: list[dict[str, Any]] = []
        repos = _make_repos(
            snapshots=[snap],
            manifest_counts={CATEGORY_ONGOING: 5},
            manifest_groups=[
                (_YEAR_ID, _MGV_ID, CATEGORY_ONGOING),
                (_YEAR_ID, _MGV_ID, CATEGORY_APPROVED),
            ],
            upserted_rows=rows,
        )
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)

        # All 4 categories from the snapshot + 0 extra MISSING_KPI (both manifest
        # groups share the same (year, mgv) that the snapshot covers).
        assert summary["reconciliation_rows"] == 4
        assert summary["missing_kpi"] == 0

    def test_summary_dict_keys(self) -> None:
        """Return value must always have exactly the five expected keys."""
        repos = _make_repos()
        summary = run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)
        assert set(summary) == {
            "reconciliation_rows",
            "matched",
            "mismatch",
            "missing_kpi",
            "missing_manifest",
        }

    def test_upsert_many_called_once(self) -> None:
        """upsert_many must be called exactly once per run_for_lb invocation."""
        snap = _make_snapshot(ongoing=1)
        repos = _make_repos(snapshots=[snap])
        run_for_lb(repos, lb_id=_LB_ID, scrape_run_id=_RUN_ID)
        repos.reconciliation_repo.upsert_many.assert_called_once()


# ---------------------------------------------------------------------------
# Integration tests — require Postgres via db_session fixture
# ---------------------------------------------------------------------------


pytestmark_integration = pytest.mark.integration


def _manifest_row_dict(
    *,
    lb_id: int,
    year_id: int,
    main_group_value_id: int,
    category: int,
    scrape_run_id: int,
    meeting_no: str,
    meeting_date: date,
) -> dict[str, Any]:
    return {
        "lb_id": lb_id,
        "year_id": year_id,
        "main_group_value_id": main_group_value_id,
        "category": category,
        "dashboard_grid_select_index": 0,
        "dr_postback_target": "Select$0",
        "meeting_no_label": meeting_no,
        "meeting_date": meeting_date,
        "meeting_type": "Regular",
        "meeting_nature": "General",
        "meeting_venue": "Hall",
        "scrape_run_id": scrape_run_id,
    }


@pytest.fixture
def seeded(db_session):
    """Seed the minimum universe for reconciliation integration tests."""
    DistrictRepository(db_session).upsert(id=1, name_ml="തിരുവനന്തപുരം")
    LBTypeRepository(db_session).upsert(id=2, name_ml="Grama Panchayat")
    YearRepository(db_session).upsert(id=27, year_int=2016)
    LBRepository(db_session).upsert(id=1001, district_id=1, lb_type_id=2, name_ml="LB One")
    mg = MainGroupValueRepository(db_session).upsert(
        lb_id=1001, ddl_value=5, name_ml="Main Group Five"
    )
    run = ScrapeRunRepository(db_session).create(kind="backfill")
    db_session.flush()
    return {
        "lb_id": 1001,
        "year_id": 27,
        "main_group_value_id": mg.id,
        "scrape_run_id": run.id,
    }


def _build_repos(db_session) -> _Repos:
    return _Repos(
        dashboard_kpi_snapshot_repo=DashboardKPISnapshotRepository(db_session),
        meeting_manifest_repo=MeetingManifestRepository(db_session),
        reconciliation_repo=ReconciliationRepository(db_session),
    )


@pytest.mark.integration
class TestRunForLbIntegration:
    """Integration tests using a real Postgres session."""

    def test_happy_matched(self, db_session, seeded) -> None:
        """KPI = 34, manifest = 34 → matched row with delta = 0."""
        lb_id = seeded["lb_id"]
        year_id = seeded["year_id"]
        mgv_id = seeded["main_group_value_id"]
        run_id = seeded["scrape_run_id"]

        # Insert KPI snapshot
        DashboardKPISnapshotRepository(db_session).upsert(
            lb_id=lb_id,
            year_id=year_id,
            main_group_value_id=mgv_id,
            scrape_run_id=run_id,
            total_meetings=34,
            ongoing=34,
            minutes_complete=0,
            minutes_incomplete=0,
            cancelled=0,
        )
        # Insert 34 manifest rows for CATEGORY_ONGOING
        manifest_rows = [
            _manifest_row_dict(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=CATEGORY_ONGOING,
                scrape_run_id=run_id,
                meeting_no=f"M{i}",
                meeting_date=date(2016, 4, i % 28 + 1),
            )
            for i in range(34)
        ]
        MeetingManifestRepository(db_session).upsert_many(manifest_rows)
        db_session.flush()

        repos = _build_repos(db_session)
        summary = run_for_lb(repos, lb_id=lb_id, scrape_run_id=run_id)

        # One snapshot → 4 categories → 4 rows
        assert summary["reconciliation_rows"] == 4

        recon_rows = ReconciliationRepository(db_session).list_for_run(run_id)
        assert len(recon_rows) == 4

        ongoing_row = next(r for r in recon_rows if r.category == CATEGORY_ONGOING)
        assert ongoing_row.dashboard_kpi_count == 34
        assert ongoing_row.manifest_row_count == 34
        assert ongoing_row.delta == 0
        assert ongoing_row.status == "matched"

        # All other categories have KPI=0, manifest=0 → matched too
        other_rows = [r for r in recon_rows if r.category != CATEGORY_ONGOING]
        for r in other_rows:
            assert r.status == "matched"
            assert r.delta == 0

    def test_mismatch_under_collected(self, db_session, seeded) -> None:
        """KPI = 34, manifest = 30 → mismatch with delta = 4."""
        lb_id = seeded["lb_id"]
        year_id = seeded["year_id"]
        mgv_id = seeded["main_group_value_id"]
        run_id = seeded["scrape_run_id"]

        DashboardKPISnapshotRepository(db_session).upsert(
            lb_id=lb_id,
            year_id=year_id,
            main_group_value_id=mgv_id,
            scrape_run_id=run_id,
            total_meetings=34,
            ongoing=34,
            minutes_complete=0,
            minutes_incomplete=0,
            cancelled=0,
        )
        # Only 30 manifest rows for ongoing
        manifest_rows = [
            _manifest_row_dict(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=CATEGORY_ONGOING,
                scrape_run_id=run_id,
                meeting_no=f"M{i}",
                meeting_date=date(2016, 4, i % 28 + 1),
            )
            for i in range(30)
        ]
        MeetingManifestRepository(db_session).upsert_many(manifest_rows)
        db_session.flush()

        repos = _build_repos(db_session)
        summary = run_for_lb(repos, lb_id=lb_id, scrape_run_id=run_id)

        assert summary["mismatch"] == 1

        recon_rows = ReconciliationRepository(db_session).list_for_run(run_id)
        ongoing_row = next(r for r in recon_rows if r.category == CATEGORY_ONGOING)
        assert ongoing_row.delta == 4
        assert ongoing_row.status == "mismatch"

    def test_missing_manifest(self, db_session, seeded) -> None:
        """KPI snapshot exists but manifest row count is 0 → missing_manifest."""
        lb_id = seeded["lb_id"]
        year_id = seeded["year_id"]
        mgv_id = seeded["main_group_value_id"]
        run_id = seeded["scrape_run_id"]

        DashboardKPISnapshotRepository(db_session).upsert(
            lb_id=lb_id,
            year_id=year_id,
            main_group_value_id=mgv_id,
            scrape_run_id=run_id,
            total_meetings=5,
            ongoing=5,
            minutes_complete=0,
            minutes_incomplete=0,
            cancelled=0,
        )
        # No manifest rows inserted at all
        db_session.flush()

        repos = _build_repos(db_session)
        summary = run_for_lb(repos, lb_id=lb_id, scrape_run_id=run_id)

        assert summary["missing_manifest"] == 1
        recon_rows = ReconciliationRepository(db_session).list_for_run(run_id)
        ongoing_row = next(r for r in recon_rows if r.category == CATEGORY_ONGOING)
        assert ongoing_row.status == "missing_manifest"
        assert ongoing_row.manifest_row_count == 0
        assert ongoing_row.dashboard_kpi_count == 5

    def test_missing_kpi_manifest_rows_no_snapshot(self, db_session, seeded) -> None:
        """Manifest rows exist for a cell but no KPI snapshot → MISSING_KPI."""
        lb_id = seeded["lb_id"]
        year_id = seeded["year_id"]
        mgv_id = seeded["main_group_value_id"]
        run_id = seeded["scrape_run_id"]

        # Insert manifest rows but NO KPI snapshot
        manifest_rows = [
            _manifest_row_dict(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=CATEGORY_APPROVED,
                scrape_run_id=run_id,
                meeting_no=f"A{i}",
                meeting_date=date(2016, 4, i + 1),
            )
            for i in range(3)
        ]
        MeetingManifestRepository(db_session).upsert_many(manifest_rows)
        db_session.flush()

        repos = _build_repos(db_session)
        summary = run_for_lb(repos, lb_id=lb_id, scrape_run_id=run_id)

        assert summary["missing_kpi"] == 1
        assert summary["reconciliation_rows"] == 1
        recon_rows = ReconciliationRepository(db_session).list_for_run(run_id)
        assert len(recon_rows) == 1
        r = recon_rows[0]
        assert r.status == "missing_kpi"
        assert r.dashboard_kpi_count == 0
        assert r.manifest_row_count == 3
        assert r.delta == -3

    def test_idempotency_same_row_count_no_duplicates(self, db_session, seeded) -> None:
        """Running reconciliation twice produces the same rows without duplicates."""
        lb_id = seeded["lb_id"]
        year_id = seeded["year_id"]
        mgv_id = seeded["main_group_value_id"]
        run_id = seeded["scrape_run_id"]

        DashboardKPISnapshotRepository(db_session).upsert(
            lb_id=lb_id,
            year_id=year_id,
            main_group_value_id=mgv_id,
            scrape_run_id=run_id,
            total_meetings=10,
            ongoing=10,
            minutes_complete=0,
            minutes_incomplete=0,
            cancelled=0,
        )
        manifest_rows = [
            _manifest_row_dict(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=CATEGORY_ONGOING,
                scrape_run_id=run_id,
                meeting_no=f"M{i}",
                meeting_date=date(2016, 4, i % 28 + 1),
            )
            for i in range(10)
        ]
        MeetingManifestRepository(db_session).upsert_many(manifest_rows)
        db_session.flush()

        repos = _build_repos(db_session)

        # First run
        summary1 = run_for_lb(repos, lb_id=lb_id, scrape_run_id=run_id)
        rows_after_first = ReconciliationRepository(db_session).list_for_run(run_id)

        # Second run (idempotent)
        summary2 = run_for_lb(repos, lb_id=lb_id, scrape_run_id=run_id)
        rows_after_second = ReconciliationRepository(db_session).list_for_run(run_id)

        assert summary1["reconciliation_rows"] == summary2["reconciliation_rows"]
        assert len(rows_after_first) == len(rows_after_second)

        # delta values should be identical
        def _deltas(rows):
            return {(r.category, r.year_id, r.main_group_value_id): r.delta for r in rows}

        assert _deltas(rows_after_first) == _deltas(rows_after_second)

    def test_full_run_row_count(self, db_session, seeded) -> None:
        """Full integration: N=(years × main_groups × 4 categories) reconciliation rows."""
        lb_id = seeded["lb_id"]
        year_id = seeded["year_id"]
        mgv_id = seeded["main_group_value_id"]
        run_id = seeded["scrape_run_id"]

        # Insert one KPI snapshot (1 year × 1 main_group)
        DashboardKPISnapshotRepository(db_session).upsert(
            lb_id=lb_id,
            year_id=year_id,
            main_group_value_id=mgv_id,
            scrape_run_id=run_id,
            total_meetings=2,
            ongoing=1,
            minutes_complete=1,
            minutes_incomplete=0,
            cancelled=0,
        )
        # Insert manifest rows for two categories
        manifest_rows = [
            _manifest_row_dict(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=CATEGORY_ONGOING,
                scrape_run_id=run_id,
                meeting_no="MO1",
                meeting_date=date(2016, 4, 1),
            ),
            _manifest_row_dict(
                lb_id=lb_id,
                year_id=year_id,
                main_group_value_id=mgv_id,
                category=CATEGORY_APPROVED,
                scrape_run_id=run_id,
                meeting_no="MA1",
                meeting_date=date(2016, 4, 2),
            ),
        ]
        MeetingManifestRepository(db_session).upsert_many(manifest_rows)
        db_session.flush()

        repos = _build_repos(db_session)
        summary = run_for_lb(repos, lb_id=lb_id, scrape_run_id=run_id)

        # 1 year × 1 main_group × 4 categories = 4 rows
        expected_rows = 1 * 1 * 4
        assert summary["reconciliation_rows"] == expected_rows
        actual_rows = ReconciliationRepository(db_session).list_for_run(run_id)
        assert len(actual_rows) == expected_rows
        assert summary["matched"] == 2  # ongoing matched + approved matched
        assert summary["missing_manifest"] == 0  # both have rows
        assert summary["mismatch"] == 0
        assert summary["missing_kpi"] == 0
