"""Discovery task for the SAKARMA scraper pipeline.

Walks the District × LB Type → LB Name cascade once at the start of a run,
populates the four dimension tables (district, lb_type, year, lb), and seeds
an ``lb_progress`` row (status=pending) for every discovered LB so the
subsequent manifest tasks have a work queue to consume.

Cascade reset behavior
----------------------
Changing District or LB Type causes the server to reset the LB Name dropdown.
Therefore, for every (district, lb_type) pair we must re-select District first
and then LB Type against the **initial page state** — you cannot continue the
cascade across iterations of the inner loop without re-anchoring.  The
``initial_state`` captured at the top of the task is kept immutable and used
as the base for every pair.
"""

from __future__ import annotations

import re

import requests
import structlog

from sakarma.config import settings
from sakarma.db.repositories import (
    DistrictRepository,
    LBProgressRepository,
    LBRepository,
    LBTypeRepository,
    YearRepository,
)
from sakarma.db.session import get_session
from sakarma.scraper.client import SakarmaClient
from sakarma.scraper.parsers import parse_dropdown_options
from sakarma.scraper.protocol import (
    DDL_DISTRICT,
    DDL_LB_NAME,
    DDL_LB_TYPE,
    DDL_YEAR,
    FormState,
    parse_form_state,
)
from sakarma.tasks.celery_app import SakarmaTask, celery_app
from sakarma.utils.rate_limiter import get_rate_limiter

logger = structlog.get_logger(__name__)

_YEAR_INT_RE = re.compile(r"\b(\d{4})\b")


def _parse_year_int(year_text: str) -> int | None:
    """Extract the first 4-digit year from a label like ``'2024'`` or ``'2024-25'``."""
    match = _YEAR_INT_RE.search(year_text)
    return int(match.group(1)) if match else None


def _fetch_lb_options(
    client: SakarmaClient,
    base_state: FormState,
    district_id: int,
    lb_type_id: int,
) -> tuple[list[tuple[int, str]], bytes]:
    """Select district then lb_type and return parsed LB options plus raw HTML bytes.

    Keeps the cascade idempotent by always branching from *base_state* so that
    each (district, lb_type) pair starts from the same clean page state.

    Returns:
        Tuple of ``(lb_options, response_html_bytes)``.
    """
    state_d = client.select_dropdown(base_state, DDL_DISTRICT, str(district_id))
    # Perform the LB Type postback and capture raw HTML via a direct POST so we
    # can pass raw bytes to parse_dropdown_options.
    data = client._build_postback_data(
        state_d,
        event_target=DDL_LB_TYPE,
        event_argument="",
        updates={DDL_LB_TYPE: str(lb_type_id)},
    )
    response = client._request(
        "POST", state_d.page_url, is_postback=True, data=data
    )
    raw_html: bytes = response.content
    lb_options = parse_dropdown_options(raw_html, DDL_LB_NAME)
    return lb_options, raw_html


