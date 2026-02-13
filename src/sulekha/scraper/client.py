"""HTTP client for interacting with the Sulekha portal.

This module provides the SulekhaClient class that handles all HTTP communication
with the Kerala Sulekha portal, including ASP.NET WebForms postback management,
session handling, and automatic retries with exponential backoff.
"""

import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
import structlog
from bs4 import BeautifulSoup
from tenacity import (
    RetryError,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from sulekha.config import settings

logger = structlog.get_logger(__name__)

# Regex to extract postback parameters from JavaScript calls
POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")


class SulekhaClientError(Exception):
    """Base exception for SulekhaClient errors."""

    pass


class SessionExpiredError(SulekhaClientError):
    """Raised when the session appears to have expired."""

    pass


class NavigationError(SulekhaClientError):
    """Raised when navigation to a page fails."""

    pass


@dataclass
class FormState:
    """Represents the ASP.NET form state extracted from a page.

    ASP.NET WebForms uses hidden fields to maintain state between postbacks.
    This class captures those fields for use in subsequent requests.
    """

    viewstate: str = ""
    viewstate_generator: str = ""
    viewstate_encrypted: str = ""
    event_validation: str = ""
    form_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class PostbackResult:
    """Result of a postback request."""

    success: bool
    soup: Optional[BeautifulSoup] = None
    response: Optional[requests.Response] = None
    error: Optional[str] = None
    is_redirect: bool = False
    redirect_url: Optional[str] = None


class SulekhaClient:
    """HTTP client for the Sulekha portal.

    Handles ASP.NET WebForms postback mechanism, session management,
    and automatic retries with exponential backoff.

    Usage:
        client = SulekhaClient()
        client.load_base()

        # Select year and LB type
        client.postback("drpYear", "", updates={"drpYear": "28"})
        client.postback("drpType", "", updates={"drpType": "1"})

        # Now parse districts from client.soup
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        request_delay: Optional[float] = None,
        request_timeout: Optional[int] = None,
        max_retries: Optional[int] = None,
    ):
        """Initialize the client.

        Args:
            base_url: Override the default portal URL
            request_delay: Override the delay between requests
            request_timeout: Override the request timeout
            max_retries: Override the maximum retry count
        """
        self.base_url = base_url or settings.scraper_base_url
        self.request_delay = request_delay or settings.scraper_request_delay
        self.request_timeout = request_timeout or settings.scraper_request_timeout
        self.max_retries = max_retries or settings.scraper_max_retries

        self._session: Optional[requests.Session] = None
        self.form_state = FormState()
        self.soup: Optional[BeautifulSoup] = None

        # Statistics tracking
        self.request_count = 0
        self.retry_count = 0

        # Initialize session
        self._create_session()

    def _create_session(self) -> None:
        """Create a new HTTP session with appropriate headers."""
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Referer": self.base_url,
                "Origin": "https://plan.lsgkerala.gov.in",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
            }
        )
        logger.debug("Created new HTTP session")

    def _reset_session(self) -> None:
        """Reset the session (useful after multiple failures)."""
        if self._session:
            self._session.close()
        self._create_session()
        self.form_state = FormState()
        self.soup = None
        logger.info("Reset HTTP session")

    def _sleep(self) -> None:
        """Sleep with some randomization to avoid detection."""
        jitter = random.uniform(0.8, 1.2)
        delay = self.request_delay * jitter
        time.sleep(delay)

    def _parse_form_state(self, html: str) -> None:
        """Parse ASP.NET form state from HTML response.

        Args:
            html: HTML content to parse

        Raises:
            SessionExpiredError: If the form cannot be found (session may have expired)
        """
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", {"id": "form1"})

        if form is None:
            raise SessionExpiredError("Could not find form id='form1' - session may have expired")

        # Extract hidden state fields
        state = FormState()

        # Get ASP.NET hidden fields
        for field_id in ["__VIEWSTATE", "__VIEWSTATEGENERATOR", "__VIEWSTATEENCRYPTED", "__EVENTVALIDATION"]:
            elem = form.find("input", {"id": field_id})
            if elem:
                value = elem.get("value", "")
                if field_id == "__VIEWSTATE":
                    state.viewstate = value
                elif field_id == "__VIEWSTATEGENERATOR":
                    state.viewstate_generator = value
                elif field_id == "__VIEWSTATEENCRYPTED":
                    state.viewstate_encrypted = value
                elif field_id == "__EVENTVALIDATION":
                    state.event_validation = value

        # Extract all input fields
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
                state.form_fields[name] = inp.get("value", "")

        # Extract select fields (current selected values)
        for sel in form.find_all("select"):
            name = sel.get("name")
            if name:
                selected = sel.find("option", selected=True)
                if selected:
                    state.form_fields[name] = selected.get("value", "")
                else:
                    first = sel.find("option")
                    state.form_fields[name] = first.get("value", "") if first else ""

        self.form_state = state
        self.soup = soup

    def _build_postback_data(
        self,
        event_target: str,
        event_argument: str = "",
        updates: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        """Build the POST data for a postback request.

        Args:
            event_target: The __EVENTTARGET value (e.g., "drpYear", "gvState")
            event_argument: The __EVENTARGUMENT value (e.g., "Select$0")
            updates: Additional form field updates

        Returns:
            Dictionary of form data for the POST request
        """
        data = {
            "__EVENTTARGET": event_target,
            "__EVENTARGUMENT": event_argument,
            "__LASTFOCUS": "",
            "__VIEWSTATE": self.form_state.viewstate,
            "__VIEWSTATEGENERATOR": self.form_state.viewstate_generator,
            "__VIEWSTATEENCRYPTED": self.form_state.viewstate_encrypted,
            "__EVENTVALIDATION": self.form_state.event_validation,
        }

        # Add current form field values
        for key, value in self.form_state.form_fields.items():
            if key not in data:
                data[key] = value

        # Apply any updates
        if updates:
            data.update(updates)

        return data

    def _log_retry(self, retry_state: Any) -> None:
        """Log retry attempts."""
        self.retry_count += 1
        exc = retry_state.outcome.exception()
        logger.warning(
            "Request retry",
            attempt=retry_state.attempt_number,
            max_retries=self.max_retries,
            exception=str(exc),
            wait_time=retry_state.next_action.sleep if retry_state.next_action else 0,
        )

        # Reset session after multiple failures
        if retry_state.attempt_number in {3, 6}:
            logger.info("Resetting session after multiple failures")
            self._reset_session()

    @retry(
        stop=stop_after_attempt(8),
        wait=wait_exponential(multiplier=2, min=2, max=180),
        retry=retry_if_exception_type((requests.Timeout, requests.ConnectionError, requests.HTTPError)),
        reraise=True,
    )
    def _request(
        self,
        method: str,
        url: str,
        stream: bool = False,
        allow_redirects: bool = True,
        **kwargs: Any,
    ) -> requests.Response:
        """Make an HTTP request with automatic retries.

        Args:
            method: HTTP method (GET, POST)
            url: URL to request
            stream: Whether to stream the response
            allow_redirects: Whether to follow redirects
            **kwargs: Additional arguments for requests

        Returns:
            Response object

        Raises:
            requests.HTTPError: For 4xx/5xx responses
            requests.Timeout: On timeout
            requests.ConnectionError: On connection failure
        """
        self._sleep()
        self.request_count += 1

        response = self._session.request(
            method,
            url,
            timeout=self.request_timeout,
            stream=stream,
            allow_redirects=allow_redirects,
            **kwargs,
        )

        # Raise for 5xx errors (will trigger retry)
        if 500 <= response.status_code < 600:
            logger.warning("Server error", status_code=response.status_code, url=url)
            raise requests.HTTPError(f"HTTP {response.status_code}", response=response)

        # Raise for 4xx errors (client errors, no retry)
        response.raise_for_status()

        return response

    def load_base(self) -> None:
        """Load the base page and initialize form state.

        This should be called before any other operations to establish
        the initial session state.

        Raises:
            SulekhaClientError: If the base page cannot be loaded
        """
        logger.info("Loading base page", url=self.base_url)

        try:
            response = self._request("GET", self.base_url)
            self._parse_form_state(response.text)
            logger.info("Base page loaded successfully")
        except Exception as e:
            logger.error("Failed to load base page", error=str(e))
            raise SulekhaClientError(f"Failed to load base page: {e}") from e

    def postback(
        self,
        event_target: str,
        event_argument: str = "",
        updates: Optional[dict[str, str]] = None,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> PostbackResult:
        """Perform an ASP.NET postback.

        Args:
            event_target: The control that triggered the postback
            event_argument: Additional argument for the postback
            updates: Form field updates to include
            stream: Whether to stream the response (for large files)
            allow_redirects: Whether to follow redirects

        Returns:
            PostbackResult with the response details
        """
        logger.debug(
            "Performing postback",
            event_target=event_target,
            event_argument=event_argument,
            updates=updates,
        )

        data = self._build_postback_data(event_target, event_argument, updates)

        try:
            response = self._request(
                "POST",
                self.base_url,
                data=data,
                stream=stream,
                allow_redirects=allow_redirects,
            )

            # Check for redirect (e.g., to PDF)
            if response.history and not allow_redirects:
                return PostbackResult(
                    success=True,
                    response=response,
                    is_redirect=True,
                    redirect_url=response.url,
                )

            # Check if response is a redirect to PDF
            if response.status_code == 302 or (
                response.history and response.url != self.base_url
            ):
                return PostbackResult(
                    success=True,
                    response=response,
                    is_redirect=True,
                    redirect_url=response.url,
                )

            # Check content type for PDF
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/pdf" in content_type:
                return PostbackResult(
                    success=True,
                    response=response,
                    is_redirect=False,
                )

            # Parse HTML response
            self._parse_form_state(response.text)

            return PostbackResult(
                success=True,
                soup=self.soup,
                response=response,
            )

        except RetryError as e:
            logger.error("Postback failed after retries", error=str(e))
            return PostbackResult(success=False, error=str(e))
        except Exception as e:
            logger.error("Postback failed", error=str(e))
            return PostbackResult(success=False, error=str(e))

    def download_pdf(
        self,
        url: str,
    ) -> tuple[Optional[bytes], Optional[str], Optional[str]]:
        """Download a PDF from a URL.

        Args:
            url: URL to download from

        Returns:
            Tuple of (pdf_bytes, filename, error)
        """
        logger.debug("Downloading PDF", url=url)

        try:
            response = self._request("GET", url, stream=True)

            # Check if it's actually a PDF
            content_type = response.headers.get("Content-Type", "").lower()
            if "application/pdf" not in content_type and "application/octet-stream" not in content_type:
                return None, None, f"Unexpected content type: {content_type}"

            # Extract filename from Content-Disposition header
            filename = None
            content_disp = response.headers.get("Content-Disposition", "")
            if "filename=" in content_disp:
                match = re.search(r'filename="?([^";\n]+)"?', content_disp)
                if match:
                    filename = match.group(1)

            # Read PDF content
            pdf_bytes = response.content

            logger.info(
                "PDF downloaded",
                url=url,
                filename=filename,
                size_bytes=len(pdf_bytes),
            )

            return pdf_bytes, filename, None

        except Exception as e:
            logger.error("PDF download failed", url=url, error=str(e))
            return None, None, str(e)

    @staticmethod
    def extract_postback_params(tag: Any) -> Optional[tuple[str, str]]:
        """Extract postback target and argument from a link tag.

        Args:
            tag: BeautifulSoup tag (usually an <a> element)

        Returns:
            Tuple of (target, argument) or None if not a postback link
        """
        if tag is None:
            return None

        for attr in ["href", "onclick"]:
            value = tag.get(attr, "")
            if value:
                match = POSTBACK_RE.search(value)
                if match:
                    return match.group("target"), match.group("argument")

        return None

    def get_year_options(self) -> list[tuple[str, str]]:
        """Get available year options from the current page.

        Returns:
            List of (value, label) tuples for years
        """
        if not self.soup:
            return []

        select = self.soup.find("select", {"id": "drpYear"})
        if not select:
            return []

        options = []
        for opt in select.find_all("option"):
            val = opt.get("value", "")
            label = opt.get_text(strip=True)
            if val and val != "0":
                options.append((val, label))

        return options

    def get_lb_type_options(self) -> list[tuple[str, str]]:
        """Get available LB type options from the current page.

        Returns:
            List of (value, label) tuples for LB types
        """
        if not self.soup:
            return []

        select = self.soup.find("select", {"id": "drpType"})
        if not select:
            return []

        options = []
        for opt in select.find_all("option"):
            val = opt.get("value", "")
            label = opt.get_text(strip=True)
            if val and val != "0":
                options.append((val, label))

        return options

    def get_stats(self) -> dict[str, int]:
        """Get client statistics.

        Returns:
            Dictionary with request and retry counts
        """
        return {
            "request_count": self.request_count,
            "retry_count": self.retry_count,
        }

    def close(self) -> None:
        """Close the HTTP session."""
        if self._session:
            self._session.close()
            self._session = None
        logger.debug("Closed HTTP session")

    def __enter__(self) -> "SulekhaClient":
        """Context manager entry."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Context manager exit."""
        self.close()
