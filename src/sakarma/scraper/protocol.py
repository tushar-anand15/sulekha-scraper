"""SAKARMA portal protocol constants and form-state primitives.

The Kerala SAKARMA meeting portal is an ASP.NET WebForms application that spans
three pages tied together by session cookies and per-page ViewState. This module
captures the verified DOM identifiers (dropdowns, drill-down KPI buttons,
per-row grid postback targets) and a :class:`FormState` dataclass that
snapshots all hidden fields and current ``<select>`` selections from a parsed
response so the next postback can faithfully echo them back.

All identifiers below were verified via live Playwright probes against
``https://meeting.lsgkerala.gov.in``. The ``ctl00$ContentPlaceHolder1$`` prefix
is ASP.NET's master-page naming container; we keep the full names as
constants so call sites read like the on-page DOM.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Page paths (relative to ``settings.scraper_base_url``)
# ---------------------------------------------------------------------------
LBWISE_PATH = "/Pages/LBWiseDashBoard.aspx"
PUBLIC_MINUTES_PATH = "/Pages/PublicMinutes.aspx"
PUBLIC_DREGISTER_PATH = "/Pages/PublicDRegister.aspx"

# ---------------------------------------------------------------------------
# Form name prefix shared by every WebForms control on the dashboard
# ---------------------------------------------------------------------------
FORM_PREFIX = "ctl00$ContentPlaceHolder1$"

# ---------------------------------------------------------------------------
# Cascading dropdown DOM IDs / postback names
# ---------------------------------------------------------------------------
DDL_DISTRICT = "ctl00$ContentPlaceHolder1$ddlDistrict"
DDL_LB_TYPE = "ctl00$ContentPlaceHolder1$ddlLBType"
DDL_YEAR = "ctl00$ContentPlaceHolder1$ddlYear"
DDL_MAIN_GROUP = "ctl00$ContentPlaceHolder1$ddlMainGroup"
DDL_LB_NAME = "ctl00$ContentPlaceHolder1$ddlLBName"

# ---------------------------------------------------------------------------
# KPI drill-down buttons (each opens the GridMeetingDEtails grid for that bucket)
# ---------------------------------------------------------------------------
BTN_BEFORE_MEETINGS = "ctl00$ContentPlaceHolder1$btnBefore_Meetings"  # ongoing
BTN_APPV_MEETINGS = "ctl00$ContentPlaceHolder1$btnAppv_Meetings"  # approved
BTN_INCOMP_MEETINGS = "ctl00$ContentPlaceHolder1$btnInComp_Meetings"  # incomplete
BTN_CANCEL_DETAILS = "ctl00$ContentPlaceHolder1$btnCancelDetails"  # cancelled

# ---------------------------------------------------------------------------
# Grid postback fragments
# ---------------------------------------------------------------------------
GRID_MEETING_DETAILS = "ctl00$ContentPlaceHolder1$GridMeetingDEtails"


def grid_select_target() -> str:
    """Return the EVENTTARGET used for a ``Select$N`` row click."""
    return GRID_MEETING_DETAILS


def grid_select_argument(row_index: int) -> str:
    """``__EVENTARGUMENT`` value for selecting the Nth row."""
    return f"Select${row_index}"


def grid_dr_target(row_index: int) -> str:
    """EVENTTARGET for the per-row "DR" link.

    Verified shape: ``ctl{02 + row_index:02d}`` so row 0 -> ``ctl02``,
    row 1 -> ``ctl03``, etc.
    """
    return f"ctl00$ContentPlaceHolder1$GridMeetingDEtails$ctl{2 + row_index:02d}$lnkDR"


def decision_attachment_target(row_index: int) -> str:
    """EVENTTARGET for a per-decision attachment ``lnkFileView`` on PublicDRegister."""
    return f"GrdDecision$ctl{2 + row_index:02d}$lnkFileView"


# ---------------------------------------------------------------------------
# FormState — a verbatim snapshot of an ASP.NET WebForms page response
# ---------------------------------------------------------------------------
@dataclass
class FormState:
    """Snapshot of all hidden inputs + current select selections for one page."""

    viewstate: str = ""
    viewstate_generator: str = ""
    viewstate_encrypted: str = ""
    event_validation: str = ""
    last_focus: str = ""
    form_fields: dict[str, str] = field(default_factory=dict)
    page_url: str = ""
    raw_html: bytes = field(default_factory=bytes)


_HIDDEN_FIELDS = (
    "__VIEWSTATE",
    "__VIEWSTATEGENERATOR",
    "__VIEWSTATEENCRYPTED",
    "__EVENTVALIDATION",
    "__LASTFOCUS",
)


def parse_form_state(html_bytes: bytes, page_url: str) -> FormState:
    """Parse a SAKARMA WebForms response into a :class:`FormState`.

    Reads raw bytes (so the caller can hand off ``response.content`` directly)
    and decodes via ``from_encoding="utf-8"`` since the portal serves UTF-8.

    Args:
        html_bytes: Raw response body bytes.
        page_url: URL the response came from (used as the postback target for
            subsequent calls and stored on the returned state for traceability).

    Returns:
        Populated :class:`FormState`. ``form_fields`` includes every named
        ``<input>`` value plus the currently selected option of every
        ``<select>`` (defaulting to the first option's value if none are
        marked ``selected``). ``raw_html`` is set to ``html_bytes`` verbatim
        so callers can pass it directly to parsers without a separate fetch.
    """
    soup = BeautifulSoup(html_bytes, "lxml", from_encoding="utf-8")

    state = FormState(page_url=page_url, raw_html=html_bytes)

    # Hidden ASP.NET fields — searched globally so we tolerate either
    # form-id naming convention. Match by ``name`` (sent on POST) but fall
    # back to ``id`` for older markup.
    for hidden_name in _HIDDEN_FIELDS:
        elem = soup.find("input", attrs={"name": hidden_name}) or soup.find(
            "input", attrs={"id": hidden_name}
        )
        value = elem.get("value", "") if elem is not None else ""
        if hidden_name == "__VIEWSTATE":
            state.viewstate = value
        elif hidden_name == "__VIEWSTATEGENERATOR":
            state.viewstate_generator = value
        elif hidden_name == "__VIEWSTATEENCRYPTED":
            state.viewstate_encrypted = value
        elif hidden_name == "__EVENTVALIDATION":
            state.event_validation = value
        elif hidden_name == "__LASTFOCUS":
            state.last_focus = value

    # Snapshot every named <input> with a value (hidden + text fields only).
    # CRITICAL: skip submit/button/image/reset inputs — those are user-clickable
    # controls whose values must NOT be echoed into postback bodies. Including
    # e.g. ``btnMod_close=CLOSE`` would cause the server to interpret a generic
    # __doPostBack as "user clicked CLOSE", which dismisses drill-down views
    # and returns the bare dashboard instead of processing the intended event.
    _SUBMIT_TYPES = {"submit", "button", "image", "reset"}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        input_type = (inp.get("type") or "text").lower()
        if input_type in _SUBMIT_TYPES:
            continue
        state.form_fields[name] = inp.get("value", "")

    # Snapshot every <select>'s currently selected option (or first option).
    for sel in soup.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        selected = sel.find("option", selected=True)
        if selected is None:
            selected = sel.find("option")
        state.form_fields[name] = (
            selected.get("value", "") if selected is not None else ""
        )

    # Snapshot named <textarea> elements as well (rare on this portal but cheap).
    for ta in soup.find_all("textarea"):
        name = ta.get("name")
        if not name:
            continue
        state.form_fields[name] = ta.get_text() or ""

    return state
