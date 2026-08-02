"""The LSGD portal's elected-member pages -- ward table and person detail.

The State Election Commission's trend site gives candidate-level votes but no
reservation status and no gender; the Local Self Government Department
publishes both, for *elected members only*, in plain server-rendered HTML with
no captcha. Two page shapes:

``electdmemberdet``
    One row per ward in a local body: ward number and name, the elected
    member's name, their role, party and the ward's reservation category.
    A local body whose election has not yet been published -- Pothencode
    Block Panchayat is a real, currently-published example -- renders the
    table's header row with **no data rows beneath it**. That is a valid,
    expected state -- distinct from a malformed page -- so it must parse to
    an empty list instead of raising or silently returning nothing.

``electdmemberpersondet``
    One member's personal detail: gender, age, marital status, education,
    occupation. Fields are read by their label, because the page renders
    label/value pairs as adjacent table cells with no named columns, and a
    field can legitimately be blank (age is exempt from the form's
    validation, for instance) -- a blank value counts as data, so it never
    signals a parse failure.

Address and phone number are deliberately skipped: both appear on the person
page as home contact details for roughly 22,000 individuals, and
constituency-level analysis has no use for them. There is no field for them
below, and none should be added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

RE_HEADER: Final = re.compile(
    r'href="/en/lbelection/(?:electlbrpt|electdistrict)/[^"]*"\s*>(.*?)</a>', re.S
)
RE_ROW: Final = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
RE_CELL: Final = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
RE_PERSON: Final = re.compile(r'href="(/en/lbelection/electdmemberpersondet/[^"]+)"')
RE_TAG: Final = re.compile(r"<[^>]+>")

_MIN_WARD_ROW_CELLS: Final = 6
"""Ward No. / Ward Name / Elected Member / Role / Party / Reservation."""

PERSON_FIELDS: Final[dict[str, str]] = {
    "age": "Age",
    "gender": "Female/Male",
    "marital_status": "Marital Status",
    "education": "Educational Qualification",
    "occupation": "Occupation",
}


def _text(html: str) -> str:
    return _clean(RE_TAG.sub("", html).replace("&nbsp;", " ").replace("&amp;", "&"))


def _clean(value: str) -> str:
    return value.strip()


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class WardMemberRow:
    """One ward's elected member, from an ``electdmemberdet`` page."""

    ward_no: int
    ward_name: str
    member_name: str
    role: str
    party: str
    reservation: str
    person_url: str
    """Empty when the row carries no link to a person page -- observed for a
    handful of rows where the portal never published the detail page."""


@dataclass(frozen=True, slots=True)
class MemberPage:
    """One local body's ward table."""

    district: str
    lb_name: str
    rows: tuple[WardMemberRow, ...]


def parse_member_page(html: str, lb_type: str) -> MemberPage:
    """Parse an ``electdmemberdet`` page into district, LB name and ward rows.

    An empty ``rows`` means the local body's result has not yet been
    published by the portal; the header row still renders regardless.
    """
    header = ""
    match = RE_HEADER.search(html)
    if match:
        header = _text(match.group(1))

    district, lb_name = "", header
    if " - " in header:
        # "Thiruvananthapuram - Chemmaruthy Grama Panchayat"
        district, lb_name = (part.strip() for part in header.split(" - ", 1))
    elif header.endswith(lb_type):
        # "Thiruvananthapuram District Panchayat" / "... Corporation": one per
        # district, so the local body carries the district's own name.
        district = header[: -len(lb_type)].strip()
        lb_name = district

    # Strip the trailing family label so lb_name matches the SEC's naming
    # ("Chemmaruthy Grama Panchayat" -> "Chemmaruthy").
    if lb_name.endswith(lb_type):
        stripped = lb_name[: -len(lb_type)].strip()
        if stripped:
            lb_name = stripped

    rows = []
    for row_html in RE_ROW.findall(html):
        cells = RE_CELL.findall(row_html)
        if len(cells) < _MIN_WARD_ROW_CELLS:
            continue
        values = [_text(cell) for cell in cells]
        if values[0].lower().startswith("ward no"):
            continue
        person = RE_PERSON.search(row_html)
        rows.append(
            WardMemberRow(
                ward_no=_to_int(values[0]),
                ward_name=values[1],
                member_name=values[2],
                role=values[3],
                party=values[4],
                reservation=values[5],
                person_url=person.group(1) if person else "",
            )
        )
    return MemberPage(district=district, lb_name=lb_name, rows=tuple(rows))


@dataclass(frozen=True, slots=True)
class PersonDetail:
    """One member's demographic detail, from an ``electdmemberpersondet`` page.

    Every field defaults to the empty string -- a page that is missing a
    label (or whose value the form left blank) still yields a complete
    record, never ``None`` and never a dropped row.
    """

    gender: str = ""
    age: str = ""
    marital_status: str = ""
    education: str = ""
    occupation: str = ""


def parse_person(html: str) -> PersonDetail:
    """Label-driven parse of a person page; missing fields are legitimate."""
    pairs: dict[str, str] = {}
    for row_html in RE_ROW.findall(html):
        cells = [_text(cell) for cell in RE_CELL.findall(row_html)]
        # Labels and values alternate across the row's cells; a row with an
        # odd cell out (a rowspan artefact) simply leaves its last label
        # unmatched, without raising.
        for i in range(0, len(cells) - 1, 2):
            label = cells[i].rstrip(":").strip()
            if label:
                pairs.setdefault(label, cells[i + 1])

    return PersonDetail(**{field: pairs.get(label, "") for field, label in PERSON_FIELDS.items()})
