"""Tests for Phase 3: Project Scraping.

Phase 3 iterates through all discovered local bodies and scrapes
all projects (rows in the gvProjects table with pagination).
"""

import pytest
from sqlalchemy.orm import Session

from sulekha.db.models import LocalBody, LocalBodyStatus, PdfStatus, Project
from sulekha.db.repositories import LocalBodyRepository, ProjectRepository
from sulekha.scraper.parsers import get_next_page_postback, parse_projects_and_pager


class TestProjectParsing:
    """Tests for parsing the gvProjects table."""

    def test_parse_projects_extracts_all_projects(self, sulekha_projects_html):
        """Test that parse_projects_and_pager extracts all projects from HTML."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sulekha_projects_html, "lxml")
        result = parse_projects_and_pager(soup)

        assert len(result.projects) == 3
        assert result.projects[0].project_no == "PRJ001"
        assert result.projects[0].project_name == "Road Construction"
        assert result.projects[0].formulation == "50000"
        assert result.projects[0].expense == "45000"
        assert result.projects[0].select_argument == "Select$0"

        assert result.projects[1].project_no == "PRJ002"
        assert result.projects[1].project_name == "School Building"

    def test_parse_projects_extracts_pager_info(self, sulekha_projects_html):
        """Test that parse_projects_and_pager extracts pagination info."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sulekha_projects_html, "lxml")
        result = parse_projects_and_pager(soup)

        assert result.pager.current_page == 1
        assert 2 in result.pager.pages
        assert result.pager.has_more

    def test_get_next_page_postback_returns_correct_page(self, sulekha_projects_html):
        """Test that get_next_page_postback returns the next page postback."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sulekha_projects_html, "lxml")
        result = parse_projects_and_pager(soup)

        next_pb = get_next_page_postback(result.pager)
        assert next_pb is not None
        assert next_pb[0] == "gvProjects"
        assert next_pb[1] == "Page$2"

    def test_get_next_page_postback_returns_none_on_last_page(self):
        """Test that get_next_page_postback returns None on last page."""
        from bs4 import BeautifulSoup

        html = """
        <html><body>
        <form id="form1">
            <table id="gvProjects">
                <tr><th>Project No</th><th>Project Name</th></tr>
                <tr>
                    <td>PRJ001</td>
                    <td>Project One</td>
                    <td><a href="javascript:__doPostBack('gvProjects','Select$0')">View</a></td>
                </tr>
                <tr>
                    <table>
                        <tr>
                            <td><a href="javascript:__doPostBack('gvProjects','Page$1')">1</a></td>
                            <td><span>2</span></td>
                        </tr>
                    </table>
                </tr>
            </table>
        </form>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        result = parse_projects_and_pager(soup)

        # Current page is 2, no next page available
        assert result.pager.current_page == 2
        next_pb = get_next_page_postback(result.pager)
        assert next_pb is None

    def test_parse_projects_handles_missing_table(self):
        """Test parsing when gvProjects table is missing."""
        from bs4 import BeautifulSoup

        html = "<html><body><form id='form1'>No table here</form></body></html>"
        soup = BeautifulSoup(html, "lxml")
        result = parse_projects_and_pager(soup)

        assert len(result.projects) == 0
        assert result.pager.current_page is None


