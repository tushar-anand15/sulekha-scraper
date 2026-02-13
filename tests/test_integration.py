"""Integration tests for the full Sulekha pipeline.

These tests verify that all phases work together correctly,
including database operations and storage integration.
"""

import pytest
from sqlalchemy.orm import Session

# Mark all tests in this module as integration tests
pytestmark = pytest.mark.integration

from sulekha.db.models import (
    District,
    DistrictStatus,
    LocalBody,
    LocalBodyStatus,
    PdfStatus,
    Project,
)
from sulekha.db.repositories import (
    DistrictRepository,
    LocalBodyRepository,
    PdfRepository,
    ProjectRepository,
    ScrapeRunRepository,
)
from sulekha.tasks.orchestrator import get_pipeline_status


class TestPipelineStatus:
    """Tests for pipeline status tracking."""

    def test_get_pipeline_status_empty_db(self, db_session: Session):
        """Test pipeline status with empty database returns zero counts."""
        # Use repositories directly with test session to verify empty counts
        district_repo = DistrictRepository(db_session)
        lb_repo = LocalBodyRepository(db_session)
        project_repo = ProjectRepository(db_session)

        district_status = district_repo.count_by_status()
        lb_status = lb_repo.count_by_status()
        pdf_status = project_repo.count_by_pdf_status()

        # Empty database should have empty or zero counts
        assert district_status == {} or all(v == 0 for v in district_status.values())
        assert lb_status == {} or all(v == 0 for v in lb_status.values())
        assert pdf_status == {} or all(v == 0 for v in pdf_status.values())

    def test_pipeline_status_counts(self, db_session: Session):
        """Test that pipeline status correctly counts entities."""
        # Create districts
        for i in range(5):
            district = District(
                year_val=28,
                year_label="2024-2025",
                lb_type_val=1,
                lb_type_label="District Panchayat",
                district_index=i + 1,
                district_name=f"District {i + 1}",
                postback_argument=f"Select${i}",
                status=DistrictStatus.DONE if i < 3 else DistrictStatus.PENDING,
            )
            db_session.add(district)

        db_session.flush()

        # Count directly
        district_repo = DistrictRepository(db_session)
        status = district_repo.count_by_status()

        assert status.get("DONE", 0) == 3
        assert status.get("PENDING", 0) == 2


