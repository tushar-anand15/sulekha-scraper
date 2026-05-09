"""BeautifulSoup parsers for SAKARMA portal page shapes.

Each public function accepts raw HTML bytes or a string and returns
typed structured data.  Callers should pass ``response.content`` (bytes)
directly; the internal ``_soup()`` helper handles encoding transparently.

All DOM identifiers and postback shapes were verified via live Playwright
probes against ``https://meeting.lsgkerala.gov.in``.

Lenient vs strict decisions
---------------------------
* ``parse_attachment_links`` is *lenient*: it includes any ``<a>`` whose
  ``id`` matches the ``GrdDecision_ctl\\d+_lnkFileView`` pattern regardless
  of visible text, because some rows were observed to have empty link text
  in live captures but still function correctly.
* ``parse_meeting_grid`` *silently skips* rows whose ``meeting_date`` cell
  is empty (logs a structlog warning).  Rows where the date cell is entirely
  absent (< 3 columns) are also skipped silently.
* ``parse_kpi_cards`` raises ``ParserError`` when *none* of the five KPI
  labels are found (completely wrong page shape); individual missing labels
  default to 0 with a warning so partial dashboards don't abort a run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Union

import structlog
from bs4 import BeautifulSoup

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------
_POSTBACK_RE = re.compile(
    r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)"
)
_SELECT_ARG_RE = re.compile(r"Select\$(\d+)")
_LNKFILEVIEW_ID_RE = re.compile(r"GrdDecision_ctl(\d+)_lnkFileView")
_LNKFILEVIEW_TARGET_RE = re.compile(r"GrdDecision\$ctl(\d+)\$lnkFileView")
_PAGER_DOPOSTBACK_RE = re.compile(r"__doPostBack\([^)]*Page\$\d+", re.IGNORECASE)
_PAGER_CLASS_RE = re.compile(r"\bpager\b", re.IGNORECASE)

# ---------------------------------------------------------------------------
# KPI label constants (Malayalam)
# These match the text labels rendered inside the KPI cards on LBWiseDashBoard.
# ---------------------------------------------------------------------------
KPI_LABEL_TOTAL = "ആകെ യോഗങ്ങൾ"
KPI_LABEL_ONGOING = "ചേരുന്ന യോഗങ്ങള്‍"
KPI_LABEL_MINUTES_COMPLETE = "മിനിറ്റ്സ് പൂര്‍ത്തിയായവ"
KPI_LABEL_MINUTES_INCOMPLETE = "മിനിറ്റ്സ് പൂര്‍ത്തിയാകാത്തവ"
KPI_LABEL_CANCELLED = "മീറ്റിംഗ് റദ്ദ്‌ ആക്കിയവ"


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------
class ParserError(ValueError):
    """Raised when an expected DOM element cannot be found on the page."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class KPISnapshot:
    """KPI summary numbers from the LBWise dashboard card section."""

    total: int
    ongoing: int
    minutes_complete: int
    minutes_incomplete: int
    cancelled: int


@dataclass
class ManifestRow:
    """One data row extracted from the ``GridMeetingDEtails`` table."""

    category: int
    """SMALLINT category constant from models (CATEGORY_APPROVED etc.)."""

    meeting_no_label: str
    """The displayed meeting number column text."""

    meeting_date: str
    """Raw "DD/MM/YYYY" string from the cell; caller parses to date."""

    meeting_type: str | None
    """Malayalam meeting type label, e.g. ``ഭരണസമിതി യോഗം``."""

    meeting_nature: str | None
    """Malayalam nature label, e.g. ``സാധാരണ യോഗം``."""

    meeting_venue: str | None
    """Venue string from the grid cell."""

    dashboard_grid_select_index: int | None
    """Row index N from the ``Select$N`` href; ``None`` when no Minutes link."""

    dr_postback_target: str | None
    """Full ``lnkDR`` EVENTTARGET id; ``None`` when no DR link present."""


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------
def _soup(html: Union[bytes, str]) -> BeautifulSoup:
    """Construct a BeautifulSoup from bytes (UTF-8) or str."""
    if isinstance(html, bytes):
        return BeautifulSoup(html, "lxml", from_encoding="utf-8")
    return BeautifulSoup(html, "lxml")


