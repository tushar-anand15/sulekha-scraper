"""The SEC's candidate reports, in both layouts and both modes.

These are the commission's own disclosures and the only source carrying gender
for *every* candidate -- winners and losers -- in every cycle. Two layouts::

    2010 / 2015   Dist  LBCode  LBName  WardCode  WardName  Name  Sex  Party  Votes
    2020 / 2025   Dist  LBCode  LBName  WardCode  WardName  Party  Name  Sex  Age  Address  Votes

and two modes:

**Patch mode** treats the report as a supplement, yielding
``(ward_code, votes) -> sex, age``. The trend site already supplies names,
parties and ward names for 2015, 2020 and 2025.

**Spine mode** treats the report as the sole candidate-level source and keeps
every published column. 2010 needs this: its trend site was decommissioned.

Both are fixed-width reports, so any value that fills its column runs into its
neighbour with no separating space. Four collisions occur, and each is handled:
skipping any of them is precisely how 638 wards went missing::

    district + lb_code           PATHANAMTHITTAB03023
    lb_name + ward_code          Chennam PallippuG04002001
    ward_name + "Invalid Vote"   Mancombu ThekkekkaraInvalid Vote
    candidate_name + sex         ...( THOTTATHM   INC   1938
    a row wrapped onto the next line

Address is deliberately skipped: it is a home address for every candidate in
Kerala, and constituency-level analysis has no use for it.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class Layout(Enum):
    """Which column order a report uses."""

    OLD = "old"
    """2010 and 2015: name, sex, party, votes."""

    NEW = "new"
    """2020 and 2025: party, name, sex, age, address, votes."""


# NOT \b...\b. A word boundary needs a non-word character before the G, so
# "Chennam PallippuG04002001" failed the guard and the row was dropped before
# any counter saw it -- 638 wards missing from 2015, found only by comparing
# against an unrelated source.
RE_WARD: Final = re.compile(r"(?<!\d)([GBDMC]\d{8})(?!\d)")
RE_LB: Final = re.compile(r"(?<!\d)([GBDMC]\d{5})(?!\d)")

# The invalid-votes pseudo-row. Requiring whitespace before the label made
# collided lines fall through to the candidate patterns, which matched them and
# emitted a FABRICATED candidate with a gender, a vote count and a party of
# "WARDInvalid Vote". Hence \s* on both joins.
RE_INVALID: Final = re.compile(r"Invalid\s*Votes?", re.I)

# Belt and braces: whatever the layout, a line mentioning the label is never a
# candidate. Without this a future truncation ("Invald Vote") would silently
# reintroduce fabricated rows.
RE_INVALID_ANY: Final = re.compile(r"Inva[lst]?[il]?d?\s*Votes?", re.I)

RE_PAGE: Final = re.compile(r"^\s*page\s+\d+\s*$", re.I)
RE_TRAIL_INT: Final = re.compile(r"(\d+)\s*$")
SPLIT: Final = re.compile(r"\s{2,}")

_HEADER_PREFIXES: Final = ("page ", "district", "distname", "dist ")

# Patch-mode cascades, most specific first. Long names get truncated and glue
# onto the sex column, some 2020 rows carry no age, and some party names contain
# spaces ("JSS( R )").
RE_NEW: Final = (
    re.compile(r"(?<!\d)([GBDMC]\d{8})(?!\d)\s+(.+?)\s+([MFT])\s+(\d{1,3})\s+(.*?)(\d+)\s*$"),
    re.compile(r"(?<!\d)([GBDMC]\d{8})(?!\d)\s+(.+?)\s+([MFT])\s+()(.*?)(\d+)\s*$"),
    re.compile(r"(?<!\d)([GBDMC]\d{8})(?!\d)\s+(.+?)([MFT])\s+(\d{1,3})\s+(.*?)(\d+)\s*$"),
    re.compile(r"(?<!\d)([GBDMC]\d{8})(?!\d)\s+(.+?)([MFT])\s+()(.*?)(\d+)\s*$"),
)
RE_OLD: Final = (
    re.compile(r"(?<!\d)([GBDMC]\d{8})(?!\d)\s+(.+?)\s+([MF])\s+(.+?)\s+(\d+)\s*$"),
    re.compile(r"(?<!\d)([GBDMC]\d{8})(?!\d)\s+(.+?)([MF])\s+(.+?)\s+(\d+)\s*$"),
)

# The report's district column is fixed-width and truncates
# ("THIRUVANANTHAP"), so district is taken from the local-body code instead.
# The raw string is kept alongside so the substitution stays auditable.
DISTRICTS: Final[dict[str, str]] = {
    "01": "THIRUVANANTHAPURAM",
    "02": "KOLLAM",
    "03": "PATHANAMTHITTA",
    "04": "ALAPPUZHA",
    "05": "KOTTAYAM",
    "06": "IDUKKI",
    "07": "ERNAKULAM",
    "08": "THRISSUR",
    "09": "PALAKKAD",
    "10": "MALAPPURAM",
    "11": "KOZHIKKODE",
    "12": "WAYANAD",
    "13": "KANNUR",
    "14": "KASARGOD",
}

LB_FAMILY: Final[dict[str, str]] = {
    "G": "Grama Panchayat",
    "B": "Block Panchayat",
    "D": "District Panchayat",
    "M": "Municipality",
    "C": "Corporation",
}

_MIN_PARTY_SIGHTINGS: Final = 3
"""A label seen once or twice is far likelier to be debris than a party."""

_MIN_INFERRED_WARD_NAME: Final = 3
"""A prefix this short is more likely coincidence than a genuine ward name."""


@dataclass(frozen=True, slots=True)
class PatchRow:
    """Patch mode: what the report adds to an already-known candidate."""

    ward_code: str
    votes: int
    sex: str
    age: str
    party_pdf: str
    name_raw: str
    parse_flag: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, int]:
        """The join key back onto the trend-site data."""
        return (self.ward_code, self.votes)


@dataclass(frozen=True, slots=True)
class SpineRow:
    """Spine mode: every published column, for a candidate or an invalid-vote row."""

    district_code: str
    district_name: str
    district_name_pdf: str
    lb_type: str
    lb_code: str
    lb_name: str
    ward_code: str
    ward_no: int
    ward_name: str
    candidate_name: str
    sex: str
    party_pdf: str
    votes: int
    row_type: str
    parse_flag: tuple[str, ...] = ()


@dataclass
class ParseReport:
    """What the parse did, including everything it could not do.

    Every repair and every rejection is counted. The defects this parser fixes
    both hid because a rejected line reached no counter at all.
    """

    candidate_rows: int = 0
    invalid_rows: int = 0
    joined_lines: int = 0
    inferred_ward_names: int = 0
    unparsed: list[str] = field(default_factory=list)
    invalid_unparsed: list[str] = field(default_factory=list)
    flags: Counter[str] = field(default_factory=Counter)
    duplicate_keys: int = 0
    duplicate_key_examples: list[tuple[str, int]] = field(default_factory=list)
    party_vocabulary: frozenset[str] = frozenset()
    wards: int = 0
    local_bodies: int = 0

    def summary(self) -> str:
        return (
            f"{self.candidate_rows:,} candidates, {self.invalid_rows:,} invalid-vote rows, "
            f"{self.wards:,} wards, {self.local_bodies:,} local bodies, "
            f"{len(self.unparsed)} unparsed"
        )


# ---------------------------------------------------------------------------
# Patch mode
# ---------------------------------------------------------------------------


def parse_patch(lines: Iterable[str], layout: Layout) -> tuple[list[PatchRow], ParseReport]:
    """Read a report as a supplement keyed by ``(ward_code, votes)``.

    Wrapped rows are rejoined first. This is not optional cleanup: how a text
    extractor breaks a long line varies by version, and on the 2025 report a
    newer ``pypdf`` splits 480 Malayalam candidate names onto a second line
    that the older one kept whole. Without the join those 480 candidates simply
    vanish from the parse -- which is the same class of silent loss this
    parser exists to prevent.
    """
    report = ParseReport()
    rows: list[PatchRow] = []
    patterns = RE_NEW if layout is Layout.NEW else RE_OLD

    joined = _join_wrapped([str(line) for line in lines])
    report.joined_lines = joined.count

    for raw in joined.lines:
        line = raw.strip()
        if not line or not RE_WARD.search(line):
            continue
        if line.lower().startswith(_HEADER_PREFIXES):
            continue

        if RE_INVALID.search(line):
            report.invalid_rows += 1
            continue
        if RE_INVALID_ANY.search(line):
            # An invalid-votes line the strict pattern cannot read. Report it,
            # but never let it reach the candidate rules.
            report.invalid_unparsed.append(line)
            continue

        row = _match_patch(line, patterns, layout)
        if row is None:
            report.unparsed.append(line)
            continue
        rows.append(row)

    report.candidate_rows = len(rows)
    report.wards = len({r.ward_code for r in rows})
    report.local_bodies = len({r.ward_code[:6] for r in rows})
    _count_duplicate_keys(rows, report)
    return rows, report


def _match_patch(
    line: str, patterns: tuple[re.Pattern[str], ...], layout: Layout
) -> PatchRow | None:
    for pattern in patterns:
        match = pattern.search(line)
        if match is None:
            continue
        if layout is Layout.NEW:
            ward, middle, sex, age, _address, votes = match.groups()
            return PatchRow(
                ward_code=ward,
                votes=int(votes),
                sex=sex,
                age=age or "",
                party_pdf="",
                name_raw=middle.strip(),
            )
        ward, middle, sex, party, votes = match.groups()
        return PatchRow(
            ward_code=ward,
            votes=int(votes),
            sex=sex,
            age="",
            party_pdf=party.strip(),
            name_raw=middle.strip(),
        )
    return None


def _count_duplicate_keys(rows: list[PatchRow], report: ParseReport) -> None:
    """Flag duplicate join keys so nobody has to guess which row is which.

    Two candidates in one ward can poll identical votes. Collapsing them would
    silently lose a candidate; the pair is reported instead.
    """
    counts = Counter(row.key for row in rows)
    duplicated = [key for key, n in counts.items() if n > 1]
    report.duplicate_keys = len(duplicated)
    report.duplicate_key_examples = sorted(duplicated)[:20]


# ---------------------------------------------------------------------------
# Spine mode
# ---------------------------------------------------------------------------


def parse_spine(lines: Iterable[str]) -> tuple[list[SpineRow], ParseReport]:
    """Read a report as the sole candidate-level source, keeping every column.

    Two passes. Pass 1 reads only rows that split cleanly and learns two
    vocabularies from them -- the set of party labels, and each ward's name.
    Pass 2 re-reads every line using those vocabularies as anchors, which
    recovers the collided rows without guessing.

    **Feed this ``pdftotext -layout`` output -- ``pypdf`` won't work here.**
    Pass 1 separates columns on runs of two or more spaces, and only the layout
    extractor preserves the report's column geometry. Measured on the 2010
    report: ``pdftotext`` leaves 1 line unparsed, ``pypdf`` leaves 69,057. The
    failure shows up -- unread lines land in ``report.unparsed`` -- but it is
    still a failure, so callers should not substitute engines here.

    ``pypdf`` remains a useful cross-check on the *ward set*: invalid-vote rows
    survive both extractions, and on the 2010 report both engines recover
    exactly the same 21,648 wards.
    """
    report = ParseReport()
    joined = _join_wrapped(list(lines))
    report.joined_lines = joined.count

    party_vocab, ward_names, inferred = _learn_vocabularies(joined.lines)
    report.party_vocabulary = party_vocab
    report.inferred_ward_names = inferred

    rows: list[SpineRow] = []
    for line in joined.lines:
        head = _split_head(line)
        if head is None:
            if RE_WARD.search(line) and not _is_noise(line):
                report.unparsed.append(line)
            continue
        parsed = _parse_rest(head.rest, head.ward_code, party_vocab, ward_names)
        if parsed is None:
            report.unparsed.append(line)
            continue
        rows.append(_build_spine_row(head, parsed))

    report.candidate_rows = sum(1 for r in rows if r.row_type == "candidate")
    report.invalid_rows = sum(1 for r in rows if r.row_type == "invalid")
    report.wards = len({r.ward_code for r in rows})
    report.local_bodies = len({r.lb_code for r in rows})
    report.flags = Counter(flag for row in rows for flag in row.parse_flag)
    return rows, report


@dataclass(frozen=True, slots=True)
class _Head:
    """The fixed part of a line: everything up to and including the ward code."""

    district_pdf: str
    lb_code: str
    lb_name: str
    ward_code: str
    rest: str


@dataclass(frozen=True, slots=True)
class _Joined:
    lines: list[str]
    count: int


def _is_noise(line: str) -> bool:
    stripped = line.strip().lower()
    return not stripped or bool(RE_PAGE.match(stripped)) or stripped.startswith(_HEADER_PREFIXES)


_MAX_WRAP_CONTINUATIONS: Final = 3
"""A row wrapping more than three times is more likely debris than a wrapped record."""


def _join_wrapped(lines: list[str]) -> _Joined:
    """Rejoin rows too wide for the page.

    The head carries the ward code but no trailing vote count; each
    continuation carries no ward code of its own. A row can wrap more than
    once -- in ward G05049001 a long candidate name pushes the sex, party and
    vote count onto a third physical line -- so continuations are absorbed
    until the vote count appears, however many lines that takes.

    Absorption stops at a line bearing its own ward code: that is the next
    record, and swallowing it would destroy two rows instead of saving one.
    """
    out: list[str] = []
    joined = 0
    index = 0
    while index < len(lines):
        line = lines[index].rstrip()
        if not line.strip() or RE_PAGE.match(line):
            index += 1
            continue

        if RE_WARD.search(line) and not RE_TRAIL_INT.search(line):
            merged = line
            lookahead = index + 1
            absorbed = 0
            while absorbed < _MAX_WRAP_CONTINUATIONS and lookahead < len(lines):
                tail = lines[lookahead].rstrip()
                if not tail.strip() or RE_PAGE.match(tail):
                    lookahead += 1
                    continue
                if RE_WARD.search(tail):
                    break
                merged = f"{merged}  {tail.strip()}"
                absorbed += 1
                lookahead += 1
                if RE_TRAIL_INT.search(merged):
                    break
            if absorbed and RE_TRAIL_INT.search(merged):
                out.append(merged)
                joined += 1
                index = lookahead
                continue

        out.append(line)
        index += 1
    return _Joined(lines=out, count=joined)


def _split_head(line: str) -> _Head | None:
    """Anchor on the two codes' positions; whitespace can't be trusted here.

    Local-body names run into the ward code with a single space
    ("Mavelikkara Thekkekkara G04055001") or none at all, and long district
    names glue straight onto the lb code -- column splitting loses those rows.
    """
    ward = RE_WARD.search(line)
    if ward is None:
        return None
    lb = RE_LB.search(line, 0, ward.start())
    if lb is None:
        return None
    return _Head(
        district_pdf=line[: lb.start()].strip(),
        lb_code=lb.group(1),
        lb_name=line[lb.end() : ward.start()].strip(),
        ward_code=ward.group(1),
        # Indentation after the ward code varies line to line, which would
        # otherwise poison both the prefix inference and the startswith() test
        # that separates ward name from candidate name.
        rest=line[ward.end() :].strip(),
    )


def _learn_vocabularies(lines: list[str]) -> tuple[frozenset[str], dict[str, str], int]:
    """Pass 1: learn party labels and ward names from unambiguous rows only.

    Ward names are recovered two ways. Rows that split cleanly state the name
    outright. For a ward whose every row is collided, the name is instead the
    longest prefix common to all its rows -- the ward name repeats across a
    ward's rows and the candidate names following it do not.
    """
    parties: Counter[str] = Counter()
    stated: defaultdict[str, Counter[str]] = defaultdict(Counter)
    bodies: defaultdict[str, list[str]] = defaultdict(list)

    for line in lines:
        head = _split_head(line)
        if head is None:
            continue
        parts = [p for p in SPLIT.split(head.rest.strip()) if p]
        if len(parts) == 5 and parts[2] in ("M", "F"):
            parties[parts[3]] += 1
            stated[head.ward_code][parts[0]] += 1
        elif len(parts) == 3 and RE_INVALID.fullmatch(parts[1].strip()):
            stated[head.ward_code][parts[0]] += 1

        trailing = RE_TRAIL_INT.search(head.rest)
        if trailing:
            # An invalid-vote row states the ward name and nothing else, so it
            # is the single most reliable contributor to the shared prefix.
            bodies[head.ward_code].append(head.rest[: trailing.start()].strip())

    names = {ward: counter.most_common(1)[0][0] for ward, counter in stated.items()}
    inferred = 0
    for ward, ward_bodies in bodies.items():
        if ward in names or len(ward_bodies) < 2:
            continue
        prefix = _common_prefix(ward_bodies)
        if len(prefix) >= _MIN_INFERRED_WARD_NAME:
            names[ward] = prefix
            inferred += 1

    vocabulary = frozenset(p for p, n in parties.items() if n >= _MIN_PARTY_SIGHTINGS)
    return vocabulary, names, inferred


def _common_prefix(strings: list[str]) -> str:
    """Longest prefix shared by every string, trimmed of trailing space."""
    if not strings:
        return ""
    prefix = strings[0]
    for candidate in strings[1:]:
        limit = 0
        while limit < len(prefix) and limit < len(candidate) and prefix[limit] == candidate[limit]:
            limit += 1
        prefix = prefix[:limit]
        if not prefix:
            return ""
    return prefix.rstrip()


@dataclass(frozen=True, slots=True)
class _Rest:
    ward_name: str
    candidate_name: str
    sex: str
    party_pdf: str
    votes: int
    row_type: str
    flags: tuple[str, ...]


def _parse_rest(
    rest: str, ward: str, party_vocab: frozenset[str], ward_names: dict[str, str]
) -> _Rest | None:
    """Pass 2: recover the variable columns, tolerating collisions."""
    trailing = RE_TRAIL_INT.search(rest)
    if not trailing:
        return None
    votes = int(trailing.group(1))
    body = rest[: trailing.start()].strip()
    known_ward = ward_names.get(ward, "")
    flags: list[str] = []

    invalid = RE_INVALID.search(body)
    if invalid:
        stated = body[: invalid.start()]
        if stated and not stated.endswith(" "):
            flags.append("wardname_glued_invalid")
        return _Rest(
            ward_name=stated.strip() or known_ward,
            candidate_name="",
            sex="",
            party_pdf="",
            votes=votes,
            row_type="invalid",
            flags=tuple(flags),
        )

    party, body = _take_party(body, party_vocab, flags)
    if party is None:
        return None
    sex, body = _take_sex(body, flags)
    ward_name, candidate_name = _split_names(body, known_ward, flags)

    return _Rest(
        ward_name=ward_name,
        candidate_name=candidate_name,
        sex=sex,
        party_pdf=party,
        votes=votes,
        row_type="candidate",
        flags=tuple(flags),
    )


def _take_party(body: str, vocabulary: frozenset[str], flags: list[str]) -> tuple[str | None, str]:
    """Prefer a known label at the end of the body over positional guessing."""
    for label in sorted(vocabulary, key=len, reverse=True):
        if body.endswith(label) and (
            len(body) == len(label) or not body[-len(label) - 1].isalnum()
        ):
            return label, body[: -len(label)].rstrip()

    tokens = SPLIT.split(body.strip())
    if len(tokens) >= 2:
        flags.append("party_unknown")
        return tokens[-1].strip(), body.rstrip()[: -len(tokens[-1])].rstrip()
    return None, body


def _take_sex(body: str, flags: list[str]) -> tuple[str, str]:
    """A trailing lone M/F, possibly glued to a truncated name."""
    separated = re.search(r"(?:\s|^)([MF])$", body)
    if separated:
        return separated.group(1), body[: separated.start()].rstrip()
    if body.endswith(("M", "F")):
        flags.append("sex_glued_name")
        return body[-1], body[:-1].rstrip()
    return "", body


def _split_names(body: str, known_ward: str, flags: list[str]) -> tuple[str, str]:
    """Separate the ward name from the candidate name."""
    if known_ward and body.startswith(known_ward):
        candidate = body[len(known_ward) :].strip()
        if not candidate:
            flags.append("no_candidate_name")
        return known_ward, candidate

    tokens = SPLIT.split(body.strip())
    if len(tokens) >= 2:
        ward_name = tokens[0].strip()
        if known_ward and ward_name != known_ward:
            flags.append("wardname_mismatch")
        return ward_name, " ".join(token.strip() for token in tokens[1:])

    flags.append("wardname_assumed")
    return known_ward, body.strip()


def _build_spine_row(head: _Head, rest: _Rest) -> SpineRow:
    district_digits = head.lb_code[1:3]
    return SpineRow(
        district_code=f"D{district_digits}001",
        district_name=DISTRICTS.get(district_digits, head.district_pdf),
        district_name_pdf=head.district_pdf,
        lb_type=LB_FAMILY.get(head.lb_code[0], ""),
        lb_code=head.lb_code,
        lb_name=head.lb_name,
        ward_code=head.ward_code,
        ward_no=int(head.ward_code[6:]),
        ward_name=rest.ward_name,
        candidate_name=rest.candidate_name,
        sex=rest.sex,
        party_pdf=rest.party_pdf,
        votes=rest.votes,
        row_type=rest.row_type,
        parse_flag=rest.flags,
    )
