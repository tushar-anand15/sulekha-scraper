"""Discovery tasks for Phase 1 and Phase 2 of the scraping pipeline.

Phase 1: Discover all districts (Year x LB Type combinations)
Phase 2: Discover all local bodies for each district
"""

import structlog

from sulekha.db.models import DistrictStatus
from sulekha.db.repositories import DistrictRepository, LocalBodyRepository
from sulekha.db.session import get_session
from sulekha.scraper.client import SulekhaClient
from sulekha.scraper.parsers import parse_district_rows, parse_local_body_rows
from sulekha.tasks.celery_app import celery_app

logger = structlog.get_logger(__name__)


@celery_app.task(
    bind=True,
    name="sulekha.tasks.discovery.discover_all_districts",
    max_retries=3,
    default_retry_delay=60,
)
def discover_all_districts(self, scrape_run_id: str = None) -> dict:
    """Phase 1: Discover all districts for all year/lb_type combinations.

    This task iterates through all years and LB types, fetching the district
    table (gvState) for each combination and storing the districts in the database.

    Args:
        scrape_run_id: Optional scrape run ID for tracking

    Returns:
        Dictionary with discovery statistics
    """
    logger.info("Starting Phase 1: Discover all districts", scrape_run_id=scrape_run_id)

    stats = {
        "years_processed": 0,
        "lb_types_processed": 0,
        "districts_discovered": 0,
        "errors": [],
    }

    with SulekhaClient() as client:
        # Load base page and get dropdown options
        client.load_base()
        years = client.get_year_options()
        lb_types = client.get_lb_type_options()

        logger.info(
            "Found dropdown options",
            year_count=len(years),
            lb_type_count=len(lb_types),
        )

        with get_session() as session:
            district_repo = DistrictRepository(session)

            for year_val, year_label in years:
                # Select year
                result = client.postback("drpYear", "", updates={"drpYear": year_val})
                if not result.success:
                    stats["errors"].append(f"Failed to select year {year_label}: {result.error}")
                    continue

                stats["years_processed"] += 1

                for lb_type_val, lb_type_label in lb_types:
                    # Select LB type
                    result = client.postback("drpType", "", updates={"drpType": lb_type_val})
                    if not result.success:
                        stats["errors"].append(
                            f"Failed to select LB type {lb_type_label}: {result.error}"
                        )
                        continue

                    stats["lb_types_processed"] += 1

                    # Parse districts from gvState table
                    if client.soup is None:
                        continue

                    districts = parse_district_rows(client.soup)

                    logger.info(
                        "Discovered districts",
                        year=year_label,
                        lb_type=lb_type_label,
                        count=len(districts),
                    )

                    # Store districts in database
                    for district in districts:
                        district_repo.upsert(
                            year_val=int(year_val),
                            year_label=year_label,
                            lb_type_val=int(lb_type_val),
                            lb_type_label=lb_type_label,
                            district_index=district.index,
                            district_name=district.district_name,
                            postback_argument=district.postback_argument,
                            num_local_bodies=district.num_local_bodies,
                            num_projects=district.num_projects,
                            scrape_run_id=scrape_run_id,
                        )
                        stats["districts_discovered"] += 1

            session.commit()

    logger.info("Phase 1 complete", **stats)
    return stats


@celery_app.task(
    bind=True,
    name="sulekha.tasks.discovery.discover_local_bodies_for_district",
    max_retries=3,
    default_retry_delay=60,
)
def discover_local_bodies_for_district(self, district_id: str) -> dict:
    """Phase 2: Discover all local bodies for a specific district.

    This task navigates to the district and fetches the local body table (gvStat),
    storing all local bodies in the database.

    Args:
        district_id: UUID of the district to process

    Returns:
        Dictionary with discovery statistics
    """
    logger.info("Discovering local bodies for district", district_id=district_id)

    stats = {
        "district_id": district_id,
        "local_bodies_discovered": 0,
        "error": None,
    }

    with get_session() as session:
        district_repo = DistrictRepository(session)
        lb_repo = LocalBodyRepository(session)

        # Get district
        district = district_repo.get(district_id)
        if not district:
            stats["error"] = f"District not found: {district_id}"
            logger.error("District not found", district_id=district_id)
            return stats

        logger.info(
            "Processing district",
            district_name=district.district_name,
            year=district.year_label,
            lb_type=district.lb_type_label,
        )

        with SulekhaClient() as client:
            try:
                # Navigate to the district
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

                # Parse local bodies from gvStat table
                if client.soup is None:
                    raise Exception("No response received after selecting district")

                local_bodies = parse_local_body_rows(client.soup)

                logger.info(
                    "Discovered local bodies",
                    district=district.district_name,
                    count=len(local_bodies),
                )

                # Store local bodies in database
                for lb in local_bodies:
                    lb_repo.upsert(
                        district_id=district_id,
                        lb_index=lb.index,
                        lb_name=lb.lb_name,
                        postback_argument=lb.postback_argument,
                        expected_projects=lb.num_projects,
                    )
                    stats["local_bodies_discovered"] += 1

                # Mark district as done
                district_repo.mark_done(district_id)
                session.commit()

                logger.info(
                    "District processing complete",
                    district=district.district_name,
                    local_bodies=stats["local_bodies_discovered"],
                )

            except Exception as e:
                stats["error"] = str(e)
                district_repo.mark_error(district_id, str(e))
                session.commit()
                logger.error(
                    "Failed to process district",
                    district_id=district_id,
                    error=str(e),
                )
                raise self.retry(exc=e)

    return stats


@celery_app.task(
    bind=True,
    name="sulekha.tasks.discovery.run_phase2",
    max_retries=0,
)
def run_phase2(self, batch_size: int = 100) -> dict:
    """Run Phase 2: Discover local bodies for all pending districts.

    This orchestrator task enqueues individual discovery tasks for each
    pending district. Districts are marked as IN_PROGRESS to prevent
    duplicate task enqueuing.

    Args:
        batch_size: Number of districts to process in this batch

    Returns:
        Dictionary with orchestration statistics
    """
    logger.info("Running Phase 2: Discover local bodies", batch_size=batch_size)

    stats = {
        "pending_districts": 0,
        "tasks_enqueued": 0,
    }

    with get_session() as session:
        district_repo = DistrictRepository(session)

        # Get pending districts
        pending = district_repo.get_pending(limit=batch_size)
        stats["pending_districts"] = len(pending)

        logger.info("Found pending districts", count=len(pending))

        # Enqueue tasks for each district and mark as IN_PROGRESS
        for district in pending:
            # Mark as IN_PROGRESS BEFORE enqueuing to prevent duplicates
            district_repo.mark_in_progress(district.id)
            discover_local_bodies_for_district.delay(district.id)
            stats["tasks_enqueued"] += 1

        # Commit all IN_PROGRESS status changes
        session.commit()

    logger.info("Phase 2 orchestration complete", **stats)
    return stats
