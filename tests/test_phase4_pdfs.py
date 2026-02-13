"""Tests for Phase 4: PDF Download.

Phase 4 iterates through all discovered projects, clicks to get
the PDF redirect, downloads PDFs, and uploads to storage.
"""

import pytest
from sqlalchemy.orm import Session

from sulekha.db.models import GcsUploadStatus, Pdf, PdfStatus, Project
from sulekha.db.repositories import PdfRepository, ProjectRepository
from sulekha.storage.gcs import S3Storage, build_storage_path, compute_hash, slugify


class TestStoragePath:
    """Tests for storage path building utilities."""

    def test_slugify_basic(self):
        """Test basic slugification."""
        assert slugify("Hello World") == "Hello_World"
        assert slugify("Test  Multiple   Spaces") == "Test_Multiple_Spaces"

    def test_slugify_special_chars(self):
        """Test slugification removes special characters."""
        assert slugify('File<>:"/\\|?*Name') == "File_Name"

    def test_slugify_truncates(self):
        """Test slugification truncates long strings."""
        long_name = "A" * 100
        result = slugify(long_name, max_length=50)
        assert len(result) == 50

    def test_build_storage_path_structure(self):
        """Test that storage path has correct structure."""
        path = build_storage_path(
            year_label="2024-2025",
            lb_type_label="District Panchayat",
            district_name="Thiruvananthapuram",
            lb_name="TVM District Panchayat",
            project_no="PRJ001",
        )

        assert path.startswith("pdfs/")
        assert "2024-2025" in path
        assert "District_Panchayat" in path
        assert "Thiruvananthapuram" in path
        assert "PRJ001.pdf" in path

    def test_compute_hash_consistent(self):
        """Test that compute_hash returns consistent results."""
        content = b"test pdf content"
        hash1 = compute_hash(content)
        hash2 = compute_hash(content)

        assert hash1 == hash2
        assert len(hash1) == 64  # SHA256 hex length


class TestS3Storage:
    """Tests for S3/Minio storage backend."""

    @pytest.mark.skipif(
        True,  # Skip if Minio not available
        reason="Minio integration tests require running Minio"
    )
    def test_upload_and_download(self, storage, sample_pdf_bytes):
        """Test uploading and downloading a file."""
        path, content_hash, size = storage.upload_pdf(
            pdf_bytes=sample_pdf_bytes,
            year_label="2024-2025",
            lb_type_label="District Panchayat",
            district_name="Test District",
            lb_name="Test LB",
            project_no="PRJ001",
        )

        assert path.startswith("pdfs/")
        assert size == len(sample_pdf_bytes)

        # Download and verify
        downloaded = storage.download(path)
        assert downloaded == sample_pdf_bytes

    @pytest.mark.skipif(
        True,
        reason="Minio integration tests require running Minio"
    )
    def test_exists(self, storage, sample_pdf_bytes):
        """Test checking if an object exists."""
        path, _, _ = storage.upload_pdf(
            pdf_bytes=sample_pdf_bytes,
            year_label="2024-2025",
            lb_type_label="District Panchayat",
            district_name="Test District",
            lb_name="Test LB",
            project_no="PRJ002",
        )

        assert storage.exists(path)
        assert not storage.exists("nonexistent/path.pdf")

    @pytest.mark.skipif(
        True,
        reason="Minio integration tests require running Minio"
    )
    def test_delete(self, storage, sample_pdf_bytes):
        """Test deleting an object."""
        path, _, _ = storage.upload_pdf(
            pdf_bytes=sample_pdf_bytes,
            year_label="2024-2025",
            lb_type_label="District Panchayat",
            district_name="Test District",
            lb_name="Test LB",
            project_no="PRJ003",
        )

        assert storage.exists(path)
        result = storage.delete(path)
        assert result
        assert not storage.exists(path)


