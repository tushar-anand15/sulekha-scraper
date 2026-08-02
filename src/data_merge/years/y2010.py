"""The 2010 builder -- the one cycle whose architecture inverts.

Every other cycle's spine is the SEC trend site; 2010's was decommissioned, so
the SEC's own candidate PDF is the sole candidate-level source. It supplies
every vote, party label, ward name and local-body name. The LSGD portal
supplies what the PDF cannot: reservation category, the elected member's role,
their own English name, and (for the winner only) their own recorded gender.

This module does not share ``years/base.py`` with the SEC-spine years -- there
is no SEC-spine assembly to share. What it does share, heavily, is
``transform``: the pairing cascade and rejection gate that verify a local-body
match, the winner/tie derivation, gender orientation and precedence, and the
control-type rollup are all the same year-agnostic code the other cycles use.
Only the plumbing that is genuinely 2010-specific lives here:

* discovering which LSGD ``electdmemberdet`` page belongs to which local-body
  type, which requires walking the portal's own type/district index pages
  (cached, so this walk touches no network) -- no other cycle needs this,
  because the SEC feed enumerates local bodies directly;
* the honorific prefixed to a 2010 candidate's name (``Adv``, ``Shri``, ...),
  which the SEC embeds in the name string itself, unlike later cycles;
  a few 2010-only party-label aliases (``IND(LDF)`` -> ``INDEPENDENT``,
  ``KRSP(B)`` -> ``RSP(B)``) that the shared party module deliberately does
  not carry, because they are not shared with any other cycle;
* the front table itself, which is authored evidence
  (``data/reference/party_front_2010.csv``), not a published feed.

``build`` is pure: it takes already-parsed collections and returns new ones.
The one function here that performs I/O, :func:`load_lsgd_members`, takes an
already-open :class:`~data_merge.sources.cache.ResponseCache` and returns
plain records -- it is the 2010-specific analogue of a ``parsers`` module,
housed here because the LSGD *site walk* (as opposed to parsing one page) has
no other owner.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from data_merge.config import Paths
from data_merge.parsers.lsgd import parse_member_page, parse_person
from data_merge.parsers.pdf_candidates import SpineRow, parse_spine
from data_merge.schema import CandidateRow, conform
from data_merge.sources.cache import ResponseCache
from data_merge.sources.pdf import extract
from data_merge.spec import YearSpec, spec_for
from data_merge.transform import gender, party, rollup, winner
from data_merge.transform.matching import (
    LBKey,
    WardTally,
    apply_gate,
    names_agree,
    normalize,
    pair_local_bodies,
    wardnames_agree,
)
from data_merge.transform.party import FrontEntry, FrontResolution
from data_merge.transform.winner import Candidate as WinnerCandidate

YEAR: Final = 2010

PARTY_GROUP_SOURCE: Final = "mapped_2010"
"""Stamped on every row. 2010 has no published front, so this value -- never
``published`` -- is the whole story of where the front assignment came from."""

# ---------------------------------------------------------------------------
# LSGD site walk -- 2010-specific, because the SEC feed that enumerates local
# bodies for every other cycle does not exist for 2010.
# ---------------------------------------------------------------------------

_BASE_URL: Final = "https://lsgkerala.gov.in"
_LSGD_YEAR: Final = "2010"

LSGD_TYPE: Final[dict[int, str]] = {
    1: "District Panchayat",
    2: "Block Panchayat",
    3: "Municipality",
    4: "Corporation",
    5: "Grama Panchayat",
}
_DIRECT_TYPES: Final[frozenset[int]] = frozenset({1, 4})
"""District Panchayat and Corporation index pages link straight to member
pages; the other three types link to a per-district list first."""

_RE_MEMBER_LINK: Final = re.compile(
    rf'href="(/en/lbelection/electdmemberdet/{_LSGD_YEAR}/(\d+))"'
)
_RE_LBRPT_LINK: Final = re.compile(
    rf'href="(/en/lbelection/electlbrpt/\d+/(\d+)/{_LSGD_YEAR})"'
)

HEAD_ROLES: Final[frozenset[str]] = frozenset({"President", "Chairperson", "Chairman", "Mayor"})

# Honorifics the 2010 SEC report embeds directly in the candidate name --
# unlike the ward/party/vote columns, no later cycle repeats this shape, so it
# stays local rather than joining matching.py's comparison-only stripping.
_RE_TITLE: Final = re.compile(r"^\s*(ADV|DR|PROF|SHRI|SMT|KUM|MR|MRS|MS)[.\s]", re.I)
# No _TITLE_GENDER table: see the gender resolution below for why an
# honorific carries no weight as a record of sex.

# LSGD renders a handful of district names differently from the PDF's own
# canonical spelling (itself taken from the local-body code, not the report's
# truncated column). Comparison-only, like matching.py's own equivalences.
_DISTRICT_ALIASES: Final[dict[str, str]] = {
    "COCHIN": "ERNAKULAM",
    "KASARAGOD": "KASARGOD",
    "KASARGODE": "KASARGOD",
    "KOZHIKODE": "KOZHIKKODE",
    "TRIVANDRUM": "THIRUVANANTHAPURAM",
}

# Abbreviations the PDF uses that the shared party module's equivalence table
# does not carry, because no other cycle shares them: 2010 alone qualifies
# independent candidates by the front they lean towards, and spells the RSP
# breakaway faction differently. Comparison-only -- a candidate row's own
# ``party_name`` always keeps the front table's spelling; this alias exists
# only for matching.
_PARTY_ALIAS_2010: Final[dict[str, str]] = {
    "IND(LDF)": "INDEPENDENT",
    "IND(UDF)": "INDEPENDENT",
    "IND(BJP)": "INDEPENDENT",
    "KRSP(B)": "RSP(B)",
}


def _canon_district(name: str) -> str:
    key = normalize(name)
    return _DISTRICT_ALIASES.get(key, key)


def _party_alias(label: str) -> str:
    return _PARTY_ALIAS_2010.get(label.strip(), label)


def _split_title(name: str) -> tuple[str, str]:
    """('Adv', 'REST') -- the SEC prefixes the honorific to the name in 2010."""
    match = _RE_TITLE.match(name or "")
    if not match:
        return "", (name or "").strip()
    return match.group(1).title(), (name or "")[match.end() :].strip()


@dataclass(frozen=True, slots=True)
class LsgdMemberRow:
    """One elected member, from the ward table joined to their person page."""

    district: str
    lb_type: str
    lb_name: str
    ward_no: int
    ward_name: str
    member_name: str
    role: str
    party: str
    reservation: str
    gender: str
    age: str
    education: str
    occupation: str


def _get_html(cache: ResponseCache, url: str) -> str | None:
    value = cache.get(f"GET|{url}")
    return value if isinstance(value, str) else None


def _discover_local_bodies(cache: ResponseCache) -> list[tuple[int, str]]:
    """Walk the type/district index pages down to (type, lbid) pairs.

    Mirrors the crawl ``scrape_lsgd.py`` performed when it built this cache --
    the index pages it walked are themselves cached, so replaying the walk
    needs no network, only the same traversal.
    """
    found: list[tuple[int, str]] = []
    for lb_type_id in sorted(LSGD_TYPE):
        index_html = _get_html(
            cache, f"{_BASE_URL}/en/lbelection/electdistrict/{_LSGD_YEAR}/{lb_type_id}"
        )
        if index_html is None:
            continue
        if lb_type_id in _DIRECT_TYPES:
            for _href, lbid in dict.fromkeys(_RE_MEMBER_LINK.findall(index_html)):
                found.append((lb_type_id, lbid))
            continue
        district_links = dict.fromkeys(_RE_LBRPT_LINK.findall(index_html))
        for path, _district_id in district_links:
            district_html = _get_html(cache, f"{_BASE_URL}{path}")
            if district_html is None:
                continue
            for _href, lbid in dict.fromkeys(_RE_MEMBER_LINK.findall(district_html)):
                found.append((lb_type_id, lbid))

    seen: set[str] = set()
    ordered: list[tuple[int, str]] = []
    for lb_type_id, lbid in found:
        if lbid not in seen:
            seen.add(lbid)
            ordered.append((lb_type_id, lbid))
    return ordered


def load_lsgd_members(cache: ResponseCache) -> tuple[LsgdMemberRow, ...]:
    """Every elected member the 2010 LSGD cache holds, joined to their person page.

    The only I/O in this module: ``cache`` is already open, so this reads
    cached responses, never the filesystem or the network directly.
    """
    rows: list[LsgdMemberRow] = []
    for lb_type_id, lbid in _discover_local_bodies(cache):
        page_url = f"{_BASE_URL}/en/lbelection/electdmemberdet/{_LSGD_YEAR}/{lbid}"
        page_html = _get_html(cache, page_url)
        if page_html is None:
            continue
        page = parse_member_page(page_html, LSGD_TYPE[lb_type_id])
        for ward_row in page.rows:
            person = parse_person("")
            if ward_row.person_url:
                person_html = _get_html(cache, f"{_BASE_URL}{ward_row.person_url}")
                if person_html is not None:
                    person = parse_person(person_html)
            rows.append(
                LsgdMemberRow(
                    district=page.district,
                    lb_type=LSGD_TYPE[lb_type_id],
                    lb_name=page.lb_name,
                    ward_no=ward_row.ward_no,
                    ward_name=ward_row.ward_name,
                    member_name=ward_row.member_name,
                    role=ward_row.role,
                    party=ward_row.party,
                    reservation=ward_row.reservation,
                    gender=person.gender,
                    age=person.age,
                    education=person.education,
                    occupation=person.occupation,
                )
            )
    return tuple(rows)


def parse_front_table(rows: Iterable[Mapping[str, str]]) -> dict[str, FrontEntry]:
    """``party_front_2010.csv`` rows to a lookup keyed by the PDF's own label."""
    return {
        row["party_pdf"]: FrontEntry(
            party_name=row["party_name"],
            party_group=row["party_group"],
            party_front=row["party_front"],
            evidence_tier=row.get("evidence_tier", ""),
        )
        for row in rows
    }


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Y2010Inputs:
    """Everything :func:`build` needs, already parsed. No path to the filesystem."""

    spine: Sequence[SpineRow]
    lsgd_members: Sequence[LsgdMemberRow]
    front_table: Mapping[str, FrontEntry]


