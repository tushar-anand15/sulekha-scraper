"""Integration tests for SAKARMA repositories.

All tests in this file require a reachable Postgres instance. They are marked
``@pytest.mark.integration`` so the whole file can be deselected when Postgres
isn't available.
"""

from __future__ import annotations

from datetime import date

import pytest

from sakarma.db.models import (
    CATEGORY_APPROVED,
    CATEGORY_ONGOING,
    LBProgress,
    MeetingArtifact,
    MeetingManifest,
    Reconciliation,
)
from sakarma.db.repositories import (
    DashboardKPISnapshotRepository,
    DistrictRepository,
    LBProgressRepository,
    LBRepository,
    LBTypeRepository,
    MainGroupValueRepository,
    MeetingArtifactRepository,
    MeetingManifestRepository,
    ReconciliationRepository,
    ScrapeRunRepository,
    YearRepository,
)

pytestmark = pytest.mark.integration


# =============================================================================
# Fixtures: a minimal LB universe (district, lb_type, year, lb, main_group)
# =============================================================================


@pytest.fixture
def seeded(db_session):
    """Seed the universe needed by manifest/artifact/recon tests."""
    DistrictRepository(db_session).upsert(id=1, name_ml="തിരുവനന്തപുരം")
    LBTypeRepository(db_session).upsert(id=2, name_ml="Grama Panchayat")
    YearRepository(db_session).upsert(id=27, year_int=2016)
    LBRepository(db_session).upsert(
        id=1001, district_id=1, lb_type_id=2, name_ml="LB One"
    )
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


# =============================================================================
# LBProgress state-machine
# =============================================================================


def test_lb_progress_bulk_create_and_state_transitions(db_session, seeded) -> None:
    repo = LBProgressRepository(db_session)
    # Add a second LB so we can test bulk insert of multiple rows.
    LBRepository(db_session).upsert(
        id=1002, district_id=1, lb_type_id=2, name_ml="LB Two"
    )
    db_session.flush()

    rows = repo.bulk_create(seeded["scrape_run_id"], [1001, 1002])
    assert len(rows) == 2
    assert all(r.status == "pending" for r in rows)

    target = rows[0]
    repo.mark_in_progress(target.id)
    refreshed = db_session.get(LBProgress, target.id)
    assert refreshed.status == "in_progress"
    assert refreshed.started_at is not None

    repo.mark_stage(target.id, "manifest")
    db_session.expire_all()
    assert db_session.get(LBProgress, target.id).current_stage == "manifest"

    repo.mark_done(target.id)
    db_session.expire_all()
    done = db_session.get(LBProgress, target.id)
    assert done.status == "done"
    assert done.completed_at is not None

    # error path on the other row
    other = rows[1]
    repo.mark_error(other.id, "boom")
    db_session.expire_all()
    err = db_session.get(LBProgress, other.id)
    assert err.status == "error"
    assert err.error_message == "boom"
    assert err.completed_at is not None


def test_lb_progress_bulk_create_idempotent(db_session, seeded) -> None:
    repo = LBProgressRepository(db_session)
    repo.bulk_create(seeded["scrape_run_id"], [1001])
    repo.bulk_create(seeded["scrape_run_id"], [1001])  # duplicate
    rows = repo.list_for_run(seeded["scrape_run_id"])
    assert len(rows) == 1


# =============================================================================
# MeetingManifest idempotent upsert
# =============================================================================


def _manifest_row(seeded, *, meeting_no: str, meeting_date: date, category: int):
    return {
        "lb_id": seeded["lb_id"],
        "year_id": seeded["year_id"],
        "main_group_value_id": seeded["main_group_value_id"],
        "category": category,
        "dashboard_grid_select_index": 0,
        "dr_postback_target": "Select$0",
        "meeting_no_label": meeting_no,
        "meeting_date": meeting_date,
        "meeting_type": "Regular",
        "meeting_nature": "General",
        "meeting_venue": "Hall",
        "scrape_run_id": seeded["scrape_run_id"],
    }


def test_meeting_manifest_upsert_many_idempotent(db_session, seeded) -> None:
    repo = MeetingManifestRepository(db_session)
    rows = [
        _manifest_row(seeded, meeting_no="M1", meeting_date=date(2016, 4, 1), category=CATEGORY_APPROVED),
        _manifest_row(seeded, meeting_no="M2", meeting_date=date(2016, 5, 1), category=CATEGORY_APPROVED),
    ]
    n1 = repo.upsert_many([dict(r) for r in rows])
    assert n1 == 2

    # Second invocation with identical rows should not change row count.
    n2 = repo.upsert_many([dict(r) for r in rows])
    assert n2 == 2  # rows submitted, but net change is zero

    count = db_session.query(MeetingManifest).count()
    assert count == 2

    # Aggregation
    cat_count = repo.count_by_lb_year_group_category(
        seeded["lb_id"],
        seeded["year_id"],
        seeded["main_group_value_id"],
        CATEGORY_APPROVED,
    )
    assert cat_count == 2


