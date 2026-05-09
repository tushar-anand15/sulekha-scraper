"""Unit and integration tests for :mod:`sakarma.tasks.artifacts`.

Test hierarchy
--------------
Unit tests (no DB, mocked client + storage + repos via MagicMock):
  - happy_path:          1 manifest row, no existing artifacts →
                         1 Minutes upload + 1 DR upload + N attachment uploads.
  - idempotent:          same run twice → 0 new uploads on the second call
                         (exists() returns True for both Minutes and DR).
  - zero_attachments:    meeting with zero attachments → 2 artifacts, no error.
  - session_expired:     SessionExpiredError on first row → recovery succeeds,
                         second row processes correctly.
  - gcs_upload_error:    storage.upload raises on Minutes → row marked skipped,
                         continues with next row.
  - empty_target:        parse_attachment_links returns tuple with empty target
                         → attachment skipped defensively.
  - empty_filename:      click_attachment_lnkfileview returns ("", "") →
                         uploaded with fallback attachment_<sha8>.pdf path.

Integration tests (mark @pytest.mark.integration):
  - integration_full_run: real DB session + mock client → 3 artifacts persisted
                           (Minutes + DR + 1 attachment) for one Approved row.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from sakarma.scraper.client import SessionExpiredError
from sakarma.scraper.protocol import FormState
from sakarma.storage.gcs import (
    ARTIFACT_ATTACHMENT_PDF,
    ARTIFACT_DR_HTML,
    ARTIFACT_MINUTES_HTML,
)
from sakarma.tasks.artifacts import ArtifactsRepos, run_for_lb

# ---------------------------------------------------------------------------
# Shared byte fixtures
# ---------------------------------------------------------------------------

MINUTES_BYTES = b"<html>minutes</html>"
DR_BYTES_NO_ATTACHMENTS = b"<html>dr no attachments</html>"
DR_BYTES_ONE_ATTACHMENT = (
    b'<html><body>'
    b'<a id="GrdDecision_ctl02_lnkFileView" '
    b'href="javascript:__doPostBack(\'GrdDecision$ctl02$lnkFileView\',\'\')">View</a>'
    b"</body></html>"
)
PDF_BYTES = b"%PDF-1.4 fake pdf content"
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()


def _form_state() -> FormState:
    return FormState(
        viewstate="VS",
        event_validation="EV",
        page_url="http://meeting.lsgkerala.gov.in/Pages/LBWiseDashBoard.aspx",
        raw_html=b"<html><body><form>"
        b'<input name="__VIEWSTATE" value="VS"/>'
        b'<input name="__EVENTVALIDATION" value="EV"/>'
        b"</form></body></html>",
    )


def _make_manifest_row(
    row_id: int = 1,
    year_id: int = 10,
    mg_id: int = 20,
    grid_idx: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id,
        year_id=year_id,
        main_group_value_id=mg_id,
        dashboard_grid_select_index=grid_idx,
        meeting_no_label="1/2025",
    )


def _make_repos(
    manifest_rows: list,
    *,
    minutes_exists: bool = False,
    dr_exists: bool = False,
) -> ArtifactsRepos:
    """Build a fully-mocked ArtifactsRepos."""
    lb = SimpleNamespace(district_id=1, lb_type_id=2, name_ml="TestLB")
    lb_repo = MagicMock()
    lb_repo.get.return_value = lb

    year_repo = MagicMock()
    year_repo.get.return_value = SimpleNamespace(year_int=2025)

    mg_repo = MagicMock()
    mg_repo.get.return_value = SimpleNamespace(ddl_value=5)

    manifest_repo = MagicMock()
    manifest_repo.list_approved_for_lb_run.return_value = manifest_rows

    artifact_repo = MagicMock()
    artifact_repo.exists.side_effect = lambda manifest_id, atype: (
        minutes_exists if atype == ARTIFACT_MINUTES_HTML
        else (dr_exists if atype == ARTIFACT_DR_HTML else False)
    )
    # get_or_create returns a dummy artifact row
    artifact_repo.get_or_create.return_value = SimpleNamespace(id=99)

    lb_progress_repo = MagicMock()

    return ArtifactsRepos(
        lb_repo=lb_repo,
        year_repo=year_repo,
        main_group_value_repo=mg_repo,
        meeting_manifest_repo=manifest_repo,
        meeting_artifact_repo=artifact_repo,
        lb_progress_repo=lb_progress_repo,
    )


def _make_client(
    *,
    minutes_bytes: bytes = MINUTES_BYTES,
    dr_bytes: bytes = DR_BYTES_NO_ATTACHMENTS,
    attachment_result: tuple[bytes, str] | None = None,
) -> MagicMock:
    """Build a mocked SakarmaClient."""
    client = MagicMock()
    state = _form_state()
    client.load_page.return_value = state
    client.select_dropdown.return_value = state
    client.click_button.return_value = state
    client.select_grid_row.return_value = state
    client.click_dr.return_value = state
    client.fetch_public_page.side_effect = [minutes_bytes, dr_bytes]
    if attachment_result is not None:
        client.click_attachment_lnkfileview.return_value = attachment_result
    return client


def _make_storage() -> MagicMock:
    storage = MagicMock()
    storage.bucket_name = "test-bucket"
    storage.upload.return_value = None
    return storage


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    """1 manifest row, no existing artifacts → 1 Minutes + 1 DR + 1 attachment."""

    def test_returns_correct_counts(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        client = _make_client(
            dr_bytes=DR_BYTES_ONE_ATTACHMENT,
            attachment_result=(PDF_BYTES, "decisions.pdf"),
        )
        storage = _make_storage()

        summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        assert summary["minutes_uploaded"] == 1
        assert summary["dr_uploaded"] == 1
        assert summary["attachments_uploaded"] == 1
        assert summary["rows_processed"] == 1
        assert summary["rows_skipped"] == 0

    def test_artifact_repo_called_for_each_type(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        client = _make_client(
            dr_bytes=DR_BYTES_ONE_ATTACHMENT,
            attachment_result=(PDF_BYTES, "file.pdf"),
        )
        storage = _make_storage()

        run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        get_or_create_calls = repos.meeting_artifact_repo.get_or_create.call_args_list
        types_saved = {c.kwargs["artifact_type"] for c in get_or_create_calls}
        assert types_saved == {
            ARTIFACT_MINUTES_HTML,
            ARTIFACT_DR_HTML,
            ARTIFACT_ATTACHMENT_PDF,
        }

    def test_storage_upload_called_three_times(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        client = _make_client(
            dr_bytes=DR_BYTES_ONE_ATTACHMENT,
            attachment_result=(PDF_BYTES, "file.pdf"),
        )
        storage = _make_storage()

        run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        assert storage.upload.call_count == 3


class TestIdempotency:
    """Second run with artifacts already existing → 0 new uploads."""

    def test_skips_row_when_both_exist(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=True, dr_exists=True)
        client = _make_client()
        storage = _make_storage()

        summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        assert summary["minutes_uploaded"] == 0
        assert summary["dr_uploaded"] == 0
        assert summary["attachments_uploaded"] == 0
        assert summary["rows_processed"] == 0
        assert summary["rows_skipped"] == 1
        storage.upload.assert_not_called()


class TestZeroAttachments:
    """Meeting with zero attachments → 2 artifacts (Minutes + DR), no error."""

    def test_two_artifacts_no_attachment_calls(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        client = _make_client(dr_bytes=DR_BYTES_NO_ATTACHMENTS)
        storage = _make_storage()

        summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        assert summary["minutes_uploaded"] == 1
        assert summary["dr_uploaded"] == 1
        assert summary["attachments_uploaded"] == 0
        assert summary["rows_processed"] == 1
        client.click_attachment_lnkfileview.assert_not_called()


class TestSessionExpiredRecovery:
    """SessionExpiredError on first row → recovery succeeds, processes correctly."""

    def test_recovers_and_processes_after_expiry(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        storage = _make_storage()

        state = _form_state()

        # First call to fetch_public_page raises SessionExpiredError; subsequent
        # calls work (minutes bytes, then DR bytes).
        call_count = {"n": 0}

        def _select_grid_row_side_effect(inner_state, row_index):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise SessionExpiredError("session gone")
            return state

        client = MagicMock()
        client.load_page.return_value = state
        client.select_dropdown.return_value = state
        client.click_button.return_value = state
        client.select_grid_row.side_effect = _select_grid_row_side_effect
        client.click_dr.return_value = state
        client.fetch_public_page.side_effect = [MINUTES_BYTES, DR_BYTES_NO_ATTACHMENTS]

        summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        assert summary["minutes_uploaded"] == 1
        assert summary["dr_uploaded"] == 1
        assert summary["rows_processed"] == 1

    def test_recovery_limit_exceeded_raises(self) -> None:
        """After 3 recoveries, SessionExpiredError propagates."""
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        storage = _make_storage()
        state = _form_state()

        # load_page always works; select_grid_row always raises to exhaust retries
        client = MagicMock()
        client.load_page.return_value = state
        client.select_dropdown.return_value = state
        client.click_button.return_value = state
        client.select_grid_row.side_effect = SessionExpiredError("always gone")

        with pytest.raises(SessionExpiredError):
            run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)


class TestGCSUploadError:
    """GCS upload error on Minutes → row skipped, continues with next row."""

    def test_upload_error_skips_row(self) -> None:
        row1 = _make_manifest_row(row_id=1, grid_idx=0)
        row2 = _make_manifest_row(row_id=2, grid_idx=1)
        repos = _make_repos([row1, row2], minutes_exists=False, dr_exists=False)
        state = _form_state()

        client = MagicMock()
        client.load_page.return_value = state
        client.select_dropdown.return_value = state
        client.click_button.return_value = state
        client.select_grid_row.return_value = state
        client.click_dr.return_value = state
        # row1 Minutes + row1 DR + row2 Minutes + row2 DR
        client.fetch_public_page.side_effect = [
            MINUTES_BYTES,
            DR_BYTES_NO_ATTACHMENTS,
            MINUTES_BYTES,
            DR_BYTES_NO_ATTACHMENTS,
        ]

        upload_call_count = {"n": 0}

        def _upload_side_effect(path, data, content_type=None):
            upload_call_count["n"] += 1
            if upload_call_count["n"] == 1:
                raise OSError("GCS failure")

        storage = MagicMock()
        storage.bucket_name = "test-bucket"
        storage.upload.side_effect = _upload_side_effect

        summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        # row1 is skipped due to upload error; row2 succeeds.
        assert summary["rows_skipped"] == 1
        assert summary["rows_processed"] == 1
        assert summary["minutes_uploaded"] == 1


class TestEmptyAttachmentTarget:
    """parse_attachment_links returns tuple with empty target → skipped defensively."""

    def test_empty_target_skipped(self) -> None:
        # Build DR HTML that triggers parsing but returns a row with empty target.
        # We patch parse_attachment_links directly.
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        client = _make_client(dr_bytes=DR_BYTES_NO_ATTACHMENTS)
        storage = _make_storage()

        with patch(
            "sakarma.tasks.artifacts.parse_attachment_links",
            return_value=[(2, "")],
        ):
            summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        assert summary["attachments_uploaded"] == 0
        client.click_attachment_lnkfileview.assert_not_called()


class TestEmptyOriginalFilename:
    """click_attachment_lnkfileview returns empty filename → fallback path used."""

    def test_fallback_filename_in_path(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        client = _make_client(
            dr_bytes=DR_BYTES_ONE_ATTACHMENT,
            # empty filename
            attachment_result=(PDF_BYTES, ""),
        )
        storage = _make_storage()

        run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        # Verify that the GCS path passed to upload contains the fallback name.
        upload_calls = storage.upload.call_args_list
        # 3 uploads: minutes, dr, attachment
        assert len(upload_calls) == 3
        att_path = upload_calls[2].args[0]
        sha8 = PDF_SHA256[:8]
        assert f"attachment_{sha8}.pdf" in att_path

    def test_attachment_count_still_one(self) -> None:
        row = _make_manifest_row()
        repos = _make_repos([row], minutes_exists=False, dr_exists=False)
        client = _make_client(
            dr_bytes=DR_BYTES_ONE_ATTACHMENT,
            attachment_result=(PDF_BYTES, ""),
        )
        storage = _make_storage()

        summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)
        assert summary["attachments_uploaded"] == 1


class TestNoManifestRows:
    """Empty manifest → returns zeroes immediately, no network calls."""

    def test_empty_manifest(self) -> None:
        repos = _make_repos([], minutes_exists=False, dr_exists=False)
        client = _make_client()
        storage = _make_storage()

        summary = run_for_lb(client, storage, repos, lb_id=42, scrape_run_id=7)

        assert summary == {
            "minutes_uploaded": 0,
            "dr_uploaded": 0,
            "attachments_uploaded": 0,
            "rows_processed": 0,
            "rows_skipped": 0,
        }
        client.load_page.assert_not_called()


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestIntegrationFullRun:
    """Real DB session + mock client → 3 artifact rows persisted."""

    def test_three_artifacts_persisted(self, db_session) -> None:
        """Persist Minutes + DR + 1 attachment for one Approved manifest row."""
        from datetime import date

        from sakarma.db.models import (
            CATEGORY_APPROVED,
            LB,
            LBType,
            District,
            MeetingArtifact,
            MeetingManifest,
            MainGroupValue,
            ScrapeRun,
            Year,
        )
        from sakarma.db.repositories import (
            LBProgressRepository,
            LBRepository,
            MainGroupValueRepository,
            MeetingArtifactRepository,
            MeetingManifestRepository,
            YearRepository,
        )

        # ---- Seed dimension rows ----
        district = District(id=1, name_ml="TestDistrict")
        lb_type = LBType(id=1, name_ml="TestType")
        year = Year(id=10, year_int=2025)
        db_session.add_all([district, lb_type, year])
        db_session.flush()

        lb = LB(id=42, district_id=1, lb_type_id=1, name_ml="TestLB")
        db_session.add(lb)
        db_session.flush()

        mg = MainGroupValue(lb_id=42, ddl_value=5, name_ml="General Body")
        db_session.add(mg)
        db_session.flush()

        scrape_run = ScrapeRun(kind="backfill", status="running")
        db_session.add(scrape_run)
        db_session.flush()

        manifest_row = MeetingManifest(
            lb_id=42,
            year_id=10,
            main_group_value_id=mg.id,
            category=CATEGORY_APPROVED,
            dashboard_grid_select_index=0,
            dr_postback_target="ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl02$lnkDR",
            meeting_no_label="1/2025",
            meeting_date=date(2025, 1, 15),
            scrape_run_id=scrape_run.id,
        )
        db_session.add(manifest_row)
        db_session.flush()

        # ---- Build real repos ----
        repos = ArtifactsRepos(
            lb_repo=LBRepository(db_session),
            year_repo=YearRepository(db_session),
            main_group_value_repo=MainGroupValueRepository(db_session),
            meeting_manifest_repo=MeetingManifestRepository(db_session),
            meeting_artifact_repo=MeetingArtifactRepository(db_session),
            lb_progress_repo=LBProgressRepository(db_session),
        )

        # ---- Build mock client ----
        state = _form_state()
        client = MagicMock()
        client.load_page.return_value = state
        client.select_dropdown.return_value = state
        client.click_button.return_value = state
        client.select_grid_row.return_value = state
        client.click_dr.return_value = state
        client.fetch_public_page.side_effect = [
            MINUTES_BYTES,
            DR_BYTES_ONE_ATTACHMENT,
        ]
        client.click_attachment_lnkfileview.return_value = (PDF_BYTES, "decisions.pdf")

        # ---- Build mock storage ----
        storage = _make_storage()

        # ---- Run ----
        summary = run_for_lb(
            client, storage, repos, lb_id=42, scrape_run_id=scrape_run.id
        )

        assert summary["minutes_uploaded"] == 1
        assert summary["dr_uploaded"] == 1
        assert summary["attachments_uploaded"] == 1

        # ---- Verify DB rows ----
        artifacts = (
            db_session.query(MeetingArtifact)
            .filter(MeetingArtifact.meeting_manifest_id == manifest_row.id)
            .all()
        )
        assert len(artifacts) == 3

        types_saved = {a.artifact_type for a in artifacts}
        assert types_saved == {
            ARTIFACT_MINUTES_HTML,
            ARTIFACT_DR_HTML,
            ARTIFACT_ATTACHMENT_PDF,
        }

        pdf_art = next(
            a for a in artifacts if a.artifact_type == ARTIFACT_ATTACHMENT_PDF
        )
        assert pdf_art.decision_index == 2  # ctl02 → index 2
        assert pdf_art.original_filename == "decisions.pdf"