@dataclass
class BuildReport:
    """What the 2010 build did -- every known gap named, never silent."""

    candidate_rows: int = 0
    invalid_vote_rows: int = 0
    wards: int = 0
    local_bodies: int = 0
    lsgd_member_rows: int = 0

    unmapped_parties: Counter[str] = field(default_factory=Counter)
    """PDF party labels absent from the front table -- mapped to OTH, reported
    here rather than silently defaulted."""

    lb_pairing_how: Counter[str] = field(default_factory=Counter)
    lb_unpaired: tuple[LBKey, ...] = ()
    """Local bodies the pairing cascade could not match to LSGD at all."""

    lb_gate_rejected: tuple[str, ...] = ()
    """Local bodies that paired but whose wards agreed on nothing -- dropped
    by the same gate that rejects a wrong SEC/LSGD pairing in other cycles."""

    wards_carrying_lsgd: int = 0

    vote_ties: int = 0
    vote_ties_resolved: int = 0
    vote_ties_unresolved: tuple[str, ...] = ()

    gender_orientation: str = ""
    gender_share_female_reserved: float = 0.0
    gender_source_counts: Counter[str] = field(default_factory=Counter)

    winners_gendered: int = 0
    winners_female: int = 0

    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BuildResult:
    candidates: tuple[CandidateRow, ...]
    wards: tuple[dict[str, str], ...]
    local_bodies: tuple[dict[str, str], ...]
    report: BuildReport


