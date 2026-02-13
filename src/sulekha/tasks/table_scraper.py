"""Table scraper tasks for Phase 3 of the scraping pipeline.

Phase 3: Scrape all projects for each local body (with pagination)
"""

import structlog

from sulekha.db.repositories import (
    DistrictRepository,
    LocalBodyRepository,
    ProjectRepository,
)
from sulekha.db.session import get_session
from sulekha.scraper.client import SulekhaClient
from sulekha.scraper.parsers import (
    get_next_page_postback,
    parse_projects_and_pager,
)
from sulekha.tasks.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(
    bind=True,
    name="sulekha.tasks.table_scraper.scrape_projects_for_local_body",
    max_retries=3,
    default_retry_delay=120,
)
def scrape_projects_for_local_body(self, local_body_id: str) -> dict:
    """Phase 3: Scrape all projects for a specific local body.

    This task navigates to the local body's project table and scrapes all pages,
    storing projects in the database. Supports resumption from last_page_scraped.

    Args:
        local_body_id: UUID of the local body to process

    Returns:
        Dictionary with scraping statistics
    """
    logger.info("Scraping projects for local body", local_body_id=local_body_id)

    stats = {
        "local_body_id": local_body_id,
        "pages_scraped": 0,
        "projects_scraped": 0,
        "resumed_from_page": 0,
        "error": None,
    }

    with get_session() as session:
        lb_repo = LocalBodyRepository(session)
        district_repo = DistrictRepository(session)
        project_repo = ProjectRepository(session)

        # Get local body and district
        lb = lb_repo.get(local_body_id)
        if not lb:
            stats["error"] = f"Local body not found: {local_body_id}"
            logger.error("Local body not found", local_body_id=local_body_id)
            return stats

        district = district_repo.get(lb.district_id)
        if not district:
            stats["error"] = f"District not found: {lb.district_id}"
            logger.error("District not found", district_id=lb.district_id)
            return stats

        # Check if resuming
        resume_from_page = lb.last_page_scraped
        stats["resumed_from_page"] = resume_from_page

        logger.info(
            "Processing local body",
            lb_name=lb.lb_name,
            district=district.district_name,
            year=district.year_label,
            lb_type=district.lb_type_label,
            resume_from=resume_from_page,
        )

        # Mark as in progress
        lb_repo.mark_in_progress(local_body_id)
        session.commit()

        with SulekhaClient() as client:
            try:
                # Navigate to the local body's projects page
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

                # If resuming, navigate to the last page scraped
                if resume_from_page > 0:
                    logger.info("Resuming from page", page=resume_from_page)
                    # Navigate to resume page
                    for page_num in range(1, resume_from_page + 1):
                        result = parse_projects_and_pager(client.soup)
                        next_pb = get_next_page_postback(result.pager)
                        if next_pb and page_num < resume_from_page:
                            client.postback(next_pb[0], next_pb[1])

                # Scrape all pages
                visited_pages = set()
                current_page = resume_from_page or 1
                total_projects = lb.scraped_projects  # Start from existing count if resuming

                while True:
                    # Parse current page
                    if client.soup is None:
                        raise Exception("No response received")

                    page_result = parse_projects_and_pager(client.soup)
                    actual_page = page_result.pager.current_page or current_page

                    # Check for page loop
                    if actual_page in visited_pages:
                        logger.warning("Page loop detected", page=actual_page)
                        break
                    visited_pages.add(actual_page)

                    logger.debug(
                        "Processing page",
                        page=actual_page,
                        projects_on_page=len(page_result.projects),
                    )

                    # Store projects
                    for project in page_result.projects:
                        project_repo.upsert(
                            local_body_id=local_body_id,
                            project_no=project.project_no,
                            project_name=project.project_name,
                            formulation=project.formulation,
                            expense=project.expense,
                            page_number=actual_page,
                            select_argument=project.select_argument,
                        )
                        total_projects += 1
                        stats["projects_scraped"] += 1

                    stats["pages_scraped"] += 1

                    # Update progress
                    lb_repo.update_progress(
                        local_body_id,
                        last_page_scraped=actual_page,
                        scraped_projects=total_projects,
                    )
                    session.commit()

                    # Check if we've reached expected projects
                    if lb.expected_projects and total_projects >= lb.expected_projects:
                        logger.info(
                            "Reached expected project count",
                            expected=lb.expected_projects,
                            actual=total_projects,
                        )
                        break

                    # Navigate to next page
                    next_pb = get_next_page_postback(page_result.pager)
                    if not next_pb:
                        logger.info("No more pages")
                        break

                    result = client.postback(next_pb[0], next_pb[1])
                    if not result.success:
                        logger.warning("Failed to navigate to next page", error=result.error)
                        break

                    current_page = actual_page + 1

                # Mark local body as done
                lb_repo.mark_done(local_body_id, total_projects)
                session.commit()

                logger.info(
                    "Local body processing complete",
                    lb_name=lb.lb_name,
                    pages=stats["pages_scraped"],
                    projects=stats["projects_scraped"],
                )

            except Exception as e:
                stats["error"] = str(e)
                lb_repo.mark_error(local_body_id, str(e))
                session.commit()
                logger.error(
                    "Failed to process local body",
                    local_body_id=local_body_id,
                    error=str(e),
                )
                raise self.retry(exc=e)

    return stats


@celery_app.task(
    bind=True,
    name="sulekha.tasks.table_scraper.run_phase3",
    max_retries=0,
)
def run_phase3(self, batch_size: int = 100) -> dict:
    """Run Phase 3: Scrape projects for all pending local bodies.

    This orchestrator task enqueues individual scraping tasks for each
    pending local body. Local bodies are marked as IN_PROGRESS to prevent
    duplicate task enqueuing.

    Args:
        batch_size: Number of local bodies to process in this batch

    Returns:
        Dictionary with orchestration statistics
    """
    logger.info("Running Phase 3: Scrape projects", batch_size=batch_size)

    stats = {
        "pending_local_bodies": 0,
        "tasks_enqueued": 0,
    }

    with get_session() as session:
        lb_repo = LocalBodyRepository(session)

        # Get pending local bodies
        pending = lb_repo.get_pending(limit=batch_size)
        stats["pending_local_bodies"] = len(pending)

        logger.info("Found pending local bodies", count=len(pending))

        # Enqueue tasks for each local body and mark as IN_PROGRESS
        for lb in pending:
            # Mark as IN_PROGRESS BEFORE enqueuing to prevent duplicates
            lb_repo.mark_in_progress(lb.id)
            scrape_projects_for_local_body.delay(lb.id)
            stats["tasks_enqueued"] += 1

        # Commit all IN_PROGRESS status changes
        session.commit()

    logger.info("Phase 3 orchestration complete", **stats)
    return stats