@pytest.mark.integration
class TestPdfRepository:
    """Tests for PDF database operations."""

    def test_create_pdf_record(self, db_session: Session, sample_project: Project):
        """Test creating a PDF record."""
        repo = PdfRepository(db_session)

        pdf = repo.create(
            project_id=sample_project.id,
            original_url="https://example.com/pdf",
            original_filename="project.pdf",
            redirect_url="https://example.com/redirect/hash",
        )

        assert pdf is not None
        assert pdf.project_id == sample_project.id
        assert pdf.status == GcsUploadStatus.PENDING

    def test_mark_uploaded(self, db_session: Session, sample_project: Project):
        """Test marking a PDF as uploaded."""
        repo = PdfRepository(db_session)

        pdf = repo.create(
            project_id=sample_project.id,
            original_url="https://example.com/pdf",
        )
        db_session.flush()

        repo.mark_uploaded(
            pdf_id=pdf.id,
            gcs_bucket="test-bucket",
            gcs_path="pdfs/test/path.pdf",
            file_size_bytes=1000,
            content_hash="abc123hash",
        )
        db_session.flush()

        updated = repo.get(pdf.id)
        assert updated.status == GcsUploadStatus.UPLOADED
        assert updated.gcs_bucket == "test-bucket"
        assert updated.gcs_path == "pdfs/test/path.pdf"
        assert updated.file_size_bytes == 1000
        assert updated.content_hash == "abc123hash"
        assert updated.uploaded_at is not None

    def test_get_by_hash_for_deduplication(self, db_session: Session, sample_project: Project):
        """Test finding a PDF by content hash for deduplication."""
        repo = PdfRepository(db_session)

        # Create a PDF with a known hash
        pdf = repo.create(project_id=sample_project.id)
        db_session.flush()

        repo.mark_uploaded(
            pdf_id=pdf.id,
            gcs_bucket="test-bucket",
            gcs_path="pdfs/test/path.pdf",
            file_size_bytes=1000,
            content_hash="unique_hash_123",
        )
        db_session.flush()

        # Should find by hash
        found = repo.get_by_hash("unique_hash_123")
        assert found is not None
        assert found.id == pdf.id

        # Should not find non-existent hash
        not_found = repo.get_by_hash("nonexistent_hash")
        assert not_found is None