@dataclass(frozen=True, slots=True)
class _Cand:
    """One candidate row plus its resolved front -- built once, reused."""

    row: SpineRow
    front: FrontResolution


def _resolve_fronts(
    spine: Sequence[SpineRow], front_table: Mapping[str, FrontEntry], report: BuildReport
) -> dict[str, list[_Cand]]:
    by_ward: dict[str, list[_Cand]] = defaultdict(list)
    for row in spine:
        if row.row_type != "candidate":
            continue
        resolution = party.resolve_front(row.party_pdf, front_table)
        if not resolution.mapped:
            report.unmapped_parties[row.party_pdf] += 1
        by_ward[row.ward_code].append(_Cand(row=row, front=resolution))
    return dict(by_ward)


_LsgdByKey = dict[tuple[str, str, str], dict[int, LsgdMemberRow]]
_LsgdPool = dict[tuple[str, str], frozenset[str]]


def _lsgd_pool(members: Sequence[LsgdMemberRow]) -> tuple[_LsgdByKey, _LsgdPool]:
    """Every member row, keyed for the pairing cascade and for ward lookup."""
    by_key: dict[tuple[str, str, str], dict[int, LsgdMemberRow]] = defaultdict(dict)
    for member in members:
        key = (_canon_district(member.district), member.lb_type, normalize(member.lb_name))
        by_key[key][member.ward_no] = member

    pool: dict[tuple[str, str], set[str]] = defaultdict(set)
    for district, lb_type, name in by_key:
        pool[(district, lb_type)].add(name)

    return dict(by_key), {k: frozenset(v) for k, v in pool.items()}