# =============================================================================
# MeetingArtifact dedup on content_hash
# =============================================================================


def test_meeting_artifact_duplicate_hash_returns_existing(db_session, seeded) -> None:
    manifest_repo = MeetingManifestRepository(db_session)
    manifest_repo.upsert_many(
        [
            _manifest_row(
                seeded,
                meeting_no="M1",
                meeting_date=date(2016, 4, 1),
                category=CATEGORY_APPROVED,
            )
        ]
    )
    db_session.flush()
    manifest = manifest_repo.get_by_natural_key(
        seeded["lb_id"],
        seeded["year_id"],
        seeded["main_group_value_id"],
        CATEGORY_APPROVED,
        "M1",
        date(2016, 4, 1),
    )
    assert manifest is not None

    repo = MeetingArtifactRepository(db_session)
    a1 = repo.get_or_create(
        meeting_manifest_id=manifest.id,
        artifact_type="minutes_html",
        content_hash="a" * 64,
        gcs_path="gs://b/p1",
        byte_size=100,
        source_page_url="http://example/1",
        scrape_run_id=seeded["scrape_run_id"],
    )
    assert a1 is not None
    assert a1.content_hash == "a" * 64

    # Same hash, different gcs_path/url — should return EXISTING row, not error.
    a2 = repo.get_or_create(
        meeting_manifest_id=manifest.id,
        artifact_type="minutes_html",
        content_hash="a" * 64,
        gcs_path="gs://b/p2",
        byte_size=200,
        source_page_url="http://example/2",
        scrape_run_id=seeded["scrape_run_id"],
    )
    assert a2.id == a1.id
    assert a2.gcs_path == "gs://b/p1"  # original preserved

    total = db_session.query(MeetingArtifact).count()
    assert total == 1


# =============================================================================
# Reconciliation upsert on composite key
# =============================================================================


def test_reconciliation_upsert_many_updates_in_place(db_session, seeded) -> None:
    repo = ReconciliationRepository(db_session)
    base = {
        "scrape_run_id": seeded["scrape_run_id"],
        "lb_id": seeded["lb_id"],
        "year_id": seeded["year_id"],
        "main_group_value_id": seeded["main_group_value_id"],
        "category": CATEGORY_ONGOING,
    }

    repo.upsert_many(
        [
            {
                **base,
                "dashboard_kpi_count": 10,
                "manifest_row_count": 8,
                "delta": 2,
                "status": "mismatch",
            }
        ]
    )
    repo.upsert_many(
        [
            {
                **base,
                "dashboard_kpi_count": 10,
                "manifest_row_count": 10,
                "delta": 0,
                "status": "matched",
            }
        ]
    )

    rows = repo.list_for_run(seeded["scrape_run_id"])
    assert len(rows) == 1
    assert rows[0].delta == 0
    assert rows[0].status == "matched"

    total = db_session.query(Reconciliation).count()
    assert total == 1


# =============================================================================
# DashboardKPISnapshot idempotency
# =============================================================================


def test_kpi_snapshot_upsert_idempotent(db_session, seeded) -> None:
    repo = DashboardKPISnapshotRepository(db_session)
    s1 = repo.upsert(
        lb_id=seeded["lb_id"],
        year_id=seeded["year_id"],
        main_group_value_id=seeded["main_group_value_id"],
        scrape_run_id=seeded["scrape_run_id"],
        total_meetings=10,
        ongoing=1,
        minutes_complete=8,
        minutes_incomplete=1,
        cancelled=0,
    )
    s2 = repo.upsert(
        lb_id=seeded["lb_id"],
        year_id=seeded["year_id"],
        main_group_value_id=seeded["main_group_value_id"],
        scrape_run_id=seeded["scrape_run_id"],
        total_meetings=12,
        ongoing=2,
        minutes_complete=9,
        minutes_incomplete=1,
        cancelled=0,
    )
    assert s1.id == s2.id
    db_session.expire_all()
    fresh = repo._get(
        seeded["lb_id"],
        seeded["year_id"],
        seeded["main_group_value_id"],
        seeded["scrape_run_id"],
    )
    assert fresh.total_meetings == 12
    assert fresh.ongoing == 2


# =============================================================================
# ScrapeRun lifecycle
# =============================================================================


def test_scrape_run_lifecycle(db_session) -> None:
    repo = ScrapeRunRepository(db_session)
    run = repo.create(kind="diff")
    assert run.id is not None
    assert run.status == "running"

    repo.mark_done(run.id)
    db_session.expire_all()
    assert repo.get(run.id).status == "done"

    run2 = repo.create(kind="backfill")
    repo.mark_failed(run2.id, error_summary={"errors": ["boom"]})
    db_session.expire_all()
    failed = repo.get(run2.id)
    assert failed.status == "failed"
    assert failed.error_summary == {"errors": ["boom"]}
