"""The SEC's "Who Is Your Representative" pages -- 2025 elected-member detail.

2025's replacement for LSGD, which had not published the December 2025
election at the time these caches were captured. Two page shapes:

``wyrw`` (ward table)
    Lists every ward in one local body with its member and role, in
    **Malayalam only**. This is the only source for role (President / Vice
    President / Member); the member page below does not carry it.

``wyr/view`` (member detail)
    One member's full record, in English: district, local body, ward,
    reservation, front, party, name, age, gender, votes, margin, serving-from
    date. The page renders as label/value pairs with no stable DOM structure
    to key off, so parsing walks the flattened text and matches each label
    against the value immediately following it -- rejecting a "value" that is
    itself one of the page's own labels, which is how a blank field's next
    label ("Phone") was previously misread as that field's value.

Deliberately skipped: member phone, home address and photograph. They appear
on the page as personal contact details for roughly 23,600 individuals, and
constituency-level analysis has no use for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

RE_TAG: Final = re.compile(r"<[^>]+>")
RE_ROW: Final = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
RE_CELL: Final = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
RE_ROW_MEMBER: Final = re.compile(r"/public/wyr/view/(\d+)")
RE_WARD_LABEL: Final = re.compile(r"\s*(\d+)\s*-\s*(.*)")

_MIN_WARD_TABLE_CELLS: Final = 6

# label -> field, read off the member page's flattened text
FIELDS: Final[dict[str, str]] = {
    "District": "district_name",
    "LSGI": "lb_name",
    "Ward Name": "ward_label",
    "Ward Reservation": "reservation",
    "Age": "age",
    "Gender": "gender",
    "Votes Secured": "votes",
    "Margin": "margin",
    "Serving From": "serving_from",
}

# LSGI names carry the family as a suffix; the trend feed does not.
RE_LB_SUFFIX: Final = re.compile(
    r"\s+(Grama\s*Panchayath?|Block\s*Panchayath?|District\s*Panchayath?|"
    r"Municipal\s*Corporation|Munci?pality|Munci?pal|Corporation)\s*$",
    re.I,
)

# Reservation comes back as WOMEN / SCWOMEN / STWOMEN here; LSGD writes
# Woman / SC Woman / ST Woman. Harmonised so the years stack.
RES_MAP: Final[dict[str, str]] = {
    "WOMEN": "Woman",
    "SCWOMEN": "SC Woman",
    "STWOMEN": "ST Woman",
    "GENERAL": "General",
    "SC": "SC",
    "ST": "ST",
}

# Role lives only in the ward table, in Malayalam. Heads and deputies are
# what the schema needs; standing-committee posts collapse to "Member" in the
# English column but keep their original text in role_mal.
_ROLE_MAP: Final[tuple[tuple[str, str], ...]] = (
    ("വൈസ് പ്രസിഡന്റ്", "Vice President"),
    ("ഡെപ്യൂട്ടി ചെയർ പേഴ്സ", "Deputy Chairperson"),
    ("ഡെപ്യൂട്ടി മേയർ", "Deputy Mayor"),
    ("പ്രസിഡന്റ്", "President"),
    ("ചെയർ പേഴ്സ", "Chairperson"),
    ("മേയർ", "Mayor"),
)

_CHAIRMAN: Final = "ചെയർമാൻ"
_CHAIRMAN_SHORT: Final = "ചെയർമാ"


def _text(html: str) -> str:
    return _clean(RE_TAG.sub(" ", html).replace("&nbsp;", " ").replace("&amp;", "&"))


def _clean(value: str) -> str:
    return value.strip()


def _to_int(value: str, default: int = 0) -> int:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def role_english(role_mal: str) -> str:
    """Malayalam post -> English.

    Standing-committee chairs are a real, distinct post and must not collapse
    into "Member": 986 of them do so if the chair test is omitted, which silently
    demotes every committee chair in the state to an ordinary member.
    """
    text = (role_mal or "").strip()
    if not text:
        return "Member"
    for needle, english in _ROLE_MAP:
        if needle in text:
            return english
    # Matched loosely: the site renders the word with and without the final
    # chillu, and the truncated form appears in some ward tables.
    if _CHAIRMAN in text or _CHAIRMAN_SHORT in text:
        return "Standing Committee Chairman"
    return "Member"


def parse_ward_table(html: str) -> dict[str, str]:
    """One ``wyrw`` page: member id -> raw Malayalam role.

    A ward whose row has fewer than the expected six cells, or whose first
    cell is not a ward number, is not a data row (header, spacer), so it is
    skipped instead of raising -- table markup on this site is not uniform
    across local-body types.
    """
    roles: dict[str, str] = {}
    for row_html in RE_ROW.findall(html):
        cells = [_text(cell) for cell in RE_CELL.findall(row_html)]
        if len(cells) < _MIN_WARD_TABLE_CELLS or not re.match(r"^\d+$", cells[0] or ""):
            continue
        member = RE_ROW_MEMBER.search(row_html)
        if member:
            roles[member.group(1)] = cells[3]
    return roles


@dataclass(frozen=True, slots=True)
class WiyrMember:
    """One elected member, from a ``wyr/view`` page.

    ``role`` and ``role_mal`` are not set here -- they live only on the ward
    table, keyed by member id, and are the caller's job to join in.
    """

    district_name: str
    lb_type: str
    lb_name: str
    ward_no: int
    ward_name: str
    reservation: str
    party_front: str
    party_name: str
    member_name: str
    age: str
    gender: str
    votes: str
    margin: str
    serving_from: str


def parse_member(html: str, lb_type: str) -> WiyrMember:
    """Parse one ``wyr/view`` member page.

    The page has no stable DOM to key off, so this walks the flattened text
    and matches each known label against the value immediately following it.
    Where a field is blank, the next token is the *following* label, so a
    candidate value is rejected outright if it is itself one of the page's
    own labels -- otherwise a blank Gender reads "Phone" as its value.
    """
    flat = RE_TAG.sub("\x00", html)
    parts = [re.sub(r"\s+", " ", p).strip() for p in flat.split("\x00")]
    parts = [p for p in parts if p]

    not_values = set(FIELDS) | {
        "Phone",
        "Address",
        "Member Profile",
        "Ward & Location Details",
        "Name of Member",
    }
    record: dict[str, str] = {}
    for i, part in enumerate(parts):
        label = part.rstrip(":").strip()
        if label in FIELDS and i + 1 < len(parts):
            candidate = parts[i + 1].rstrip(":").strip()
            if candidate not in not_values:
                record.setdefault(FIELDS[label], candidate)

    # Front, party and member name sit between "Member Profile" and "Age:"
    # with no labels of their own -- positional, but confined to one block.
    party_front = party_name = member_name = ""
    block_match = re.search(r"Member Profile(.*?)Age", flat, re.S)
    if block_match:
        block = [re.sub(r"\s+", " ", p).strip() for p in block_match.group(1).split("\x00")]
        block = [p for p in block if p and p != "[PHOTO]"]
        if len(block) >= 3:
            party_front, party_name, member_name = block[:3]
        elif len(block) == 2:
            party_front, member_name = block

    ward_label = record.get("ward_label", "")
    ward_match = RE_WARD_LABEL.match(ward_label)
    reservation_raw = record.get("reservation", "")
    reservation_key = re.sub(r"[^A-Z]", "", reservation_raw.upper())

    return WiyrMember(
        district_name=record.get("district_name", ""),
        lb_type=lb_type,
        lb_name=RE_LB_SUFFIX.sub("", record.get("lb_name", "")).strip(),
        ward_no=_to_int(ward_match.group(1)) if ward_match else 0,
        ward_name=(ward_match.group(2).strip() if ward_match else ward_label),
        reservation=RES_MAP.get(reservation_key, reservation_raw),
        party_front=party_front,
        party_name=party_name,
        member_name=member_name,
        age=record.get("age", ""),
        gender=record.get("gender", ""),
        votes=record.get("votes", ""),
        margin=record.get("margin", ""),
        serving_from=record.get("serving_from", ""),
    )