def _safe_int(text: str) -> int | None:
    """Convert stripped text to int; returns None on failure."""
    try:
        return int(text.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# parse_dropdown_options
# ---------------------------------------------------------------------------
def parse_dropdown_options(
    form_state_or_html: Union[bytes, str], ddl_name: str
) -> list[tuple[int, str]]:
    """Return ``[(value_int, text)]`` for each non-placeholder option in *ddl_name*.

    Placeholder options are identified by ``value="0"`` or text starting
    with ``"----"``.  The ``ddl_name`` should be the full ASP.NET postback
    name (e.g. ``DDL_DISTRICT`` from ``protocol.py``).

    Args:
        form_state_or_html: Raw HTML bytes or string of a dashboard page.
        ddl_name: The ``name`` attribute of the target ``<select>`` element.

    Returns:
        Ordered list of ``(int_value, label_text)`` tuples, placeholder
        excluded.
    """
    soup = _soup(form_state_or_html)
    # ASP.NET select elements use the long postback name as ``name`` attr.
    select = soup.find("select", attrs={"name": ddl_name})
    if select is None:
        # Fall back to id-based lookup (id uses underscores; name uses $).
        id_equiv = ddl_name.replace("$", "_")
        select = soup.find("select", attrs={"id": id_equiv})
    if select is None:
        logger.warning("Dropdown not found", ddl_name=ddl_name)
        return []

    results: list[tuple[int, str]] = []
    for option in select.find_all("option"):
        value_str = option.get("value", "").strip()
        text = option.get_text(strip=True)
        # Skip placeholder: value "0" or text starting with "----"
        if value_str == "0" or text.startswith("----"):
            continue
        val = _safe_int(value_str)
        if val is None:
            continue
        results.append((val, text))
    return results


# ---------------------------------------------------------------------------
# parse_kpi_cards
# ---------------------------------------------------------------------------
def parse_kpi_cards(html: Union[bytes, str]) -> KPISnapshot:
    """Extract the 5 KPI numbers from the LBWise dashboard card section.

    Each KPI card contains an ``<h3>`` with the count and a ``<p>`` (or
    similar block-level element) with the Malayalam label.  We locate each
    label text and then look for the nearest preceding ``<h3>`` sibling (or
    parent's preceding sibling) that contains a digit.

    Args:
        html: Raw HTML bytes or string of the LBWise dashboard page.

    Returns:
        :class:`KPISnapshot` with the five KPI integer values.

    Raises:
        :class:`ParserError`: If *no* KPI label is found anywhere on the page
            (almost certainly the wrong page shape).
    """
    soup = _soup(html)

    def _find_kpi(label: str) -> int:
        # Strategy 1: find any element whose text equals the label exactly,
        # then walk up to its card container and find the first h3 inside it.
        for elem in soup.find_all(string=lambda t: t and t.strip() == label):
            # Walk up: the container card is typically 2-3 levels up.
            card = elem.parent
            for _ in range(5):
                if card is None:
                    break
                h3 = card.find("h3")
                if h3:
                    val = _safe_int(h3.get_text(strip=True))
                    if val is not None:
                        return val
                card = card.parent

        # Strategy 2: regex text search (handles NBSPs / zero-width chars)
        all_text = soup.get_text(" ")
        if label in all_text:
            # Find all h3 elements and the closest one before the label
            h3_list = soup.find_all("h3")
            for h3 in reversed(h3_list):
                val = _safe_int(h3.get_text(strip=True))
                if val is not None:
                    return val

        logger.warning("KPI label not found, defaulting to 0", label=label)
        return 0

    _LABELS = (
        KPI_LABEL_TOTAL,
        KPI_LABEL_ONGOING,
        KPI_LABEL_MINUTES_COMPLETE,
        KPI_LABEL_MINUTES_INCOMPLETE,
        KPI_LABEL_CANCELLED,
    )
    found_any = any(
        soup.find(string=lambda t: t and t.strip() == lbl) for lbl in _LABELS
    )
    if not found_any:
        raise ParserError("No KPI labels found — is this the LBWise dashboard?")

    total = _find_kpi(KPI_LABEL_TOTAL)
    ongoing = _find_kpi(KPI_LABEL_ONGOING)
    minutes_complete = _find_kpi(KPI_LABEL_MINUTES_COMPLETE)
    minutes_incomplete = _find_kpi(KPI_LABEL_MINUTES_INCOMPLETE)
    cancelled = _find_kpi(KPI_LABEL_CANCELLED)

    return KPISnapshot(
        total=total,
        ongoing=ongoing,
        minutes_complete=minutes_complete,
        minutes_incomplete=minutes_incomplete,
        cancelled=cancelled,
    )


# ---------------------------------------------------------------------------
# parse_meeting_grid
# ---------------------------------------------------------------------------
def parse_meeting_grid(
    html: Union[bytes, str], category: int
) -> list[ManifestRow]:
    """Extract rows from the ``GridMeetingDEtails`` meeting list table.

    Column layout (0-based, inside each ``<tr>``):
      0 – serial no (display only; ignored)
      1 – meeting_no_label
      2 – meeting_date  (raw "DD/MM/YYYY")
      3 – meeting_type
      4 – meeting_nature
      5 – meeting_venue
      6 – DR link  → extract lnkDR EVENTTARGET
      7 – Minutes link → extract Select$N argument

    The header row (contains ``<th>`` cells) is skipped automatically.
    Rows with < 3 columns or an empty date cell are silently skipped.

    Args:
        html: Raw HTML bytes or string of the dashboard page after a KPI
            button postback.
        category: SMALLINT category constant (e.g. ``CATEGORY_APPROVED``).

    Returns:
        List of :class:`ManifestRow` objects.

    Raises:
        :class:`ParserError`: If the ``GridMeetingDEtails`` table is not
            present in the page.
    """
    soup = _soup(html)

    # The table id in the DOM uses underscores; find by id-suffix for robustness.
    grid = soup.find(
        "table",
        attrs={"id": lambda v: v and v.endswith("GridMeetingDEtails")},
    )
    if grid is None:
        raise ParserError(
            "GridMeetingDEtails table not found — is this a dashboard page after "
            "clicking a KPI button?"
        )

    rows: list[ManifestRow] = []
    table_row_index = 0  # zero-based data-row counter (excluding header)

    for tr in grid.find_all("tr"):
        # Skip header rows (contain <th> elements)
        if tr.find("th"):
            continue

        cells = tr.find_all("td")
        if len(cells) < 3:
            # Empty / footer rows
            continue

        # Extract cell text values
        def _cell(i: int) -> str:
            if i < len(cells):
                return cells[i].get_text(strip=True)
            return ""

        meeting_no_label = _cell(1)
        meeting_date = _cell(2)
        meeting_type = _cell(3) or None
        meeting_nature = _cell(4) or None
        meeting_venue = _cell(5) or None

        # Skip rows without a meeting date (empty / summary rows)
        if not meeting_date:
            logger.warning(
                "Skipping grid row: empty meeting_date",
                meeting_no_label=meeting_no_label,
                table_row_index=table_row_index,
            )
            continue

        # --- DR link (col 6) ---
        dr_postback_target: str | None = None
        if len(cells) > 6:
            dr_link = cells[6].find("a")
            if dr_link:
                match = _POSTBACK_RE.search(dr_link.get("href", ""))
                if match:
                    dr_postback_target = match.group("target")

        # --- Minutes / Select link (col 7) ---
        dashboard_grid_select_index: int | None = None
        if len(cells) > 7:
            min_link = cells[7].find("a")
            if min_link:
                href = min_link.get("href", "")
                m = _POSTBACK_RE.search(href)
                if m:
                    arg_match = _SELECT_ARG_RE.search(m.group("argument"))
                    if arg_match:
                        dashboard_grid_select_index = int(arg_match.group(1))

        rows.append(
            ManifestRow(
                category=category,
                meeting_no_label=meeting_no_label,
                meeting_date=meeting_date,
                meeting_type=meeting_type,
                meeting_nature=meeting_nature,
                meeting_venue=meeting_venue,
                dashboard_grid_select_index=dashboard_grid_select_index,
                dr_postback_target=dr_postback_target,
            )
        )
        table_row_index += 1

    logger.info(
        "Parsed meeting grid",
        category=category,
        row_count=len(rows),
    )
    return rows


# ---------------------------------------------------------------------------
# parse_attachment_links
# ---------------------------------------------------------------------------
def parse_attachment_links(
    html: Union[bytes, str],
) -> list[tuple[int, str]]:
    """Return attachment link info from a ``PublicDRegister`` page.

    Lenient policy: any ``<a>`` whose ``id`` matches the pattern
    ``GrdDecision_ctl{NN}_lnkFileView`` is included, regardless of its
    visible text.  This is intentional — live captures showed some rows
    with an empty link text that still performed a valid postback.

    Args:
        html: Raw HTML bytes or string of a PublicDRegister page.

    Returns:
        List of ``(decision_index, lnkFileView_target_id)`` tuples where
        ``decision_index`` is the raw integer captured from the element id
        (e.g. ``3`` for ``GrdDecision_ctl03_lnkFileView``) and
        ``lnkFileView_target_id`` is the postback target string in
        ``GrdDecision$ctl{NN}$lnkFileView`` form (as found in the href or
        derived from the id).
    """
    soup = _soup(html)
    results: list[tuple[int, str]] = []

    for a_tag in soup.find_all("a"):
        elem_id = a_tag.get("id", "")
        id_match = _LNKFILEVIEW_ID_RE.fullmatch(elem_id)
        if id_match is None:
            continue

        raw_index = int(id_match.group(1))

        # Try to extract target from href postback
        href = a_tag.get("href", "")
        pb_match = _POSTBACK_RE.search(href)
        if pb_match:
            target = pb_match.group("target")
        else:
            # Derive from the id (replace _ with $ in the GrdDecision portion)
            nn = id_match.group(1)  # zero-padded string e.g. "03"
            target = f"GrdDecision$ctl{nn}$lnkFileView"

        results.append((raw_index, target))

    logger.info("Parsed attachment links", count=len(results))
    return results


# ---------------------------------------------------------------------------
# detect_grid_pagination
# ---------------------------------------------------------------------------
def detect_grid_pagination(html: Union[bytes, str]) -> bool:
    """Return ``True`` when the meeting grid exposes pager controls.

    Mirrors the logic in :meth:`SakarmaClient._detect_pagination` so the
    function is publicly testable.  The client retains its own private copy;
    this module's version is the canonical public API.

    Two heuristics (either triggers True):
    1. Any ``__doPostBack(…, 'Page$\\d+')`` argument found anywhere in the
       page text — the cheapest check.
    2. The text contains ``"GridMeetingDEtails"`` AND a ``<tr>`` with
       ``class`` containing ``"pager"`` — the CSS-based fallback.

    Args:
        html: Raw HTML bytes or string.

    Returns:
        ``True`` if pagination is detected, ``False`` otherwise.
    """
    if isinstance(html, bytes):
        try:
            text = html.decode("utf-8", errors="replace")
        except Exception:
            return False
    else:
        text = html

    # Heuristic 1: Page$N postback argument anywhere on page
    if _PAGER_DOPOSTBACK_RE.search(text):
        return True

    # Heuristic 2: pager CSS class inside the grid
    if "GridMeetingDEtails" in text and _PAGER_CLASS_RE.search(text):
        return bool(
            re.search(r'<tr[^>]*class="[^"]*\bpager\b', text, re.IGNORECASE)
        )

    return False