def _sec_local_bodies(
    cands_by_ward: Mapping[str, list[_Cand]],
) -> dict[str, tuple[str, str, str, str, str]]:
    """One entry per PDF local-body code: (district_norm, lb_type, name_norm,
    raw lb_name, raw district_name) -- the pairing target."""
    sec_lb: dict[str, tuple[str, str, str, str, str]] = {}
    for group in cands_by_ward.values():
        sample = group[0].row
        sec_lb.setdefault(
            sample.lb_code,
            (
                _canon_district(sample.district_name),
                sample.lb_type,
                normalize(sample.lb_name),
                sample.lb_name,
                sample.district_name,
            ),
        )
    return sec_lb


def _verify_and_gate(
    cands_by_ward: Mapping[str, list[_Cand]],
    sec_lb: Mapping[str, tuple[str, str, str, str, str]],
    matched: Mapping[LBKey, str],
    lsgd_by_key: Mapping[tuple[str, str, str], dict[int, LsgdMemberRow]],
    report: BuildReport,
) -> dict[str, LsgdMemberRow]:
    """Join each ward to its LSGD member row, then drop any local body whose
    wards agree on nothing -- the rejection gate, applied to the whole body
    even where an individual ward happened to agree."""
    ward_lsgd: dict[str, LsgdMemberRow] = {}
    tallies: dict[str, WardTally] = {}

    for ward_code, group in cands_by_ward.items():
        code = ward_code[:6]
        district, lb_type, name, _raw_name, _raw_district = sec_lb[code]
        matched_name = matched.get((district, lb_type, name))
        if matched_name is None:
            continue
        member = lsgd_by_key.get((district, lb_type, matched_name), {}).get(group[0].row.ward_no)
        if member is None:
            continue

        top = max(c.row.votes for c in group)
        leaders = [c for c in group if c.row.votes == top]
        ward_agrees = wardnames_agree(member.ward_name, group[0].row.ward_name)
        name_agrees = any(names_agree(member.member_name, c.row.candidate_name) for c in leaders)
        party_agrees = any(
            party.parties_agree(_party_alias(c.row.party_pdf), _party_alias(member.party))
            for c in leaders
        )

        tallies[code] = tallies.get(code, WardTally()).add(
            ward=ward_agrees, name=name_agrees, party=party_agrees
        )
        if ward_agrees or name_agrees or party_agrees:
            ward_lsgd[ward_code] = member

    gate = apply_gate(tallies)
    report.lb_gate_rejected = tuple(sorted(gate.rejected))
    return {wc: row for wc, row in ward_lsgd.items() if wc[:6] in gate.kept}


def _derive_winners(
    cands_by_ward: Mapping[str, list[_Cand]],
    ward_lsgd: Mapping[str, LsgdMemberRow],
    report: BuildReport,
) -> dict[str, winner.WinnerResult]:
    results: dict[str, winner.WinnerResult] = {}
    for ward_code, group in cands_by_ward.items():
        candidates = [
            WinnerCandidate(
                candidate_id=str(i),
                name=c.row.candidate_name,
                party=_party_alias(c.row.party_pdf),
                votes=c.row.votes,
            )
            for i, c in enumerate(group)
        ]
        member = ward_lsgd.get(ward_code)
        result = winner.derive_winner(
            candidates,
            member_name=member.member_name if member else "",
            member_party=_party_alias(member.party) if member else "",
        )
        results[ward_code] = result
        if result.tie:
            report.vote_ties += 1
            if result.winner_id is not None:
                report.vote_ties_resolved += 1
            else:
                report.vote_ties_unresolved = (*report.vote_ties_unresolved, ward_code)
    return results


