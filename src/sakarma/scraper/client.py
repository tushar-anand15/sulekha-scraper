"""Session-bound HTTP client for the SAKARMA portal.

Unlike the single-page Sulekha client, SAKARMA spans three pages
(``LBWiseDashBoard.aspx``, ``PublicMinutes.aspx``, ``PublicDRegister.aspx``).
Each page has its own ViewState; navigation between them is carried by
session cookies on a shared :class:`requests.Session`.

The client itself is **stateless** across calls — every method takes an
explicit :class:`FormState` and returns a new one. This makes parallel
drill-down workflows trivial to compose without sharing mutable client state.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Optional

import requests
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

from sakarma.scraper.protocol import (
    FORM_PREFIX,
    GRID_MEETING_DETAILS,
    FormState,
    grid_dr_target,
    grid_select_argument,
    grid_select_target,
    parse_form_state,
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
class SakarmaClientError(Exception):
    """Base exception for SakarmaClient errors."""


class SessionExpiredError(SakarmaClientError):
    """Raised when a postback response lacks ``__VIEWSTATE`` (session lost)."""


class PaginationDetectedError(SakarmaClientError):
    """Raised when ``GridMeetingDEtails`` exposes pager controls.

    Pagination would silently under-collect rows, so we fail loudly until the
    plan adds explicit pager handling. The message carries ``category=...``
    and ``url=...`` context for the orchestrator to log.
    """


# ---------------------------------------------------------------------------
# Helpers used by detection logic
# ---------------------------------------------------------------------------
_PAGER_DOPOSTBACK_RE = re.compile(r"__doPostBack\([^)]*Page\$\d+", re.IGNORECASE)
_PAGER_CLASS_RE = re.compile(r"\bpager\b", re.IGNORECASE)
_VIEWSTATE_PROBE_RE = re.compile(rb"name=\"__VIEWSTATE\"", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class SakarmaClient:
    """Multi-page WebForms client backed by a shared :class:`requests.Session`."""

    def __init__(
        self,
        session: requests.Session,
        settings: Any,
        rate_limiter: Any,
        logger: Optional[structlog.BoundLogger] = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._rate_limiter = rate_limiter
        self._logger = logger or structlog.get_logger(__name__)
        self._last_referer: Optional[str] = None
        self.request_count = 0

        # Apply persistent default headers up-front; per-request Referer is
        # stamped in :meth:`_request`.
        self._session.headers.setdefault("User-Agent", settings.user_agent)
        self._session.headers.setdefault(
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        )
        self._session.headers.setdefault("Accept-Language", "en-US,en;q=0.9")

    # ------------------------------------------------------------------
    # URL helpers
    # ------------------------------------------------------------------
    def _abs(self, path: str) -> str:
        base = self._settings.scraper_base_url.rstrip("/")
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{base}{path if path.startswith('/') else '/' + path}"

    # ------------------------------------------------------------------
    # Detection helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _has_viewstate(html_bytes: bytes) -> bool:
        return bool(_VIEWSTATE_PROBE_RE.search(html_bytes))

    @staticmethod
    def _detect_pagination(html_bytes: bytes) -> bool:
        """Heuristic: True if the response shows pager controls in the grid."""
        # Cheapest check: __doPostBack(..., 'Page$N')
        try:
            text = html_bytes.decode("utf-8", errors="replace")
        except Exception:
            return False
        if _PAGER_DOPOSTBACK_RE.search(text):
            return True
        # Look for class="pager" inside a tr (common GridView pager skeleton)
        # and require the grid name in the same blob to avoid unrelated
        # paginated widgets on the page.
        if "GridMeetingDEtails" in text and _PAGER_CLASS_RE.search(text):
            # Only treat as pagination if a <tr> with that class exists.
            return bool(re.search(r'<tr[^>]*class="[^"]*\bpager\b', text, re.IGNORECASE))
        return False

    # ------------------------------------------------------------------
    # Core request primitive (retry + rate-limit + jitter)
    # ------------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        is_postback: bool = False,
        check_pagination: bool = False,
        category: Optional[str] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Issue a single rate-limited HTTP request with retry."""

        @retry(
            stop=stop_after_attempt(self._settings.scraper_max_retries),
            wait=wait_random_exponential(
                multiplier=self._settings.scraper_backoff_base,
                max=self._settings.scraper_backoff_max,
            ),
            retry=retry_if_exception_type(
                (requests.RequestException, requests.HTTPError)
            ),
            reraise=True,
        )
        def _do() -> requests.Response:
            with self._rate_limiter.acquire():
                # Random jitter sleep before the request.
                delay = random.uniform(
                    self._settings.scraper_delay_min,
                    self._settings.scraper_delay_max,
                )
                time.sleep(delay)

                headers = dict(kwargs.pop("headers", {}) or {})
                if self._last_referer and "Referer" not in headers:
                    headers["Referer"] = self._last_referer
                headers.setdefault("User-Agent", self._settings.user_agent)

                self.request_count += 1
                response = self._session.request(
                    method,
                    url,
                    timeout=self._settings.scraper_request_timeout,
                    headers=headers,
                    **kwargs,
                )

                if 500 <= response.status_code < 600:
                    raise requests.HTTPError(
                        f"HTTP {response.status_code} from {url}", response=response
                    )

                response.raise_for_status()
                return response

        response = _do()

        # Track Referer for the next request.
        self._last_referer = url

        # Session-expiry detection: postback responses must echo VIEWSTATE.
        if is_postback and not self._has_viewstate(response.content):
            raise SessionExpiredError(
                f"Postback response from {url} missing __VIEWSTATE — session expired"
            )

        if check_pagination and self._detect_pagination(response.content):
            raise PaginationDetectedError(
                f"Pagination detected (category={category!r}, url={url})"
            )

        return response

    # ------------------------------------------------------------------
    # Postback data assembly
    # ------------------------------------------------------------------
    @staticmethod
    def _build_postback_data(
        state: FormState,
        event_target: str,
        event_argument: str = "",
        updates: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        """Build a POST body that echoes ``state`` plus the requested overrides."""
        data: dict[str, str] = {}
        # Start from the captured form snapshot so every input round-trips.
        for k, v in state.form_fields.items():
            data[k] = v
        # Stamp ASP.NET hidden fields (these may also appear in form_fields,
        # but explicit assignment keeps them authoritative).
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument
        data["__LASTFOCUS"] = state.last_focus
        data["__VIEWSTATE"] = state.viewstate
        if state.viewstate_generator:
            data["__VIEWSTATEGENERATOR"] = state.viewstate_generator
        if state.viewstate_encrypted:
            data["__VIEWSTATEENCRYPTED"] = state.viewstate_encrypted
        data["__EVENTVALIDATION"] = state.event_validation

        if updates:
            data.update(updates)
        return data

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def load_page(self, path: str) -> FormState:
        """GET ``path`` and return its parsed :class:`FormState`."""
        url = self._abs(path)
        response = self._request("GET", url)
        if not self._has_viewstate(response.content):
            raise SessionExpiredError(
                f"Initial GET of {url} returned no __VIEWSTATE — landing/login?"
            )
        return parse_form_state(response.content, page_url=url)

    def select_dropdown(
        self, state: FormState, ddl_name: str, value: str
    ) -> FormState:
        """Postback a cascading dropdown change.

        The caller is responsible for re-setting downstream dropdowns when
        an upstream postback resets them — this method only fires the change
        for the named control.
        """
        updates = {ddl_name: value}
        data = self._build_postback_data(
            state, event_target=ddl_name, event_argument="", updates=updates
        )
        response = self._request(
            "POST", state.page_url, is_postback=True, data=data
        )
        return parse_form_state(response.content, page_url=state.page_url)

    def click_button(self, state: FormState, button_target: str) -> FormState:
        """Click one of the four KPI drill-down buttons.

        Triggers pagination detection on the response since the resulting
        ``GridMeetingDEtails`` is what we ingest row-by-row.
        """
        # Map the button name to a category for clearer pagination errors.
        category = button_target.split("$")[-1]
        data = self._build_postback_data(
            state, event_target=button_target, event_argument=""
        )
        response = self._request(
            "POST",
            state.page_url,
            is_postback=True,
            check_pagination=True,
            category=category,
            data=data,
        )
        return parse_form_state(response.content, page_url=state.page_url)

    def select_grid_row(self, state: FormState, row_index: int) -> FormState:
        """Trigger ``Select$N`` on ``GridMeetingDEtails`` to bind the row to the session.

        The response body contains a ``window.open(...)`` script that the
        scraper ignores; the meaningful side effect is server-side session
        state. The caller follows up with :meth:`fetch_public_page` against
        ``PublicMinutes.aspx``.
        """
        data = self._build_postback_data(
            state,
            event_target=grid_select_target(),
            event_argument=grid_select_argument(row_index),
        )
        response = self._request(
            "POST", state.page_url, is_postback=True, data=data
        )
        return parse_form_state(response.content, page_url=state.page_url)

    def click_dr(self, state: FormState, row_index: int) -> FormState:
        """Trigger the per-row ``lnkDR`` link to bind the DR to the session."""
        data = self._build_postback_data(
            state, event_target=grid_dr_target(row_index), event_argument=""
        )
        response = self._request(
            "POST", state.page_url, is_postback=True, data=data
        )
        return parse_form_state(response.content, page_url=state.page_url)

    def fetch_public_page(self, path: str) -> bytes:
        """Plain GET of an artifact page; returns raw bytes verbatim.

        Used for ``PublicMinutes.aspx`` and ``PublicDRegister.aspx`` after
        the corresponding ``select_grid_row`` / ``click_dr`` call has bound
        the row to the server-side session.
        """
        url = self._abs(path)
        response = self._request("GET", url)
        return response.content

    def click_attachment_lnkfileview(
        self, state: FormState, target: str
    ) -> tuple[bytes, str]:
        """Trigger an attachment ``lnkFileView`` postback and return ``(bytes, filename)``.

        ``filename`` is parsed from ``Content-Disposition``; if the header is
        missing the function returns ``""`` so the caller can fall back to a
        generated name.
        """
        data = self._build_postback_data(
            state, event_target=target, event_argument=""
        )
        # Attachment downloads are binary — do not run pagination/viewstate
        # checks against them.
        response = self._request("POST", state.page_url, data=data)
        filename = ""
        disposition = response.headers.get("Content-Disposition", "")
        if disposition:
            match = _FILENAME_RE.search(disposition)
            if match:
                filename = match.group(1).strip()
        return response.content, filename