@pytest.mark.integration
class TestPhase4Task:
    """Tests for the Phase 4 PDF download task."""

    def test_pdf_download_updates_project_status(
        self, db_session: Session, sample_project: Project
    ):
        """Test that successful PDF download updates project status."""
        project_repo = ProjectRepository(db_session)

        project_repo.mark_downloading(sample_project.id)
        db_session.flush()

        updated = project_repo.get(sample_project.id)
        assert updated.pdf_status == PdfStatus.DOWNLOADING
        assert updated.pdf_last_attempt_at is not None

        project_repo.mark_downloaded(sample_project.id)
        db_session.flush()

        updated = project_repo.get(sample_project.id)
        assert updated.pdf_status == PdfStatus.DOWNLOADED

    def test_pdf_missing_marks_project(
        self, db_session: Session, sample_project: Project
    ):
        """Test that missing PDF marks project correctly."""
        project_repo = ProjectRepository(db_session)

        project_repo.mark_missing(sample_project.id)
        db_session.flush()

        updated = project_repo.get(sample_project.id)
        assert updated.pdf_status == PdfStatus.MISSING

    def test_pdf_error_tracks_retries(
        self, db_session: Session, sample_project: Project
    ):
        """Test that PDF errors track retry count."""
        project_repo = ProjectRepository(db_session)

        # First error
        project_repo.mark_error(sample_project.id, "Connection timeout")
        db_session.flush()

        updated = project_repo.get(sample_project.id)
        assert updated.pdf_status == PdfStatus.ERROR
        assert updated.pdf_retry_count == 1

        # Second error
        project_repo.mark_error(sample_project.id, "Server error")
        db_session.flush()

        updated = project_repo.get(sample_project.id)
        assert updated.pdf_retry_count == 2
        assert updated.pdf_error_message == "Server error"

    def test_phase4_resumption_skips_downloaded(
        self, db_session: Session, sample_local_body
    ):
        """Test that Phase 4 resumes correctly, skipping downloaded PDFs."""
        project_repo = ProjectRepository(db_session)

        # Create projects with different PDF statuses
        statuses = [
            PdfStatus.DOWNLOADED,
            PdfStatus.PENDING,
            PdfStatus.ERROR,
            PdfStatus.MISSING,
        ]

        for i, status in enumerate(statuses):
            project = Project(
                local_body_id=sample_local_body.id,
                project_no=f"PRJ{i:03d}",
                project_name=f"Project {i}",
                select_argument=f"Select${i}",
                page_number=1,
                pdf_status=status,
            )
            db_session.add(project)

        db_session.flush()

        # Get pending should return PENDING and ERROR (not DOWNLOADED or MISSING)
        pending = project_repo.get_pending_pdfs()
        assert len(pending) == 2
        assert all(p.pdf_status in [PdfStatus.PENDING, PdfStatus.ERROR] for p in pending)

    def test_phase4_respects_max_retries(
        self, db_session: Session, sample_local_body
    ):
        """Test that Phase 4 doesn't retry beyond max_retries."""
        project_repo = ProjectRepository(db_session)

        # Create project that has hit max retries
        project = Project(
            local_body_id=sample_local_body.id,
            project_no="PRJ_MAX",
            project_name="Max Retry Project",
            select_argument="Select$0",
            page_number=1,
            pdf_status=PdfStatus.ERROR,
            pdf_retry_count=10,  # Exceeds typical max
        )
        db_session.add(project)
        db_session.flush()

        # Should not be returned (assuming max_retries=3 in settings)
        pending = project_repo.get_pending_pdfs()
        assert len(pending) == 0

    def test_phase4_idempotency_with_hash_dedup(
        self, db_session: Session, sample_project: Project
    ):
        """Test that Phase 4 handles duplicate PDFs via hash."""
        pdf_repo = PdfRepository(db_session)

        # Create first PDF
        pdf1 = pdf_repo.create(project_id=sample_project.id)
        db_session.flush()

        pdf_repo.mark_uploaded(
            pdf_id=pdf1.id,
            gcs_bucket="test-bucket",
            gcs_path="pdfs/test/path.pdf",
            file_size_bytes=1000,
            content_hash="same_hash",
        )
        db_session.flush()

        # Check for duplicate by hash
        existing = pdf_repo.get_by_hash("same_hash")
        assert existing is not None
        assert existing.id == pdf1.id

        # In real implementation, would skip upload for duplicate hash


@pytest.mark.integration
class TestEndToEndPhase4:
    """End-to-end tests for Phase 4 with mocked storage."""

    def test_full_pdf_workflow(
        self, db_session: Session, sample_project: Project, mock_storage, sample_pdf_bytes
    ):
        """Test the full PDF download and upload workflow."""
        project_repo = ProjectRepository(db_session)
        pdf_repo = PdfRepository(db_session)

        # Step 1: Mark downloading
        project_repo.mark_downloading(sample_project.id)
        db_session.flush()

        # Step 2: Create PDF record
        pdf = pdf_repo.create(
            project_id=sample_project.id,
            original_url="https://sulekha.example.com/pdf/123",
            redirect_url="https://sgwapi.example.com/abc123",
        )
        db_session.flush()

        # Step 3: Upload to storage (mocked)
        gcs_path, content_hash, file_size = mock_storage.upload_pdf.return_value

        # Step 4: Update records
        pdf_repo.mark_uploaded(
            pdf_id=pdf.id,
            gcs_bucket=mock_storage.bucket_name,
            gcs_path=gcs_path,
            file_size_bytes=file_size,
            content_hash=content_hash,
        )
        project_repo.mark_downloaded(sample_project.id)
        db_session.flush()

        # Verify final state
        updated_project = project_repo.get(sample_project.id)
        assert updated_project.pdf_status == PdfStatus.DOWNLOADED

        updated_pdf = pdf_repo.get(pdf.id)
        assert updated_pdf.status == GcsUploadStatus.UPLOADED
        assert updated_pdf.gcs_path == gcs_path