def _resolve_genders(
    cands_by_ward: Mapping[str, list[_Cand]],
    ward_lsgd: Mapping[str, LsgdMemberRow],
    winners: Mapping[str, winner.WinnerResult],
    report: BuildReport,
) -> dict[tuple[str, int], gender.GenderResolution]:
    """One resolution per (ward_code, index-in-ward) -- the reserved-ward rule
    binds every candidate, so this covers all of them, winners and losers alike."""
    reserved_sexes = [
        c.row.sex
        for ward_code, group in cands_by_ward.items()
        for c in group
        if (m := ward_lsgd.get(ward_code)) is not None and m.reservation in gender.WOMEN_RESERVED
    ]
    orientation = gender.measure_orientation(reserved_sexes)
    report.gender_orientation = orientation.verdict.value
    report.gender_share_female_reserved = orientation.share_female

    out: dict[tuple[str, int], gender.GenderResolution] = {}
    for ward_code, group in cands_by_ward.items():
        member = ward_lsgd.get(ward_code)
        reserved = member is not None and member.reservation in gender.WOMEN_RESERVED
        result = winners.get(ward_code)
        winner_id = result.winner_id if result else None

        for i, c in enumerate(group):
            pdf_sex = gender.oriented_sex(c.row.sex, orientation)

            lsgd_sex = ""
            if member is not None and winner_id == str(i):
                lsgd_sex = {"female": "F", "male": "M"}.get(member.gender.strip().lower(), "")

            resolution = gender.resolve_gender(
                reserved=reserved,
                # The honorific is deliberately absent. The SEC report carries a
                # Sex column for every candidate and 2010's spine *is* that
                # report, so the value arrives per row with no join to go wrong
                # -- it resolves 99.95% of rows unaided. An honorific records
                # how someone is addressed, and the SEC writes "Shri" for
                # women often enough that trusting it blanked two local bodies'
                # reservations in the pipeline this one replaces.
                sources=(
                    gender.GenderSource("pdf", pdf_sex),
                    gender.GenderSource("lsgd", lsgd_sex),
                ),
            )
            out[(ward_code, i)] = resolution
            report.gender_source_counts[resolution.source or "(unresolved)"] += 1
    return out


