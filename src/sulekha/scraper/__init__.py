"""Scraper module for Sulekha portal interaction."""

from sulekha.scraper.client import SulekhaClient
from sulekha.scraper.parsers import (
    parse_district_rows,
    parse_local_body_rows,
    parse_projects_and_pager,
)

__all__ = [
    "SulekhaClient",
    "parse_district_rows",
    "parse_local_body_rows",
    "parse_projects_and_pager",
]
