"""PDF scraper tasks for Phase 4 of the scraping pipeline.

Phase 4: Download PDFs for each project and upload to GCS
"""

import re
from datetime import datetime
from typing import Optional
from urllib.parse import urljoin

import structlog
from bs4 import BeautifulSoup

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

            except Exception as e:
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
def run_phase4(self, batch_size: int = 500) -> dict:
    """Run Phase 4: Download PDFs for all pending projects.

    This orchestrator task enqueues individual download tasks for each
    pending project. Projects are marked as DOWNLOADING to prevent
    duplicate task enqueuing.

    Args:
        batch_size: Number of projects to process in this batch

    Returns:
        Dictionary with orchestration statistics
    """
    logger.info("Running Phase 4: Download PDFs", batch_size=batch_size)

    stats = {
        "pending_projects": 0,
        "tasks_enqueued": 0,
    }

    with get_session() as session:
        project_repo = ProjectRepository(session)

        # Get pending projects
        pending = project_repo.get_pending_pdfs(limit=batch_size)
        stats["pending_projects"] = len(pending)

        logger.info("Found pending projects for PDF download", count=len(pending))

        # Enqueue tasks for each project and mark as DOWNLOADING
        for project in pending:
            # Mark as DOWNLOADING BEFORE enqueuing to prevent duplicates
            project_repo.mark_downloading(project.id)
            download_pdf_for_project.delay(project.id)
            stats["tasks_enqueued"] += 1

        # Commit all DOWNLOADING status changes
        session.commit()

    logger.info("Phase 4 orchestration complete", **stats)
    return stats