def build(spec: YearSpec, inputs: Y2010Inputs) -> BuildResult:
    """Assemble 2010's candidates, wards and local bodies from parsed inputs.

    Pure: every collection here is new. The only mutation is the running
    :class:`BuildReport`, which is never exposed until the build is done.
    """
    report = BuildReport()
    invalids: dict[str, int] = {
        r.ward_code: r.votes for r in inputs.spine if r.row_type == "invalid"
    }

    cands_by_ward = _resolve_fronts(inputs.spine, inputs.front_table, report)
    report.candidate_rows = sum(len(g) for g in cands_by_ward.values())
    report.invalid_vote_rows = len(invalids)
    report.wards = len(cands_by_ward)
    report.local_bodies = len({wc[:6] for wc in cands_by_ward})
    report.lsgd_member_rows = len(inputs.lsgd_members)

    lsgd_by_key, lsgd_pool = _lsgd_pool(inputs.lsgd_members)
    sec_lb = _sec_local_bodies(cands_by_ward)

    targets: list[LBKey] = [(d, t, n) for d, t, n, _raw, _draw in sec_lb.values()]
    pairing = pair_local_bodies(targets, lsgd_pool)
    report.lb_pairing_how = pairing.how
    report.lb_unpaired = pairing.unpaired

    ward_lsgd = _verify_and_gate(cands_by_ward, sec_lb, pairing.matched, lsgd_by_key, report)
    report.wards_carrying_lsgd = len(ward_lsgd)

    winners = _derive_winners(cands_by_ward, ward_lsgd, report)
    genders = _resolve_genders(cands_by_ward, ward_lsgd, winners, report)

    lb_seats: dict[str, Counter[str]] = defaultdict(Counter)
    for ward_code, seat_result in winners.items():
        if seat_result.winner_id is None:
            continue
        winning_cand = cands_by_ward[ward_code][int(seat_result.winner_id)]
        lb_seats[ward_code[:6]][winning_cand.front.party_front] += 1
    lb_control = {code: rollup.rollup(counts) for code, counts in lb_seats.items()}

    lb_head: dict[str, dict[str, str]] = {}
    for ward_code, head_member in ward_lsgd.items():
        if head_member.role not in HEAD_ROLES:
            continue
        head_result = winners.get(ward_code)
        head_group = ""
        if head_result and head_result.winner_id is not None:
            # This stores the harmonised front, despite what the column name
            # suggests. Every shipped year does this: 2015 writes
            # party_group="BJP+" on a candidate row but "NDA" here, so that
            # lb_head_party_group and lb_ruling_front stay comparable to each
            # other and across cycles. Writing "BJP+" here would make 2010 the
            # only year whose head and ruling columns use different vocabularies.
            head_group = cands_by_ward[ward_code][int(head_result.winner_id)].front.party_front
        lb_head[ward_code[:6]] = {
            "role": head_member.role,
            "name": head_member.member_name,
            "party": head_member.party,
            "party_group": head_group,
        }

    candidates: list[CandidateRow] = []
    wards: list[dict[str, str]] = []
    winners_gendered = 0
    winners_female = 0

    for ward_code in sorted(cands_by_ward):
        original = cands_by_ward[ward_code]
        # (original index, candidate) pairs, sorted for display -- the index
        # is the id derive_winner assigned, so it must survive the sort.
        ranked = sorted(enumerate(original), key=lambda pair: -pair[1].row.votes)
        group = [cand for _i, cand in ranked]
        member = ward_lsgd.get(ward_code)
        result = winners[ward_code]
        control = lb_control.get(ward_code[:6])
        head = lb_head.get(ward_code[:6], {})
        top_votes = max(c.row.votes for c in group)
        winning_index = int(result.winner_id) if result.winner_id is not None else None
        winning = original[winning_index] if winning_index is not None else None

        for rank, (original_index, cand) in enumerate(ranked, start=1):
            title, name = _split_title(cand.row.candidate_name)
            is_winner = winning is not None and cand is winning
            is_tie_row = (
                result.winner_id is None and cand.row.votes == top_votes and len(group) > 1
            )
            resolution = genders[(ward_code, original_index)]

            candidates.append(
                conform(
                    {
                        "district_code": cand.row.district_code,
                        "district_name": cand.row.district_name,
                        "lb_type": cand.row.lb_type,
                        "lb_code": cand.row.lb_code,
                        "lb_name": cand.row.lb_name,
                        "ward_code": ward_code,
                        "ward_no": cand.row.ward_no,
                        "ward_name": cand.row.ward_name,
                        "party_name": cand.front.party_name,
                        "party_group": cand.front.party_group,
                        "party_front": cand.front.party_front,
                        "party_group_source": PARTY_GROUP_SOURCE,
                        "candidate_code": rank,
                        "candidate_title": title,
                        "candidate_gender": resolution.gender,
                        "gender_source": resolution.source,
                        "candidate_name": name,
                        "candidate_name_eng": member.member_name if is_winner and member else "",
                        "status": "won" if is_winner else ("tie" if is_tie_row else "lost"),
                        "total_votes": cand.row.votes,
                        "invalid_votes": invalids.get(ward_code, ""),
                        "ward_reservation": member.reservation if member else "",
                        "candidate_role": member.role if (is_winner and member) else "",
                        "ward_winner_party": winning.front.party_name if winning else "",
                        "ward_winner_party_group": winning.front.party_group if winning else "",
                        "lb_ruling_front": control.ruling_front if control else "",
                        "lb_control_type": control.control_type if control else "",
                        "lb_head_party_group": head.get("party_group", ""),
                    }
                )
            )
            if is_winner and resolution.gender:
                winners_gendered += 1
                if resolution.gender == "F":
                    winners_female += 1

        runner_up = group[1] if len(group) > 1 else None
        valid_votes = sum(c.row.votes for c in group)
        invalid = invalids.get(ward_code, "")
        wards.append(
            {
                "district_code": group[0].row.district_code,
                "district_name": group[0].row.district_name,
                "lb_type": group[0].row.lb_type,
                "lb_code": group[0].row.lb_code,
                "lb_name": group[0].row.lb_name,
                "ward_code": ward_code,
                "ward_no": str(group[0].row.ward_no),
                "ward_name": group[0].row.ward_name,
                "n_candidates": str(len(group)),
                "valid_votes": str(valid_votes),
                "invalid_votes": str(invalid),
                "winner_name": winning.row.candidate_name if winning else "",
                "winner_party": winning.front.party_name if winning else "",
                "winner_party_group": winning.front.party_group if winning else "",
                "winner_votes": str(winning.row.votes) if winning else "",
                "runnerup_name": runner_up.row.candidate_name if runner_up else "",
                "runnerup_votes": str(runner_up.row.votes) if runner_up else "",
                "uncontested": "Y" if len(group) == 1 else "",
                "tie": "Y" if result.tie else "",
                "reservation": member.reservation if member else "",
                "winner_role": member.role if member else "",
                "winner_gender": member.gender if member else "",
                "lsgd_match": "ok" if member else "none",
                "lsgd_member_name": member.member_name if member else "",
                "lb_ruling_front": control.ruling_front if control else "",
                "lb_control_type": control.control_type if control else "",
            }
        )

    local_bodies: list[dict[str, str]] = []
    for code in sorted(sec_lb):
        # Every PDF local body gets a row, even one with zero decided seats
        # (every ward there tied undecidably) -- rollup.rollup({}) correctly
        # returns a zero-seat control result for that case.
        control = lb_control.get(code) or rollup.rollup({})
        head = lb_head.get(code, {})
        sample = next(c for w in cands_by_ward.values() for c in w if c.row.lb_code == code).row
        cross = rollup.head_cross_front(head.get("party_group", ""), control.largest_front)
        local_bodies.append(
            {
                "district_code": sample.district_code,
                "district_name": sample.district_name,
                "lb_type": sample.lb_type,
                "lb_code": code,
                "lb_name": sample.lb_name,
                "total_wards": str(control.total_seats),
                "lb_seats_udf": str(control.seats_by_front.get("UDF", 0)),
                "lb_seats_ldf": str(control.seats_by_front.get("LDF", 0)),
                "lb_seats_nda": str(control.seats_by_front.get("NDA", 0)),
                "lb_seats_oth": str(control.seats_by_front.get("OTH", 0)),
                "lb_majority_threshold": str(control.majority_threshold),
                "lb_largest_front": control.largest_front,
                "lb_largest_front_seats": str(control.largest_front_seats),
                "lb_ruling_front": control.ruling_front,
                "lb_control_type": control.control_type,
                "lb_head_role": head.get("role", ""),
                "lb_head_name": head.get("name", ""),
                "lb_head_party": head.get("party", ""),
                "lb_head_party_group": head.get("party_group", ""),
                "lb_head_cross_front": cross,
            }
        )

    report.winners_gendered = winners_gendered
    report.winners_female = winners_female
    report.notes = spec.expect.notes

    return BuildResult(
        candidates=tuple(candidates),
        wards=tuple(wards),
        local_bodies=tuple(local_bodies),
        report=report,
    )