@celery_app.task(bind=True, base=SakarmaTask, name="sakarma.tasks.discovery.run")
def run(self, scrape_run_id: int) -> dict:
    """Walk District × LB Type → LB Name cascade, populate dimensions, seed lb_progress rows.

    Steps:
    1. Load the LBWise dashboard (one GET, captures VIEWSTATE).
    2. Parse district, lb_type, year dropdowns from the initial HTML.
    3. Upsert district, lb_type, year rows into the DB.
    4. For every (district, lb_type) pair, select_dropdown both dropdowns
       against the immutable initial_state, parse DDL_LB_NAME, upsert LB rows.
    5. Bulk-create lb_progress rows for all discovered LBs.
    6. Return a summary dict.

    Args:
        scrape_run_id: PK of the ``ScrapeRun`` row created by the orchestrator.

    Returns:
        Summary dict with keys ``districts``, ``lb_types``, ``years``,
        ``lbs``, and ``lb_progress_rows``.
    """
    log = logger.bind(scrape_run_id=scrape_run_id, task_id=self.request.id)
    log.info("discovery.run started")

    # ------------------------------------------------------------------
    # 1. Build HTTP session, load the page, capture raw HTML.
    # ------------------------------------------------------------------
    http_session = requests.Session()
    client = SakarmaClient(http_session, settings, get_rate_limiter())

    # GET the page and grab raw HTML bytes directly for dropdown parsing.
    page_url = client._abs(settings.scraper_lbwise_path)
    response = client._request("GET", page_url)
    raw_initial_html: bytes = response.content

    # Also parse the FormState so we can drive postbacks.
    initial_state: FormState = parse_form_state(raw_initial_html, page_url=page_url)

    # ------------------------------------------------------------------
    # 2. Parse top-level dimension options from the initial HTML.
    # ------------------------------------------------------------------
    district_options: list[tuple[int, str]] = parse_dropdown_options(
        raw_initial_html, DDL_DISTRICT
    )
    lb_type_options: list[tuple[int, str]] = parse_dropdown_options(
        raw_initial_html, DDL_LB_TYPE
    )
    year_options: list[tuple[int, str]] = parse_dropdown_options(
        raw_initial_html, DDL_YEAR
    )

    log.info(
        "Dropdown options parsed",
        n_districts=len(district_options),
        n_lb_types=len(lb_type_options),
        n_years=len(year_options),
    )

    # ------------------------------------------------------------------
    # 3. Upsert dimension rows.
    # ------------------------------------------------------------------
    with get_session() as session:
        district_repo = DistrictRepository(session)
        lb_type_repo = LBTypeRepository(session)
        year_repo = YearRepository(session)

        for district_id, name_ml in district_options:
            district_repo.upsert(id=district_id, name_ml=name_ml)

        for lb_type_id, name_ml in lb_type_options:
            lb_type_repo.upsert(id=lb_type_id, name_ml=name_ml)

        for year_id, year_text in year_options:
            year_int = _parse_year_int(year_text)
            if year_int is None:
                log.warning(
                    "Could not parse year_int; skipping year row",
                    year_id=year_id,
                    year_text=year_text,
                )
                continue
            year_repo.upsert(id=year_id, year_int=year_int)

    # ------------------------------------------------------------------
    # 4. District × LB Type → LB Name cascade.
    # ------------------------------------------------------------------
    all_lb_ids: list[int] = []

    for district_id, district_name in district_options:
        for lb_type_id, lb_type_name in lb_type_options:
            try:
                lb_options, _ = _fetch_lb_options(
                    client, initial_state, district_id, lb_type_id
                )
            except Exception as exc:
                log.warning(
                    "Failed to fetch LB options for district×lb_type; skipping",
                    district_id=district_id,
                    district_name=district_name,
                    lb_type_id=lb_type_id,
                    lb_type_name=lb_type_name,
                    error=str(exc),
                )
                continue

            if not lb_options:
                log.info(
                    "No LBs for this district×lb_type combination (expected for some)",
                    district_id=district_id,
                    district_name=district_name,
                    lb_type_id=lb_type_id,
                    lb_type_name=lb_type_name,
                )
                continue

            with get_session() as session:
                lb_repo = LBRepository(session)
                for lb_id, lb_name_ml in lb_options:
                    lb_repo.upsert(
                        id=lb_id,
                        district_id=district_id,
                        lb_type_id=lb_type_id,
                        name_ml=lb_name_ml,
                        scrape_run_id=scrape_run_id,
                    )
                    all_lb_ids.append(lb_id)

            log.info(
                "Upserted LBs",
                district_id=district_id,
                district_name=district_name,
                lb_type_id=lb_type_id,
                lb_type_name=lb_type_name,
                lb_count=len(lb_options),
            )

    # Deduplicate: an LB id could theoretically appear in multiple combos;
    # preserve first-seen order.
    unique_lb_ids: list[int] = list(dict.fromkeys(all_lb_ids))

    # ------------------------------------------------------------------
    # 5. Seed lb_progress rows (idempotent via ON CONFLICT DO NOTHING).
    # ------------------------------------------------------------------
    with get_session() as session:
        progress_repo = LBProgressRepository(session)
        progress_rows = progress_repo.bulk_create(
            scrape_run_id=scrape_run_id, lb_ids=unique_lb_ids
        )

    summary = {
        "districts": len(district_options),
        "lb_types": len(lb_type_options),
        "years": len(year_options),
        "lbs": len(unique_lb_ids),
        "lb_progress_rows": len(progress_rows),
    }
    log.info("discovery.run complete", **summary)
    return summary
