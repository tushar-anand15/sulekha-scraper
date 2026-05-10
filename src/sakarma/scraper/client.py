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
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
import structlog
from bs4 import BeautifulSoup
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


class ServerSideUnavailableError(SakarmaClientError):
    """Raised when the source server returns an unhandled exception page.

    Verified live: certain LBs / meetings cause the SAKARMA portal's
    ASP.NET handler to throw ``Object reference not set to an instance
    of an object.`` (a C# ``NullReferenceException``) — the page renders
    HTTP 500 with a ``<title>`` containing that exact phrase, and the
    same body is returned regardless of which row was selected. This is
    a server-side data bug we cannot fix from the scraper.

    Distinct from a generic 5xx because it must NOT be retried (the
    server will keep crashing on the same null), and it must NOT abort
    the LB (other meetings/LBs may still work). The artifacts task
    catches this per-row and skips.
    """


# ---------------------------------------------------------------------------
# MS-AJAX async-postback delta response
# ---------------------------------------------------------------------------
@dataclass
class AsyncPostbackResponse:
    """Parsed MS-AJAX delta response from an UpdatePanel async postback.

    The body format is a sequence of pipe-delimited segments::

        <length>|<type>|<id>|<content>|<length>|<type>|<id>|<content>|...

    where ``length`` is the character count of ``content``. We read each
    segment, populate ``update_panels`` (HTML for each refreshed panel) and
    ``hidden_fields`` (incl. updated __VIEWSTATE / __EVENTVALIDATION), and
    expose helpers to find tables across all panel HTML.
    """

    update_panels: dict[str, bytes] = field(default_factory=dict)
    hidden_fields: dict[str, str] = field(default_factory=dict)
    page_url: str = ""
    raw: bytes = b""

    @classmethod
    def from_delta(cls, body: bytes, page_url: str) -> "AsyncPostbackResponse":
        """Parse the pipe-delimited MS-AJAX delta body."""
        out = cls(page_url=page_url, raw=body)
        text = body.decode("utf-8", errors="replace")
        i = 0
        n = len(text)
        while i < n:
            # length
            j = text.find("|", i)
            if j == -1:
                break
            try:
                length = int(text[i:j])
            except ValueError:
                # Some ASP.NET errors render before the delta starts (HTML).
                # Bail out — caller may still inspect ``raw``.
                break
            i = j + 1
            # type
            j = text.find("|", i)
            if j == -1:
                break
            seg_type = text[i:j]
            i = j + 1
            # id
            j = text.find("|", i)
            if j == -1:
                break
            seg_id = text[i:j]
            i = j + 1
            # content (length characters)
            content = text[i : i + length]
            i += length
            # Trailing | (skip if present)
            if i < n and text[i] == "|":
                i += 1

            if seg_type == "updatePanel":
                out.update_panels[seg_id] = content.encode("utf-8")
            elif seg_type == "hiddenField":
                out.hidden_fields[seg_id] = content
            # Other types (scriptBlock, scriptStartupBlock, asyncPostBackControlIDs,
            # postBackControlIDs, updatePanelIDs, asyncPostBackTimeout, expando,
            # arrayDeclaration, etc.) are ignored — we don't need them to scrape.
        return out

    def panel_html(self, panel_id: str | None = None) -> bytes:
        """Return concatenated HTML across all updated panels (or one).

        With no argument, joins every ``update_panels`` entry — useful when
        we don't know which panel a grid lives in.
        """
        if panel_id is not None:
            return self.update_panels.get(panel_id, b"")
        return b"\n".join(self.update_panels.values())

    def to_form_state(self, prior: FormState) -> FormState:
        """Merge the delta's hidden-field updates into a fresh FormState.

        Reuses the prior form_fields (selects, etc.) and overlays the new
        __VIEWSTATE / __EVENTVALIDATION / __VIEWSTATEGENERATOR. The resulting
        state can drive a follow-up postback.
        """
        new_form = dict(prior.form_fields)
        for k, v in self.hidden_fields.items():
            new_form[k] = v
        return FormState(
            viewstate=self.hidden_fields.get("__VIEWSTATE", prior.viewstate),
            viewstate_generator=self.hidden_fields.get(
                "__VIEWSTATEGENERATOR", prior.viewstate_generator
            ),
            viewstate_encrypted=self.hidden_fields.get(
                "__VIEWSTATEENCRYPTED", prior.viewstate_encrypted
            ),
            event_validation=self.hidden_fields.get(
                "__EVENTVALIDATION", prior.event_validation
            ),
            last_focus=self.hidden_fields.get("__LASTFOCUS", prior.last_focus),
            form_fields=new_form,
            page_url=prior.page_url,
            # raw_html: synthesize from the panel HTML so parsers can run
            raw_html=self.panel_html(),
        )