# ---------------------------------------------------------------------------
# Entry points -- the same shape the SEC-spine years expose, so the CLI can
# treat every cycle uniformly despite 2010's inverted architecture.
# ---------------------------------------------------------------------------


def load(paths: Paths, *, pdf_cache_dir: Path | None = None) -> Y2010Inputs:
    """Read 2010's PDF, LSGD cache and front table into :class:`Y2010Inputs`.

    Spine mode needs ``pdftotext -layout``: pass 1 separates columns on runs of
    two or more spaces, and only the layout extractor preserves the report's
    geometry. On this report pypdf leaves 69,057 lines unparsed against
    pdftotext's zero, so the engine is named here rather than defaulted.
    """
    spec = spec_for(YEAR)
    text = extract(paths.sec_pdfs / spec.pdf, engine="pdftotext", cache_dir=pdf_cache_dir)
    spine, _report = parse_spine(text.lines())

    if not spec.member_cache or not spec.front_table:
        raise ValueError("2010 needs both a member_cache and a front_table")
    with ResponseCache(paths.caches / spec.member_cache) as cache:
        members = load_lsgd_members(cache)

    with (paths.reference / spec.front_table).open(encoding="utf-8-sig", newline="") as fh:
        front = parse_front_table(list(csv.DictReader(fh)))

    return Y2010Inputs(spine=spine, lsgd_members=members, front_table=front)


def build_year(paths: Paths, *, pdf_cache_dir: Path | None = None) -> BuildResult:
    """Read and assemble 2010 end to end."""
    return build(spec_for(YEAR), load(paths, pdf_cache_dir=pdf_cache_dir))
