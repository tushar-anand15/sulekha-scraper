"""HTML parsers for extracting data from Sulekha portal pages.

This module provides functions to parse the various tables and elements
from the Sulekha portal HTML responses.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

import structlog
from bs4 import BeautifulSoup

from sulekha.scraper.client import SulekhaClient

logger = structlog.get_logger(__name__)

# Regex for extracting postback parameters
POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")


@dataclass
class DistrictRow:
    """Parsed row from the gvState (districts) table."""

    index: int
    district_name: str
    num_local_bodies: Optional[int]
    num_projects: Optional[int]
    postback_target: str
    postback_argument: str

    # Additional columns from the table
    productive: Optional[float] = None
    service: Optional[float] = None
    infrastructure: Optional[float] = None
    total: Optional[float] = None


@dataclass
class LocalBodyRow:
    """Parsed row from the gvStat (local bodies) table."""

    index: int
    lb_name: str
    num_projects: Optional[int]
    postback_target: str
    postback_argument: str

    # Additional columns
    productive: Optional[float] = None
    service: Optional[float] = None
    infrastructure: Optional[float] = None
    total: Optional[float] = None


@dataclass
class ProjectRow:
    """Parsed row from the gvProjects (projects) table."""

    project_no: str
    project_name: str
    formulation: Optional[str]
    expense: Optional[str]
    select_argument: str  # e.g., "Select$0"


@dataclass
class PagerInfo:
    """Pagination information from a projects table."""

    current_page: Optional[int] = None
    pages: dict[int, tuple[str, str]] = field(default_factory=dict)  # page_num -> (target, arg)
    ellipsis_postbacks: list[tuple[str, str]] = field(default_factory=list)
    has_more: bool = False


@dataclass
class ProjectsPageResult:
    """Result of parsing a projects page."""

    projects: list[ProjectRow]
    pager: PagerInfo


def _safe_int(text: str) -> Optional[int]:
    """Safely convert text to integer."""
    try:
        return int(text.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _safe_float(text: str) -> Optional[float]:
    """Safely convert text to float."""
    try:
        return float(text.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _extract_postback(tag) -> Optional[tuple[str, str]]:
    """Extract postback parameters from a tag."""
    if tag is None:
        return None

    for attr in ["href", "onclick"]:
        value = tag.get(attr, "")
        if value:
            match = POSTBACK_RE.search(value)
            if match:
                return match.group("target"), match.group("argument")

    return None


def parse_district_rows(soup: BeautifulSoup) -> list[DistrictRow]:
    """Parse district rows from the gvState table.

    The gvState table appears after selecting a year and LB type,
    showing all 14 Kerala districts with their project counts.

    Args:
        soup: BeautifulSoup object of the page

    Returns:
        List of DistrictRow objects
    """
    gv = soup.find("table", {"id": "gvState"})
    if not gv:
        logger.warning("gvState table not found")
        return []

    rows = gv.find_all("tr", recursive=False)
    districts = []

    # Skip header row (index 0) and footer row (last)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue

        try:
            # Column 0: Sl No (index)
            index = _safe_int(tds[0].get_text(strip=True))
            if index is None:
                continue

            # Column 1: District name
            district_name = tds[1].get_text(strip=True)

            # Column 2: Number of LBs
            num_local_bodies = _safe_int(tds[2].get_text(strip=True))

            # Column 3: Number of projects
            num_projects = _safe_int(tds[3].get_text(strip=True))

            # Additional financial columns (if present)
            productive = _safe_float(tds[4].get_text(strip=True)) if len(tds) > 4 else None
            service = _safe_float(tds[5].get_text(strip=True)) if len(tds) > 5 else None
            infrastructure = _safe_float(tds[6].get_text(strip=True)) if len(tds) > 6 else None
            total = _safe_float(tds[7].get_text(strip=True)) if len(tds) > 7 else None

            # Last column: Details link
            link = tds[-1].find("a")
            postback = _extract_postback(link)
            if not postback:
                logger.warning("No postback link found for district", district=district_name)
                continue

            districts.append(
                DistrictRow(
                    index=index,
                    district_name=district_name,
                    num_local_bodies=num_local_bodies,
                    num_projects=num_projects,
                    postback_target=postback[0],
                    postback_argument=postback[1],
                    productive=productive,
                    service=service,
                    infrastructure=infrastructure,
                    total=total,
                )
            )

        except Exception as e:
            logger.warning("Failed to parse district row", error=str(e))
            continue

    logger.info("Parsed districts", count=len(districts))
    return districts


def parse_local_body_rows(soup: BeautifulSoup) -> list[LocalBodyRow]:
    """Parse local body rows from the gvStat table.

    The gvStat table appears after clicking on a district,
    showing all local bodies within that district.

    Args:
        soup: BeautifulSoup object of the page

    Returns:
        List of LocalBodyRow objects
    """
    gv = soup.find("table", {"id": "gvStat"})
    if not gv:
        logger.warning("gvStat table not found")
        return []

    rows = gv.find_all("tr", recursive=False)
    local_bodies = []

    # Skip header row (index 0) and footer row (last)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 3:
            continue

        try:
            # Column 0: Serial No (index)
            index_text = tds[0].get_text(strip=True)
            index = _safe_int(index_text)
            if index is None:
                continue

            # Column 1: Local body name
            lb_name = tds[1].get_text(strip=True)

            # Column 2: Number of projects
            num_projects = _safe_int(tds[2].get_text(strip=True))

            # Additional financial columns (if present)
            productive = _safe_float(tds[3].get_text(strip=True)) if len(tds) > 3 else None
            service = _safe_float(tds[4].get_text(strip=True)) if len(tds) > 4 else None
            infrastructure = _safe_float(tds[5].get_text(strip=True)) if len(tds) > 5 else None
            total = _safe_float(tds[6].get_text(strip=True)) if len(tds) > 6 else None

            # Last column: Details link
            link = tds[-1].find("a")
            postback = _extract_postback(link)
            if not postback:
                logger.warning("No postback link found for local body", lb_name=lb_name)
                continue

            local_bodies.append(
                LocalBodyRow(
                    index=index,
                    lb_name=lb_name,
                    num_projects=num_projects,
                    postback_target=postback[0],
                    postback_argument=postback[1],
                    productive=productive,
                    service=service,
                    infrastructure=infrastructure,
                    total=total,
                )
            )

        except Exception as e:
            logger.warning("Failed to parse local body row", error=str(e))
            continue

    logger.info("Parsed local bodies", count=len(local_bodies))
    return local_bodies


def parse_projects_and_pager(soup: BeautifulSoup) -> ProjectsPageResult:
    """Parse projects and pagination from the gvProjects table.

    The gvProjects table shows projects for a local body,
    with pagination for large result sets.

    Args:
        soup: BeautifulSoup object of the page

    Returns:
        ProjectsPageResult with projects and pager info
    """
    gv = soup.find("table", {"id": "gvProjects"})
    if not gv:
        logger.warning("gvProjects table not found")
        return ProjectsPageResult(projects=[], pager=PagerInfo())

    rows = gv.find_all("tr", recursive=False)
    projects = []
    pager = PagerInfo()

    # Find pager row (contains nested table)
    pager_tr = None
    for tr in rows:
        if tr.find("table"):
            pager_tr = tr
            break

    # Parse project rows (skip header, skip pager row)
    for tr in rows[1:]:
        if tr is pager_tr:
            continue

        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue

        try:
            # Column 0: Project number
            project_no = tds[0].get_text(strip=True)

            # Column 1: Project name
            project_name = tds[1].get_text(strip=True)

            # Skip rows that look like totals (all numbers in project_name)
            if project_name and re.fullmatch(r"[0-9,\s]+", project_name):
                continue

            # Column 2: Formulation (budget)
            formulation = tds[2].get_text(strip=True)

            # Column 3: Expense
            expense = tds[3].get_text(strip=True)

            # Find Select link for this project
            select_arg = None
            for a in tr.find_all("a"):
                postback = _extract_postback(a)
                if postback and postback[0] == "gvProjects" and postback[1].startswith("Select$"):
                    select_arg = postback[1]
                    break

            if not select_arg:
                logger.warning("No select link found for project", project_no=project_no)
                continue

            projects.append(
                ProjectRow(
                    project_no=project_no,
                    project_name=project_name,
                    formulation=formulation,
                    expense=expense,
                    select_argument=select_arg,
                )
            )

        except Exception as e:
            logger.warning("Failed to parse project row", error=str(e))
            continue

    # Parse pager if present
    if pager_tr:
        # Find current page (span element)
        for span in pager_tr.find_all("span"):
            txt = span.get_text(strip=True)
            if txt.isdigit():
                pager.current_page = int(txt)
                break

        # Find page links
        for a in pager_tr.find_all("a"):
            text = a.get_text(strip=True)
            postback = _extract_postback(a)

            if not postback:
                continue

            if text == "...":
                pager.ellipsis_postbacks.append(postback)
                pager.has_more = True
            elif text.isdigit():
                pager.pages[int(text)] = postback

    logger.info(
        "Parsed projects page",
        project_count=len(projects),
        current_page=pager.current_page,
        available_pages=list(pager.pages.keys()),
        has_more=pager.has_more,
    )

    return ProjectsPageResult(projects=projects, pager=pager)


def get_next_page_postback(pager: PagerInfo) -> Optional[tuple[str, str]]:
    """Get the postback parameters for the next page.

    Args:
        pager: PagerInfo from current page

    Returns:
        (target, argument) tuple for next page, or None if no more pages
    """
    if pager.current_page is None:
        return None

    next_page = pager.current_page + 1

    # Check if next page is directly available
    if next_page in pager.pages:
        return pager.pages[next_page]

    # Use ellipsis to advance to more pages
    if pager.ellipsis_postbacks:
        return pager.ellipsis_postbacks[-1]

    return None
