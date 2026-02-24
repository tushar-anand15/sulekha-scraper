"""PDF scraper tasks for Phase 4 of the scraping pipeline.

Phase 4: Download PDFs for each project and upload to GCS
"""

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

from sulekha.db.models import GcsUploadStatus
from sulekha.db.repositories import (
    DistrictRepository,
    LocalBodyRepository,
    PdfRepository,
    ProjectRepository,
)
from sulekha.db.session import get_session
from sulekha.scraper.client import SulekhaClient
from sulekha.storage.gcs import GCSStorage
from sulekha.tasks.celery_app import celery_app

logger = structlog.get_logger(__name__)

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"


def _find_pdf_url_in_html(soup: BeautifulSoup) -> Optional[str]:
    """Search for PDF URLs in HTML response.
    
    Looks for:
    - Direct links to .pdf files
    - iframe/embed/object sources pointing to PDFs
    
    Args:
        soup: BeautifulSoup parsed HTML
        
    Returns:
        PDF URL (may be relative) or None if not found
    """
    # Direct links
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href.lower().endswith(".pdf") or ".pdf?" in href.lower():
            return urljoin(BASE_URL, href)

    # iframe/object/embed src
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = (tag.get("src") or tag.get("data") or "").strip()
        if src and (src.lower().endswith(".pdf") or ".pdf?" in src.lower() or "pdf" in src.lower()):
            return urljoin(BASE_URL, src)

    return None


def _download_single_pdf(
    client: SulekhaClient,
    project,
    district,
    lb,
    pdf_repo: PdfRepository,
    project_repo: ProjectRepository,
    session,
    current_page: int,
) -> tuple[bool, int, Optional[str]]:
    """Download a single PDF within an existing navigation context.
    
    Args:
        client: SulekhaClient already navigated to the local body
        project: Project to download PDF for
        district: District entity
        lb: LocalBody entity
        pdf_repo: PDF repository
        project_repo: Project repository
        session: Database session
        current_page: Current page number in the projects table
        
    Returns:
        Tuple of (success, new_page_number, error_message)
    """
    try:
        # Navigate to the correct page if needed
        if project.page_number and project.page_number != current_page:
            result = client.postback(
                "gvProjects",
                f"Page${project.page_number}",
            )
            if not result.success:
                return False, current_page, f"Failed to navigate to page {project.page_number}: {result.error}"
            current_page = project.page_number

        # Click on project to get PDF
        result = client.postback(
            "gvProjects",
            project.select_argument,
            stream=True,
        )

        if not result.success:
            return False, current_page, f"Failed to click project: {result.error}"

        pdf_bytes = None
        original_filename = None

        # Check if response is directly a PDF
        if result.response:
            content_type = result.response.headers.get("Content-Type", "").lower()
            content_disp = result.response.headers.get("Content-Disposition", "").lower()
            
            if "application/pdf" in content_type or ".pdf" in content_disp:
                pdf_bytes = result.response.content
                if "filename=" in content_disp:
                    match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', content_disp)
                    if match:
                        original_filename = match.group(1).strip()

        # If not PDF, look for PDF URL in HTML response
        if pdf_bytes is None and result.soup:
            pdf_url = _find_pdf_url_in_html(result.soup)
            if pdf_url:
                pdf_bytes, original_filename, download_error = client.download_pdf(pdf_url)
                if download_error:
                    return False, current_page, f"Failed to download PDF from URL: {download_error}"

        # No PDF found - mark as missing
        if pdf_bytes is None:
            project_repo.mark_missing(project.id)
            session.commit()
            return True, current_page, None  # Success but no PDF

        # Skip if PDF already exists (race: another task processed this project)
        existing_pdf = pdf_repo.get_by_project(project.id)
        if existing_pdf and existing_pdf.status == GcsUploadStatus.UPLOADED:
            project_repo.mark_downloaded(project.id)
            session.commit()
            return True, current_page, None

        # Create PDF record
        pdf_record = pdf_repo.create(
            project_id=project.id,
            original_url=BASE_URL,
            original_filename=original_filename,
            redirect_url=BASE_URL,
        )
        pdf_record.downloaded_at = datetime.utcnow()
        session.commit()

        # Upload to GCS
        gcs = GCSStorage()
        gcs_path, content_hash, file_size = gcs.upload_pdf(
            pdf_bytes=pdf_bytes,
            year_label=district.year_label,
            lb_type_label=district.lb_type_label,
            district_name=district.district_name,
            lb_name=lb.lb_name,
            project_no=project.project_no,
            original_filename=original_filename,
        )

        # Update PDF record with GCS info
        pdf_repo.mark_uploaded(
            pdf_id=pdf_record.id,
            gcs_bucket=gcs.bucket_name,
            gcs_path=gcs_path,
            file_size_bytes=file_size,
            content_hash=content_hash,
        )

        # Mark project as downloaded
        project_repo.mark_downloaded(project.id)
        session.commit()

        return True, current_page, None

    except IntegrityError as e:
        # PDF already exists (race: duplicate task or retry). Mark done and continue.
        session.rollback()
        if "pdfs_project_id_key" in str(e) or "duplicate key" in str(e).lower():
            project_repo.mark_downloaded(project.id)
            session.commit()
            return True, current_page, None
        return False, current_page, str(e)
    except Exception as e:
        session.rollback()
        return False, current_page, str(e)