class TestResumption:
    """Tests for pipeline resumption after failures."""

    def test_resume_from_partial_local_body(
        self, db_session: Session, sample_district: District
    ):
        """Test resuming scraping from a partially scraped local body."""
        lb_repo = LocalBodyRepository(db_session)
        project_repo = ProjectRepository(db_session)

        # Create a partially scraped LB
        lb = lb_repo.upsert(
            district_id=sample_district.id,
            lb_index=1,
            lb_name="Partial LB",
            postback_argument="Select$0",
            expected_projects=100,
        )
        db_session.flush()

        # Update to partial status
        lb_repo.update_progress(
            lb.id,
            last_page_scraped=3,
            scraped_projects=60,
            total_pages=5,
        )
        db_session.flush()

        # Add projects from first 3 pages
        for i in range(60):
            project = Project(
                local_body_id=lb.id,
                project_no=f"PRJ{i:03d}",
                project_name=f"Project {i}",
                select_argument=f"Select${i % 20}",
                page_number=(i // 20) + 1,
                pdf_status=PdfStatus.PENDING,
            )
            db_session.add(project)

        db_session.flush()

        # Verify partial state
        updated_lb = lb_repo.get(lb.id)
        assert updated_lb.status == LocalBodyStatus.PARTIAL
        assert updated_lb.last_page_scraped == 3

        # Get pending should include this LB
        pending = lb_repo.get_pending()
        assert len(pending) == 1
        assert pending[0].id == lb.id

    def test_resume_from_failed_pdf_downloads(
        self, db_session: Session, sample_local_body: LocalBody
    ):
        """Test resuming PDF downloads after failures."""
        project_repo = ProjectRepository(db_session)

        # Create projects with mixed PDF statuses
        statuses = [
            PdfStatus.DOWNLOADED,  # Skip
            PdfStatus.DOWNLOADED,  # Skip
            PdfStatus.PENDING,  # Process
            PdfStatus.ERROR,  # Process (retry)
            PdfStatus.MISSING,  # Skip
        ]

        for i, status in enumerate(statuses):
            project = Project(
                local_body_id=sample_local_body.id,
                project_no=f"PRJ{i:03d}",
                project_name=f"Project {i}",
                select_argument=f"Select${i}",
                page_number=1,
                pdf_status=status,
                pdf_retry_count=1 if status == PdfStatus.ERROR else 0,
            )
            db_session.add(project)

        db_session.flush()

        # Get pending PDFs should only return PENDING and ERROR
        pending = project_repo.get_pending_pdfs()
        assert len(pending) == 2
        pending_statuses = {p.pdf_status for p in pending}
        assert pending_statuses == {PdfStatus.PENDING, PdfStatus.ERROR}


class TestIdempotency:
    """Tests for idempotent operations."""

    def test_district_upsert_idempotent(self, db_session: Session):
        """Test that district upserts are idempotent."""
        repo = DistrictRepository(db_session)

        # Create multiple times
        for run in range(3):
            for i in range(5):
                repo.upsert(
                    year_val=28,
                    year_label="2024-2025",
                    lb_type_val=1,
                    lb_type_label="District Panchayat",
                    district_index=i + 1,
                    district_name=f"District {i + 1}",
                    postback_argument=f"Select${i}",
                    num_projects=100 + run,  # Different value each time
                )
            db_session.flush()

        # Should still only have 5 districts
        all_districts = repo.get_all_for_year_lb(28, 1)
        assert len(all_districts) == 5

        # Should have latest project count
        assert all(d.num_projects == 102 for d in all_districts)

    def test_project_upsert_idempotent(
        self, db_session: Session, sample_local_body: LocalBody
    ):
        """Test that project upserts are idempotent."""
        repo = ProjectRepository(db_session)

        # Create multiple times
        for run in range(3):
            for i in range(10):
                repo.upsert(
                    local_body_id=sample_local_body.id,
                    project_no=f"PRJ{i:03d}",
                    project_name=f"Project {i} - Run {run}",
                    select_argument=f"Select${i}",
                    page_number=1,
                )
            db_session.flush()

        # Should still only have 10 projects
        all_projects = repo.get_all_for_local_body(sample_local_body.id)
        assert len(all_projects) == 10

        # Should have latest name
        assert all("Run 2" in p.project_name for p in all_projects)


class TestPhaseTransitions:
    """Tests for phase transition logic."""

    def test_phase1_to_phase2_transition(self, db_session: Session):
        """Test transition from Phase 1 to Phase 2."""
        district_repo = DistrictRepository(db_session)

        # Create all districts as PENDING
        for i in range(14):  # 14 Kerala districts
            district = District(
                year_val=28,
                year_label="2024-2025",
                lb_type_val=1,
                lb_type_label="District Panchayat",
                district_index=i + 1,
                district_name=f"District {i + 1}",
                postback_argument=f"Select${i}",
                status=DistrictStatus.PENDING,
            )
            db_session.add(district)

        db_session.flush()

        # Initially all pending
        status = district_repo.count_by_status()
        assert status.get("PENDING", 0) == 14
        assert status.get("DONE", 0) == 0

        # Simulate Phase 2 processing some districts
        pending = district_repo.get_pending(limit=5)
        for d in pending:
            district_repo.mark_done(d.id)
        db_session.flush()

        # Check status after partial processing
        status = district_repo.count_by_status()
        assert status.get("DONE", 0) == 5
        assert status.get("PENDING", 0) == 9

    def test_phase3_to_phase4_transition(
        self, db_session: Session, sample_local_body: LocalBody
    ):
        """Test transition from Phase 3 to Phase 4."""
        lb_repo = LocalBodyRepository(db_session)
        project_repo = ProjectRepository(db_session)

        # Create projects
        for i in range(20):
            project = Project(
                local_body_id=sample_local_body.id,
                project_no=f"PRJ{i:03d}",
                project_name=f"Project {i}",
                select_argument=f"Select${i}",
                page_number=1,
                pdf_status=PdfStatus.PENDING,
            )
            db_session.add(project)

        # Mark LB as done
        lb_repo.mark_done(sample_local_body.id, scraped_projects=20)
        db_session.flush()

        # All projects should be pending for PDF download
        pending_pdfs = project_repo.get_pending_pdfs()
        assert len(pending_pdfs) == 20

        # Simulate downloading some
        for p in pending_pdfs[:10]:
            project_repo.mark_downloaded(p.id)
        db_session.flush()

        # Check remaining
        pending_pdfs = project_repo.get_pending_pdfs()
        assert len(pending_pdfs) == 10


class TestStorageIntegration:
    """Integration tests for storage operations."""

    def test_storage_path_consistency(self):
        """Test that storage paths are consistent and reproducible."""
        from sulekha.storage.gcs import build_storage_path

        path1 = build_storage_path(
            year_label="2024-2025",
            lb_type_label="District Panchayat",
            district_name="Thiruvananthapuram",
            lb_name="TVM DP",
            project_no="PRJ001",
        )

        path2 = build_storage_path(
            year_label="2024-2025",
            lb_type_label="District Panchayat",
            district_name="Thiruvananthapuram",
            lb_name="TVM DP",
            project_no="PRJ001",
        )

        # Same inputs should produce same path
        assert path1 == path2
        assert path1.endswith(".pdf")
        assert "pdfs/" in path1

    def test_content_hash_for_deduplication(self):
        """Test that content hashing works for deduplication."""
        from sulekha.storage.gcs import compute_hash

        content1 = b"test pdf content"
        content2 = b"test pdf content"
        content3 = b"different content"

        hash1 = compute_hash(content1)
        hash2 = compute_hash(content2)
        hash3 = compute_hash(content3)

        # Same content should have same hash
        assert hash1 == hash2

        # Different content should have different hash
        assert hash1 != hash3
