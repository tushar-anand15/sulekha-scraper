"""Tests for Phase 2: Local Body Discovery.

Phase 2 iterates through all discovered districts and discovers
all local bodies (rows in the gvStat table).
"""

import pytest
import responses
from sqlalchemy.orm import Session

from sulekha.db.models import District, DistrictStatus, LocalBody, LocalBodyStatus
from sulekha.db.repositories import DistrictRepository, LocalBodyRepository
from sulekha.scraper.parsers import parse_local_body_rows


class TestLocalBodyParsing:
    """Tests for parsing the gvStat table."""

    def test_parse_local_body_rows_extracts_all_lbs(self, sulekha_local_bodies_html):
        """Test that parse_local_body_rows extracts all local bodies from HTML."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sulekha_local_bodies_html, "lxml")
        local_bodies = parse_local_body_rows(soup)

        assert len(local_bodies) == 1
        assert local_bodies[0].lb_name == "Thiruvananthapuram District Panchayat"
        assert local_bodies[0].index == 1
        assert local_bodies[0].num_projects == 1166
        assert local_bodies[0].postback_argument == "Select$0"

    def test_parse_local_body_rows_handles_multiple_lbs(self):
        """Test parsing multiple local bodies."""
        from bs4 import BeautifulSoup

        html = """
        <html><body>
        <form id="form1">
            <table id="gvStat">
                <tr><th>Sl No</th><th>Local Body</th><th>Projects</th><th>Details</th></tr>
                <tr>
                    <td>1</td>
                    <td>LB One</td>
                    <td>100</td>
                    <td><a href="javascript:__doPostBack('gvStat','Select$0')">Details</a></td>
                </tr>
                <tr>
                    <td>2</td>
                    <td>LB Two</td>
                    <td>200</td>
                    <td><a href="javascript:__doPostBack('gvStat','Select$1')">Details</a></td>
                </tr>
                <tr>
                    <td>3</td>
                    <td>LB Three</td>
                    <td>300</td>
                    <td><a href="javascript:__doPostBack('gvStat','Select$2')">Details</a></td>
                </tr>
                <tr><td colspan="4">Footer</td></tr>
            </table>
        </form>
        </body></html>
        """
        soup = BeautifulSoup(html, "lxml")
        local_bodies = parse_local_body_rows(soup)

        assert len(local_bodies) == 3
        assert local_bodies[0].lb_name == "LB One"
        assert local_bodies[1].lb_name == "LB Two"
        assert local_bodies[2].lb_name == "LB Three"
        assert local_bodies[2].num_projects == 300

    def test_parse_local_body_rows_handles_missing_table(self):
        """Test parsing when gvStat table is missing."""
        from bs4 import BeautifulSoup

        html = "<html><body><form id='form1'>No table here</form></body></html>"
        soup = BeautifulSoup(html, "lxml")
        local_bodies = parse_local_body_rows(soup)

        assert len(local_bodies) == 0


@pytest.mark.integration
class TestLocalBodyRepository:
    """Tests for local body database operations."""

    def test_upsert_creates_new_local_body(self, db_session: Session, sample_district: District):
        """Test that upsert creates a new local body record."""
        repo = LocalBodyRepository(db_session)

        lb = repo.upsert(
            district_id=sample_district.id,
            lb_index=1,
            lb_name="Thiruvananthapuram District Panchayat",
            postback_argument="Select$0",
            expected_projects=1166,
        )

        assert lb is not None
        assert lb.lb_name == "Thiruvananthapuram District Panchayat"
        assert lb.status == LocalBodyStatus.PENDING
        assert lb.district_id == sample_district.id

    def test_upsert_updates_existing_local_body(self, db_session: Session, sample_district: District):
        """Test that upsert updates an existing local body record."""
        repo = LocalBodyRepository(db_session)

        # Create first
        lb1 = repo.upsert(
            district_id=sample_district.id,
            lb_index=1,
            lb_name="Original Name",
            postback_argument="Select$0",
            expected_projects=1000,
        )
        db_session.flush()

        # Update
        lb2 = repo.upsert(
            district_id=sample_district.id,
            lb_index=1,
            lb_name="Updated Name",
            postback_argument="Select$0",
            expected_projects=2000,
        )

        assert lb2.id == lb1.id
        assert lb2.lb_name == "Updated Name"
        assert lb2.expected_projects == 2000

    def test_get_pending_returns_only_pending(self, db_session: Session, sample_district: District):
        """Test that get_pending returns pending and partial local bodies."""
        repo = LocalBodyRepository(db_session)

        # Create LBs with different statuses
        statuses = [
            LocalBodyStatus.PENDING,
            LocalBodyStatus.DONE,
            LocalBodyStatus.PARTIAL,
            LocalBodyStatus.ERROR,
        ]

        for i, status in enumerate(statuses):
            lb = LocalBody(
                district_id=sample_district.id,
                lb_index=i + 1,
                lb_name=f"LB {i + 1}",
                postback_argument=f"Select${i}",
                status=status,
            )
            db_session.add(lb)

        db_session.flush()

        pending = repo.get_pending()
        # Should return PENDING, PARTIAL, and ERROR (not DONE)
        assert len(pending) == 3
        assert all(lb.status != LocalBodyStatus.DONE for lb in pending)

    def test_update_progress_tracks_pagination(self, db_session: Session, sample_local_body: LocalBody):
        """Test that update_progress tracks pagination correctly."""
        repo = LocalBodyRepository(db_session)

        assert sample_local_body.last_page_scraped == 0
        assert sample_local_body.status == LocalBodyStatus.PENDING

        # Simulate scraping page 1
        repo.update_progress(
            sample_local_body.id,
            last_page_scraped=1,
            scraped_projects=20,
            total_pages=5,
        )
        db_session.flush()

        updated = repo.get(sample_local_body.id)
        assert updated.last_page_scraped == 1
        assert updated.scraped_projects == 20
        assert updated.total_pages == 5
        assert updated.status == LocalBodyStatus.PARTIAL

    def test_mark_done_updates_status(self, db_session: Session, sample_local_body: LocalBody):
        """Test that mark_done updates the local body status."""
        repo = LocalBodyRepository(db_session)

        repo.mark_done(sample_local_body.id, scraped_projects=100)
        db_session.flush()

        updated = repo.get(sample_local_body.id)
        assert updated.status == LocalBodyStatus.DONE
        assert updated.scraped_projects == 100
        assert updated.last_scraped_at is not None


@pytest.mark.integration
class TestPhase2Task:
    """Tests for the Phase 2 discovery task."""

    def test_discover_local_bodies_stores_in_db(
        self, db_session: Session, sample_district: District, sulekha_local_bodies_html
    ):
        """Test that discovering local bodies stores them in database."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(sulekha_local_bodies_html, "lxml")
        local_bodies = parse_local_body_rows(soup)

        lb_repo = LocalBodyRepository(db_session)
        for lb in local_bodies:
            lb_repo.upsert(
                district_id=sample_district.id,
                lb_index=lb.index,
                lb_name=lb.lb_name,
                postback_argument=lb.postback_argument,
                expected_projects=lb.num_projects,
            )

        db_session.flush()

        # Verify local bodies were stored
        all_lbs = lb_repo.get_all_for_district(sample_district.id)
        assert len(all_lbs) == 1
        assert all_lbs[0].lb_name == "Thiruvananthapuram District Panchayat"

    def test_phase2_marks_district_done(self, db_session: Session, sample_district: District):
        """Test that Phase 2 marks district as DONE after discovering LBs."""
        district_repo = DistrictRepository(db_session)
        lb_repo = LocalBodyRepository(db_session)

        # Simulate discovering LBs
        lb_repo.upsert(
            district_id=sample_district.id,
            lb_index=1,
            lb_name="Test LB",
            postback_argument="Select$0",
        )

        # Mark district as done
        district_repo.mark_done(sample_district.id)
        db_session.flush()

        updated = district_repo.get(sample_district.id)
        assert updated.status == DistrictStatus.DONE

    def test_phase2_resumption_skips_done_districts(self, db_session: Session):
        """Test that Phase 2 resumes correctly, skipping done districts."""
        district_repo = DistrictRepository(db_session)

        # Create districts with different statuses
        for i, status in enumerate([DistrictStatus.DONE, DistrictStatus.PENDING, DistrictStatus.ERROR]):
            district = District(
                year_val=28,
                year_label="2024-2025",
                lb_type_val=1,
                lb_type_label="District Panchayat",
                district_index=i + 1,
                district_name=f"District {i + 1}",
                postback_argument=f"Select${i}",
                status=status,
            )
            db_session.add(district)

        db_session.flush()

        # Get pending should return PENDING and ERROR, not DONE
        pending = district_repo.get_pending()
        assert len(pending) == 2
        assert all(d.status != DistrictStatus.DONE for d in pending)

    def test_phase2_idempotency(self, db_session: Session, sample_district: District):
        """Test that running Phase 2 twice doesn't create duplicate LBs."""
        lb_repo = LocalBodyRepository(db_session)

        # Run twice with same data
        for _ in range(2):
            lb_repo.upsert(
                district_id=sample_district.id,
                lb_index=1,
                lb_name="Test LB",
                postback_argument="Select$0",
            )
            db_session.flush()

        # Should only have one LB
        all_lbs = lb_repo.get_all_for_district(sample_district.id)
        assert len(all_lbs) == 1