@celery_app.task(
    bind=True,
    name="sulekha.tasks.pdf_scraper.download_pdfs_for_local_body",
    max_retries=2,
    default_retry_delay=120,
)
def download_pdfs_for_local_body(self, local_body_id: str) -> dict:
    """Phase 4 (Batched): Download all PDFs for a local body in one session.

    This task navigates to the local body once, then downloads all pending PDFs
    by iterating through pages. Much more efficient than per-project navigation.

    Args:
        local_body_id: UUID of the local body to process

    Returns:
        Dictionary with download statistics
    """
    logger.info("Downloading PDFs for local body (batched)", local_body_id=local_body_id)

    stats = {
        "local_body_id": local_body_id,
        "total_projects": 0,
        "downloaded": 0,
        "missing": 0,
        "errors": 0,
        "error_details": [],
    }

    with get_session() as session:
        project_repo = ProjectRepository(session)
        lb_repo = LocalBodyRepository(session)
        district_repo = DistrictRepository(session)
        pdf_repo = PdfRepository(session)

        # Get local body and district
        lb = lb_repo.get(local_body_id)
        if not lb:
            stats["error_details"].append(f"Local body not found: {local_body_id}")
            logger.error("Local body not found", local_body_id=local_body_id)
            return stats

        district = district_repo.get(lb.district_id)
        if not district:
            stats["error_details"].append(f"District not found: {lb.district_id}")
            logger.error("District not found", district_id=lb.district_id)
            return stats

        # Get pending projects for this local body
        pending_projects = project_repo.get_pending_pdfs_for_local_body(local_body_id)
        stats["total_projects"] = len(pending_projects)

        if not pending_projects:
            logger.info("No pending PDFs for local body", local_body_id=local_body_id)
            return stats

        logger.info(
            "Processing local body PDFs",
            lb_name=lb.lb_name,
            district=district.district_name,
            year=district.year_label,
            pending_count=len(pending_projects),
        )

        with SulekhaClient() as client:
            try:
                # Navigate to the local body ONCE
                client.load_base()

                # Select year
                result = client.postback(
                    "drpYear", "", updates={"drpYear": str(district.year_val)}
                )
                if not result.success:
                    raise Exception(f"Failed to select year: {result.error}")

                # Select LB type
                result = client.postback(
                    "drpType", "", updates={"drpType": str(district.lb_type_val)}
                )
                if not result.success:
                    raise Exception(f"Failed to select LB type: {result.error}")

                # Click on district
                result = client.postback(
                    district.postback_target,
                    district.postback_argument,
                )
                if not result.success:
                    raise Exception(f"Failed to select district: {result.error}")

                # Click on local body
                result = client.postback(
                    lb.postback_target,
                    lb.postback_argument,
                )
                if not result.success:
                    raise Exception(f"Failed to select local body: {result.error}")

                # Now iterate through all pending projects
                current_page = 1
                consecutive_errors = 0
                max_consecutive_errors = 5

                for project in pending_projects:
                    # Mark as downloading
                    project_repo.mark_downloading(project.id)
                    session.commit()

                    success, current_page, error = _download_single_pdf(
                        client=client,
                        project=project,
                        district=district,
                        lb=lb,
                        pdf_repo=pdf_repo,
                        project_repo=project_repo,
                        session=session,
                        current_page=current_page,
                    )

                    if success:
                        if error is None:
                            stats["downloaded"] += 1
                        else:
                            stats["missing"] += 1
                        consecutive_errors = 0
                    else:
                        stats["errors"] += 1
                        stats["error_details"].append(f"{project.project_no}: {error}")
                        project_repo.mark_error(project.id, error or "Unknown error")
                        session.commit()
                        consecutive_errors += 1

                        # If too many consecutive errors, bail out
                        if consecutive_errors >= max_consecutive_errors:
                            logger.warning(
                                "Too many consecutive errors, stopping batch",
                                local_body_id=local_body_id,
                                consecutive_errors=consecutive_errors,
                            )
                            break

                logger.info(
                    "Local body PDF batch complete",
                    local_body_id=local_body_id,
                    lb_name=lb.lb_name,
                    downloaded=stats["downloaded"],
                    missing=stats["missing"],
                    errors=stats["errors"],
                )

            except Exception as e:
                session.rollback()
                logger.error(
                    "Failed to process local body PDFs",
                    local_body_id=local_body_id,
                    error=str(e),
                )
                stats["error_details"].append(f"Navigation error: {str(e)}")
                raise self.retry(exc=e)

    return stats