# ---------------------------------------------------------------------------
# Helpers used by detection logic
# ---------------------------------------------------------------------------
_PAGER_DOPOSTBACK_RE = re.compile(r"__doPostBack\([^)]*Page\$\d+", re.IGNORECASE)
_PAGER_CLASS_RE = re.compile(r"\bpager\b", re.IGNORECASE)
_VIEWSTATE_PROBE_RE = re.compile(rb"name=\"__VIEWSTATE\"", re.IGNORECASE)
_FILENAME_RE = re.compile(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', re.IGNORECASE)

# Server-side NRE signatures: any of these in a 500 response body means
# the source ASP.NET handler crashed on null data. Don't retry — the
# server will keep crashing on the same record.
_NRE_SIGNATURES = (
    b"Object reference not set to an instance of an object",
    b"NullReferenceException",
    b"<title>Object reference not set",
)


def _is_server_side_unavailable(body: bytes) -> bool:
    """Return True if a 500 response body indicates an unhandled NRE on the
    source server (per-meeting or per-LB data corruption we cannot fix)."""
    if not body:
        return False
    return any(sig in body for sig in _NRE_SIGNATURES)


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
                    # Server-side unhandled NRE — non-retryable, the server
                    # will keep crashing on this record. Caller (artifacts
                    # task) catches this per-row and skips gracefully.
                    if _is_server_side_unavailable(response.content):
                        raise ServerSideUnavailableError(
                            f"HTTP {response.status_code} (server NRE) from {url}"
                        )
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

    def fetch_attachment_files(
        self, state: FormState, lnkfileview_target: str
    ) -> list[tuple[bytes, str]]:
        """Two-step attachment download.

        Step 1 posts the ``GrdDecision$ctlNN$lnkFileView`` target which causes
        the DR page to re-render with a populated ``GrdFileView`` table listing
        every file attached to that decision. Step 2 posts ``GrdFileView`` with
        ``Select$N`` per-file to receive the actual ``application/pdf`` bytes.

        Returns a list of ``(content_bytes, filename)`` tuples — one per file.
        Empty list if step 1 yields no file rows (decision had no attachment).
        ``state`` must be a FormState parsed from the current PublicDRegister
        page (``state.page_url`` ending in ``PublicDRegister.aspx``).
        """
        # Step 1: trigger lnkFileView; response is HTML with GrdFileView grid + new VIEWSTATE.
        data1 = self._build_postback_data(
            state, event_target=lnkfileview_target, event_argument=""
        )
        r1 = self._request("POST", state.page_url, data=data1)
        state1 = parse_form_state(r1.content, page_url=state.page_url)

        soup = BeautifulSoup(r1.content, "lxml", from_encoding="utf-8")
        grd = soup.find("table", id=re.compile(r"GrdFileView"))
        if grd is None:
            return []

        # Each <tr> with td cells corresponds to one file (Select$N).
        # Skip header rows (those without td or with only th).
        file_rows: list[tuple[int, str]] = []
        for tr in grd.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            filename = cells[0].get_text(strip=True) if cells else ""
            file_rows.append((len(file_rows), filename))

        results: list[tuple[bytes, str]] = []
        for select_idx, filename in file_rows:
            data2 = self._build_postback_data(
                state1,
                event_target="GrdFileView",
                event_argument=f"Select${select_idx}",
            )
            r2 = self._request("POST", state.page_url, data=data2)
            disp = r2.headers.get("Content-Disposition", "")
            disp_filename = ""
            if disp:
                m = _FILENAME_RE.search(disp)
                if m:
                    disp_filename = m.group(1).strip()
            results.append((r2.content, disp_filename or filename))

        return results

    # ------------------------------------------------------------------
    # MS-AJAX UpdatePanel async postback
    # ------------------------------------------------------------------
    def async_postback(
        self,
        state: FormState,
        event_target: str,
        event_argument: str = "",
        scriptmanager_id: str = "ctl00$ContentPlaceHolder1$ScriptManager1",
        update_panel_id: str = "ctl00$ContentPlaceHolder1$UpdatePanelDEstimate",
    ) -> "AsyncPostbackResponse":
        """Issue an MS-AJAX async postback and parse the pipe-delimited delta.

        Some controls on the SAKARMA dashboard live inside an UpdatePanel and
        only render their full content when invoked via async postback (with
        ``X-MicrosoftAjax: Delta=true`` and the ScriptManager hidden field).
        Verified for the cancellation sub-buttons (btncnlquarum, btnPublicH,
        btnOthersH) and for some KPI category drill-downs whose targets are
        wrapped in the panel.

        The response body is the MS-AJAX delta format — a sequence of
        ``<length>|<type>|<id>|<content>`` segments separated by ``|``. We
        parse it into a dict-like object exposing the updated panel HTML,
        new VIEWSTATE / EVENTVALIDATION, and any extra hidden fields.
        """
        # Build form data — same as a regular postback PLUS the ScriptManager
        # hidden field that names the panel + control.
        data = self._build_postback_data(
            state,
            event_target=event_target,
            event_argument=event_argument,
        )
        # ``<panel>|<control>`` tells the server which panel triggered the
        # postback so it can render only that panel's contents.
        data[scriptmanager_id] = f"{update_panel_id}|{event_target}"
        data["__ASYNCPOST"] = "true"

        headers = {
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Cache-Control": "no-cache",
        }
        response = self._request(
            "POST",
            state.page_url,
            data=data,
            headers=headers,
            # The delta response is text/plain not HTML; skip the
            # __VIEWSTATE-presence check (we'll find it inside the body).
            is_postback=False,
        )
        return AsyncPostbackResponse.from_delta(
            response.content, page_url=state.page_url
        )