@pytest.mark.integration
class TestProjectRepository:
    """Tests for project database operations."""

    def test_upsert_creates_new_project(self, db_session: Session, sample_local_body: LocalBody):
        """Test that upsert creates a new project record."""
        repo = ProjectRepository(db_session)

        project = repo.upsert(
            local_body_id=sample_local_body.id,
            project_no="PRJ001",
            project_name="Road Construction",
            select_argument="Select$0",
            page_number=1,
            formulation="50000",
            expense="45000",
        )

        assert project is not None
        assert project.project_no == "PRJ001"
        assert project.project_name == "Road Construction"
        assert project.pdf_status == PdfStatus.PENDING

    def test_upsert_updates_existing_project(self, db_session: Session, sample_local_body: LocalBody):
        """Test that upsert updates an existing project record."""
        repo = ProjectRepository(db_session)

        # Create first
        p1 = repo.upsert(
            local_body_id=sample_local_body.id,
            project_no="PRJ001",
            project_name="Original Name",
            select_argument="Select$0",
            page_number=1,
        )
        db_session.flush()

        # Update
        p2 = repo.upsert(
            local_body_id=sample_local_body.id,
            project_no="PRJ001",
            project_name="Updated Name",
            select_argument="Select$0",
            page_number=1,
            formulation="100000",
        )

        assert p2.id == p1.id
        assert p2.project_name == "Updated Name"
        assert p2.formulation == "100000"

    def test_get_pending_pdfs_returns_correct_projects(
        self, db_session: Session, sample_local_body: LocalBody
    ):
        """Test that get_pending_pdfs returns projects needing PDF download."""
        repo = ProjectRepository(db_session)

        # Create projects with different PDF statuses
        statuses = [
            PdfStatus.PENDING,
            PdfStatus.DOWNLOADED,
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

        pending = repo.get_pending_pdfs()
        # Should return PENDING and ERROR (not DOWNLOADED or MISSING)
        assert len(pending) == 2
        assert all(p.pdf_status in [PdfStatus.PENDING, PdfStatus.ERROR] for p in pending)

    def test_mark_downloaded_updates_status(self, db_session: Session, sample_project: Project):
        """Test that mark_downloaded updates the project status."""
        repo = ProjectRepository(db_session)

        repo.mark_downloaded(sample_project.id)
        db_session.flush()

        updated = repo.get(sample_project.id)
        assert updated.pdf_status == PdfStatus.DOWNLOADED

    def test_mark_error_increments_retry_count(self, db_session: Session, sample_project: Project):
        """Test that mark_error increments retry count."""
        repo = ProjectRepository(db_session)

        assert sample_project.pdf_retry_count == 0

        repo.mark_error(sample_project.id, "Download failed")
        db_session.flush()

        updated = repo.get(sample_project.id)
        assert updated.pdf_status == PdfStatus.ERROR
        assert updated.pdf_retry_count == 1
        assert updated.pdf_error_message == "Download failed"


@pytest.mark.integration
class TestPhase3Task:
    """Tests for the Phase 3 scraping task."""

    def test_scrape_projects_stores_in_db(
        self, db_session: Session, sample_local_body: LocalBody, sulekha_projects_html
    ):
        """Test that scraping projects stores them in database."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sulekha_projects_html, "lxml")
        result = parse_projects_and_pager(soup)

        project_repo = ProjectRepository(db_session)
        for i, p in enumerate(result.projects):
            project_repo.upsert(
                local_body_id=sample_local_body.id,
                project_no=p.project_no,
                project_name=p.project_name,
                formulation=p.formulation,
                expense=p.expense,
                page_number=1,
                select_argument=p.select_argument,
            )

        db_session.flush()

        # Verify projects were stored
        all_projects = project_repo.get_all_for_local_body(sample_local_body.id)
        assert len(all_projects) == 3
        assert all_projects[0].project_no == "PRJ001"

    def test_phase3_tracks_pagination(self, db_session: Session, sample_local_body: LocalBody):
        """Test that Phase 3 tracks pagination progress."""
        lb_repo = LocalBodyRepository(db_session)

        # Simulate scraping pages
        lb_repo.update_progress(
            sample_local_body.id,
            last_page_scraped=1,
            scraped_projects=20,
            total_pages=5,
        )
        db_session.flush()

        updated = lb_repo.get(sample_local_body.id)
        assert updated.last_page_scraped == 1
        assert updated.status == LocalBodyStatus.PARTIAL

        # Simulate completing
        lb_repo.mark_done(sample_local_body.id, scraped_projects=100)
        db_session.flush()

        updated = lb_repo.get(sample_local_body.id)
        assert updated.status == LocalBodyStatus.DONE
        assert updated.scraped_projects == 100

    def test_phase3_resumption_from_last_page(self, db_session: Session, sample_local_body: LocalBody):
        """Test that Phase 3 resumes from last_page_scraped."""
        lb_repo = LocalBodyRepository(db_session)
        project_repo = ProjectRepository(db_session)

        # Simulate partial scrape
        lb_repo.update_progress(
            sample_local_body.id,
            last_page_scraped=3,
            scraped_projects=60,
            total_pages=10,
        )

        # Add projects from first 3 pages
        for i in range(60):
            project = Project(
                local_body_id=sample_local_body.id,
                project_no=f"PRJ{i:03d}",
                project_name=f"Project {i}",
                select_argument=f"Select${i}",
                page_number=(i // 20) + 1,
                pdf_status=PdfStatus.PENDING,
            )
            db_session.add(project)

        db_session.flush()

        # On resumption, should start from page 4
        updated = lb_repo.get(sample_local_body.id)
        assert updated.last_page_scraped == 3
        assert updated.status == LocalBodyStatus.PARTIAL

        # Existing projects should be preserved
        all_projects = project_repo.get_all_for_local_body(sample_local_body.id)
        assert len(all_projects) == 60

    def test_phase3_idempotency(self, db_session: Session, sample_local_body: LocalBody):
        """Test that running Phase 3 twice doesn't create duplicate projects."""
        project_repo = ProjectRepository(db_session)

        # Run twice with same data
        for _ in range(2):
            project_repo.upsert(
                local_body_id=sample_local_body.id,
                project_no="PRJ001",
                project_name="Test Project",
                select_argument="Select$0",
                page_number=1,
            )
            db_session.flush()

        # Should only have one project
        all_projects = project_repo.get_all_for_local_body(sample_local_body.id)
        assert len(all_projects) == 1

    def test_phase3_handles_multiple_pages(
        self, db_session: Session, sample_local_body: LocalBody
    ):
        """Test scraping multiple pages of projects."""
        project_repo = ProjectRepository(db_session)

        # Simulate scraping 3 pages of 20 projects each
        total_projects = 0
        for page in range(1, 4):
            for i in range(20):
                project_repo.upsert(
                    local_body_id=sample_local_body.id,
                    project_no=f"PRJ{total_projects:03d}",
                    project_name=f"Project {total_projects}",
                    select_argument=f"Select${i}",
                    page_number=page,
                )
                total_projects += 1

        db_session.flush()

        all_projects = project_repo.get_all_for_local_body(sample_local_body.id)
        assert len(all_projects) == 60

        # Verify page distribution
        page_1_projects = [p for p in all_projects if p.page_number == 1]
        page_2_projects = [p for p in all_projects if p.page_number == 2]
        page_3_projects = [p for p in all_projects if p.page_number == 3]

        assert len(page_1_projects) == 20
        assert len(page_2_projects) == 20
        assert len(page_3_projects) == 20