@celery_app.task(
    bind=True,
    name="sulekha.tasks.pdf_scraper.download_pdf_for_project",
    max_retries=3,
    default_retry_delay=60,
)
def download_pdf_for_project(self, project_id: str) -> dict:
    """Phase 4: Download PDF for a specific project and upload to GCS.

    This task navigates to the project detail page, follows the PDF redirect,
    downloads the PDF, and uploads it to GCS.

    Args:
        project_id: UUID of the project to process

    Returns:
        Dictionary with download statistics
    """
    logger.info("Downloading PDF for project", project_id=project_id)

    stats = {
        "project_id": project_id,
        "pdf_downloaded": False,
        "pdf_uploaded": False,
        "file_size_bytes": 0,
        "gcs_path": None,
        "error": None,
    }

    with get_session() as session:
        project_repo = ProjectRepository(session)
        lb_repo = LocalBodyRepository(session)
        district_repo = DistrictRepository(session)
        pdf_repo = PdfRepository(session)

        # Get project and related entities
        project = project_repo.get(project_id)
        if not project:
            stats["error"] = f"Project not found: {project_id}"
            logger.error("Project not found", project_id=project_id)
            return stats

        lb = lb_repo.get(project.local_body_id)
        if not lb:
            stats["error"] = f"Local body not found: {project.local_body_id}"
            return stats

        district = district_repo.get(lb.district_id)
        if not district:
            stats["error"] = f"District not found: {lb.district_id}"
            return stats

        logger.info(
            "Processing project",
            project_no=project.project_no,
            project_name=project.project_name[:50],
            lb_name=lb.lb_name,
            district=district.district_name,
        )

        # Mark as downloading
        project_repo.mark_downloading(project_id)
        session.commit()

        with SulekhaClient() as client:
            try:
                # Navigate to the project's page
                client.load_base()

                # Select year
                result = client.postback(
                    "drpYear", "", updates={"drpYear": str(district.year_val)}
                )
                if not result.success:
                    raise Exception(f"Failed to select year: {result.error}")

                # Select LB type
                result = client.postback(
                    "drpType", "", updates={"drpType": str(district.lb_type_val)}
                )
                if not result.success:
                    raise Exception(f"Failed to select LB type: {result.error}")

                # Click on district
                result = client.postback(
                    district.postback_target,
                    district.postback_argument,
                )
                if not result.success:
                    raise Exception(f"Failed to select district: {result.error}")

                # Click on local body
                result = client.postback(
                    lb.postback_target,
                    lb.postback_argument,
                )
                if not result.success:
                    raise Exception(f"Failed to select local body: {result.error}")

                # Navigate to the correct page if project is not on page 1
                if project.page_number and project.page_number > 1:
                    logger.debug(
                        "Navigating to project page",
                        page=project.page_number,
                        project_no=project.project_no,
                    )
                    result = client.postback(
                        "gvProjects",
                        f"Page${project.page_number}",
                    )
                    if not result.success:
                        raise Exception(
                            f"Failed to navigate to page {project.page_number}: {result.error}"
                        )

                # Click on project - use stream=True to catch PDF responses
                result = client.postback(
                    "gvProjects",
                    project.select_argument,
                    stream=True,
                )

                if not result.success:
                    raise Exception(f"Failed to click project: {result.error}")

                pdf_bytes = None
                original_filename = None

                # Check if response is directly a PDF (content-type check)
                if result.response:
                    content_type = result.response.headers.get("Content-Type", "").lower()
                    content_disp = result.response.headers.get("Content-Disposition", "").lower()
                    
                    if "application/pdf" in content_type or ".pdf" in content_disp:
                        # PDF returned directly in response
                        logger.info("Got PDF directly from postback response")
                        pdf_bytes = result.response.content
                        
                        # Extract filename from Content-Disposition
                        if "filename=" in content_disp:
                            match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', content_disp)
                            if match:
                                original_filename = match.group(1).strip()

                # If not PDF, look for PDF URL in HTML response
                if pdf_bytes is None and result.soup:
                    pdf_url = _find_pdf_url_in_html(result.soup)
                    if pdf_url:
                        logger.info("Found PDF URL in HTML", url=pdf_url)
                        pdf_bytes, original_filename, download_error = client.download_pdf(pdf_url)
                        if download_error:
                            raise Exception(f"Failed to download PDF from URL: {download_error}")

                # No PDF found
                if pdf_bytes is None:
                    logger.warning(
                        "No PDF found for project",
                        project_id=project_id,
                    )
                    project_repo.mark_missing(project_id)
                    session.commit()
                    stats["error"] = "No PDF found"
                    return stats

                stats["pdf_downloaded"] = True
                stats["file_size_bytes"] = len(pdf_bytes)

                # Skip if PDF already exists (race: another task processed this project)
                existing_pdf = pdf_repo.get_by_project(project_id)
                if existing_pdf and existing_pdf.status == GcsUploadStatus.UPLOADED:
                    project_repo.mark_downloaded(project_id)
                    session.commit()
                    stats["pdf_uploaded"] = True
                    stats["gcs_path"] = existing_pdf.gcs_path
                    return stats

                # Determine source URL for record-keeping
                source_url = BASE_URL  # PDF came from postback response

                # Create PDF record
                pdf_record = pdf_repo.create(
                    project_id=project_id,
                    original_url=source_url,
                    original_filename=original_filename,
                    redirect_url=source_url,
                )
                pdf_record.downloaded_at = datetime.utcnow()
                session.commit()

                # Upload to GCS
                gcs = GCSStorage()
                gcs_path, content_hash, file_size = gcs.upload_pdf(
                    pdf_bytes=pdf_bytes,
                    year_label=district.year_label,
                    lb_type_label=district.lb_type_label,
                    district_name=district.district_name,
                    lb_name=lb.lb_name,
                    project_no=project.project_no,
                    original_filename=original_filename,
                )

                # Update PDF record with GCS info
                pdf_repo.mark_uploaded(
                    pdf_id=pdf_record.id,
                    gcs_bucket=gcs.bucket_name,
                    gcs_path=gcs_path,
                    file_size_bytes=file_size,
                    content_hash=content_hash,
                )

                # Mark project as downloaded
                project_repo.mark_downloaded(project_id)
                session.commit()

                stats["pdf_uploaded"] = True
                stats["gcs_path"] = gcs_path

                logger.info(
                    "PDF downloaded and uploaded",
                    project_id=project_id,
                    gcs_path=gcs_path,
                    file_size=file_size,
                )

            except IntegrityError as e:
                session.rollback()
                if "pdfs_project_id_key" in str(e) or "duplicate key" in str(e).lower():
                    existing = pdf_repo.get_by_project(project_id)
                    project_repo.mark_downloaded(project_id)
                    session.commit()
                    stats["pdf_uploaded"] = True
                    if existing:
                        stats["gcs_path"] = existing.gcs_path
                    return stats
                stats["error"] = str(e)
                project_repo.mark_error(project_id, str(e))
                session.commit()
                raise self.retry(exc=e)
            except Exception as e:
                session.rollback()
                stats["error"] = str(e)
                project_repo.mark_error(project_id, str(e))
                session.commit()
                logger.error(
                    "Failed to download PDF",
                    project_id=project_id,
                    error=str(e),
                )
                raise self.retry(exc=e)

    return stats


@celery_app.task(
    bind=True,
    name="sulekha.tasks.pdf_scraper.run_phase4",
    max_retries=0,
)
def run_phase4(self, batch_size: int = 50) -> dict:
    """Run Phase 4: Download PDFs for all pending projects (batched by local body).

    This orchestrator task enqueues batched download tasks for each local body
    that has pending PDFs. Each task processes all PDFs for a local body in
    one session, which is much more efficient than per-project tasks.

    Args:
        batch_size: Number of local bodies to process in this batch

    Returns:
        Dictionary with orchestration statistics
    """
    logger.info("Running Phase 4: Download PDFs (batched)", batch_size=batch_size)

    stats = {
        "pending_local_bodies": 0,
        "tasks_enqueued": 0,
    }

    with get_session() as session:
        project_repo = ProjectRepository(session)

        # Get local bodies with pending PDFs
        pending_lb_ids = project_repo.get_local_bodies_with_pending_pdfs(limit=batch_size)
        stats["pending_local_bodies"] = len(pending_lb_ids)

        logger.info("Found local bodies with pending PDFs", count=len(pending_lb_ids))

        # Enqueue batched tasks for each local body
        for lb_id in pending_lb_ids:
            # Mark all pending PDFs for this local body as DOWNLOADING
            # This may return 0 if projects are already DOWNLOADING from interrupted runs
            marked_count = project_repo.mark_local_body_pdfs_downloading(lb_id)
            
            # Always enqueue the task - the task will find DOWNLOADING projects too
            download_pdfs_for_local_body.delay(lb_id)
            stats["tasks_enqueued"] += 1
            logger.debug(
                "Enqueued batch task",
                local_body_id=lb_id,
                projects_marked=marked_count,
            )

        # Commit all status changes
        session.commit()

    logger.info("Phase 4 orchestration complete", **stats)
    return stats
