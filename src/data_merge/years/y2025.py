"""2025 -- the third SEC-spine year, and the only one WIYR-backed.

Genuinely 2025-specific, well beyond a ``YearSpec`` declaration:

* **Members come from WIYR, not LSGD.** ``base.load_inputs`` hardcodes
  :func:`~data_merge.years.base.load_lsgd_members`, so this module walks the
  WIYR cache itself (:func:`load_wiyr_members`) and builds
  :class:`~data_merge.years.base.LsgdMember` records directly -- the field
  set is structural, and ``base.build`` does not care which portal produced
  it. Every WIYR page is a sitting member (the site publishes no losers), so
  this is a *winners-only* member feed, unlike LSGD's ward-by-ward roster.

* **Front is derived, not published.** 2025's trend site answers
  ``detailed_results_*`` with a 500, so there is no per-candidate front feed
  at all. What there *is*: every WIYR member page carries its own front
  badge (UDF/LDF/NDA/OTH), a genuine SEC-published fact about that member's
  party. :func:`load_wiyr_members` turns that into ``front_by_party`` --
  one front per party abbreviation, resolved by majority vote wherever a
  party's own winners disagree -- and :func:`_derive_detail` applies it to
  every candidate sharing that party, winners and losers alike, exactly the
  way ``base.build`` expects to find a ``party_group`` on any SEC-spine year.
  A party that never won anywhere has no evidence and is left unmapped
  (empty group) rather than guessed at "OTH": ``base.build`` itself has no
  notion of "unmapped" (it always stamps ``party_group_source=published``,
  correct for 2015/2020's genuinely published front), so
  :func:`_relabel_party_group_source` corrects that label afterwards, for
  exactly the rows this module knows were never actually mapped.

* **No ward roster, no contest feed, no invalid votes.** ``spec.has_roster``
  and ``spec.has_contest_feed`` are both ``False``; ``spec.has_invalid_votes``
  is unset (``False``), which is what empties the column -- see
  ``base.build``'s row assembly, gated on the declaration, not on whether a
  row was seen.

* **PDF layout is NEW** (party, name, sex, age, address, votes), the same
  shape as 2020's report -- passed to :func:`~data_merge.parsers.pdf_candidates.parse_patch`
  explicitly, the same reasoning as 2015's ``Layout.OLD``.

* **The known ``dv`` gap in Wayanad and Kasargod is a non-issue here.**
  ``base._load_local_bodies`` never reads a local body's ward *count* from
  the ``dv`` summary, only its code/name/type/district; the wards themselves
  come from the ``can`` feed (per ward), which the broken summary does not
  touch. Nothing in this module needs to special-case it.

* **District Panchayat and Corporation have no ``/wyrw/`` level.** Grama,
  Block and Municipality each have many bodies per district, so their
  ``wyrlb`` index page lists one link per body, each pointing at that body's
  own ``wyrw`` ward table. District Panchayat and Corporation have exactly
  one body per district -- there is nothing to index -- so their ``wyrlb``
  page *is* the ward table, member links and all. The same structural fact
  shows up on the LSGD portal, where ``base.py``'s ``_LSGD_DIRECT_TYPES``
  constant exists for exactly this reason. :func:`_ward_table_pages` branches
  on what a page actually contains (any ``/wyrw/`` link at all) rather than
  hardcoding which family codes behave which way, so it does not silently
  break if a future district gains a second corporation.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from data_merge.config import Paths
from data_merge.parsers.pdf_candidates import LB_FAMILY, Layout, parse_patch
from data_merge.parsers.sec_trend import INVALID_VOTES_CODE, CanRow, DetailCandidate
from data_merge.parsers.wiyr import (
    RE_CELL,
    RE_ROW,
    RE_ROW_MEMBER,
    RE_TAG,
    parse_member,
    role_english,
)
from data_merge.schema import CandidateRow
from data_merge.sources.cache import ResponseCache
from data_merge.sources.pdf import extract
from data_merge.spec import spec_for
from data_merge.transform.party import party_key
from data_merge.years import base
from data_merge.years.base import BuildResult, LsgdMember, SecSpineInputs

YEAR = 2025

PDF_LAYOUT = Layout.NEW

_BASE_URL: Final = "https://sec.kerala.gov.in"

# The WIYR site indexes local bodies by numeric district id (1-14, matching
# the SEC's own district numbering) and a one-letter family code -- the same
# G/B/D/M/C alphabet ``LB_FAMILY`` already names, so this list is restated
# rather than imported as anything more specific.
_DISTRICT_IDS: Final = range(1, 15)
_LB_LETTERS: Final = ("B", "C", "D", "G", "M")

# One ``wyrlb`` row links a local body's WIYR ward-table page ("G14001 - name"
# in the link text -- SEC's own ``lb_code``, though this module has no use
# for it: the WIYR/SEC pairing that matters happens by district+type+name in
# ``base.py``'s own local-body pairing cascade, not by code). Only the page
# id is needed here.
RE_WYRW_LINK: Final = re.compile(r'href="/public/wyrw/(\d+)"')

_WARD_ROW_MIN_CELLS: Final = 6
"""Ward no, ward name, member, role, party, reservation -- a row with fewer
cells is header or spacer markup, not a data row (mirrors
``wiyr.parse_ward_table``'s own row-selection rule)."""


def _get_html(cache: ResponseCache, url: str) -> str | None:
    value = cache.get(f"GET|{url}")
    return value if isinstance(value, str) else None


def _clean_cell(html: str) -> str:
    text = RE_TAG.sub(" ", html).replace("&nbsp;", " ").replace("&amp;", "&")
    return re.sub(r"\s+", " ", text).strip()


def _ward_table_pages(cache: ResponseCache) -> list[tuple[str, str]]:
    """Every WIYR ward-table page's raw HTML, paired with its local-body
    type -- walking the district/family index pages exactly as the WIYR
    scrape did when it filled the cache.

    A ``wyrlb`` page that links to one or more ``wyrw`` pages is a normal
    multi-body index (Grama, Block, Municipality) -- each linked page is
    fetched and returned. A ``wyrlb`` page with no such link is District
    Panchayat or Corporation's own page, which carries the ward table
    directly (see the module docstring) and is returned as-is. Branching on
    the page's own content, rather than on the family letter, is what keeps
    this correct even for a family that mixes both shapes.
    """
    pages: list[tuple[str, str]] = []
    seen_wyrw: set[str] = set()
    for district in _DISTRICT_IDS:
        for letter in _LB_LETTERS:
            html = _get_html(cache, f"{_BASE_URL}/public/wyrlb/{district}/{letter}")
            if html is None:
                continue
            lb_type = LB_FAMILY.get(letter, "")
            wyrw_ids = dict.fromkeys(RE_WYRW_LINK.findall(html))
            if not wyrw_ids:
                pages.append((lb_type, html))
                continue
            for wyrw_id in wyrw_ids:
                if wyrw_id in seen_wyrw:
                    continue
                seen_wyrw.add(wyrw_id)
                ward_html = _get_html(cache, f"{_BASE_URL}/public/wyrw/{wyrw_id}")
                if ward_html is not None:
                    pages.append((lb_type, ward_html))
    return pages


def _parse_ward_rows(html: str) -> dict[str, tuple[str, str]]:
    """``member id -> (role_mal, party_abbrev)``, read off one ``wyrw`` page.

    Mirrors ``wiyr.parse_ward_table``'s row-selection rule (skip anything
    whose first cell is not a ward number) but also keeps the party column
    (cell index 4), which that function does not expose -- 2025 has no LSGD
    party field, so a winner's own ward-table row is the only place their
    party's SEC abbreviation is stated at all.
    """
    out: dict[str, tuple[str, str]] = {}
    for row_html in RE_ROW.findall(html):
        cells_html = RE_CELL.findall(row_html)
        if len(cells_html) < _WARD_ROW_MIN_CELLS:
            continue
        cells = [_clean_cell(c) for c in cells_html]
        if not re.match(r"^\d+$", cells[0] or ""):
            continue
        member = RE_ROW_MEMBER.search(row_html)
        if member:
            out[member.group(1)] = (cells[3], cells[4])
    return out


def load_wiyr_members(cache: ResponseCache) -> tuple[tuple[LsgdMember, ...], dict[str, str]]:
    """Every WIYR-published elected member, plus the party front table their
    own pages establish.

    Every WIYR record is a winner -- the site has no losers to publish -- so
    the front badge on a member's own page is real evidence for their party,
    not just for their own ward: ``front_by_party`` (keyed by
    :func:`~data_merge.transform.party.party_key`, so abbreviation spelling
    differences do not fragment one party into two) resolves by majority
    vote wherever a party's own winners disagree, the same tolerance
    ``base.py``'s own local-body pairing gives fuzzy evidence elsewhere.
    """
    members: list[LsgdMember] = []
    front_votes: dict[str, Counter[str]] = defaultdict(Counter)

    for lb_type, ward_html in _ward_table_pages(cache):
        for member_id, (role_mal, party_abbrev) in _parse_ward_rows(ward_html).items():
            member_html = _get_html(cache, f"{_BASE_URL}/public/wyr/view/{member_id}")
            if member_html is None:
                continue
            wm = parse_member(member_html, lb_type)
            members.append(
                LsgdMember(
                    district=wm.district_name,
                    lb_type=lb_type,
                    lb_name=wm.lb_name,
                    ward_no=wm.ward_no,
                    ward_name=wm.ward_name,
                    member_name=wm.member_name,
                    role=role_english(role_mal),
                    party=party_abbrev,
                    reservation=wm.reservation,
                    gender=wm.gender,
                )
            )
            if party_abbrev and wm.party_front:
                front_votes[party_key(party_abbrev)][wm.party_front] += 1

    front_by_party = {key: votes.most_common(1)[0][0] for key, votes in front_votes.items()}
    return tuple(members), front_by_party


def _derive_detail(
    can_by_ward: Mapping[str, tuple[CanRow, ...]], front_by_party: Mapping[str, str]
) -> dict[str, tuple[DetailCandidate, ...]]:
    """The per-candidate front 2025 never publishes, derived instead from the
    party's own group -- established wherever that party actually won (see
    :func:`load_wiyr_members`). A party with no winner anywhere has no
    evidence and is left unmapped (empty ``party_group``) rather than
    defaulted to a front it was never observed to hold; ``build_year``
    relabels those rows' ``party_group_source`` afterwards.

    ``base.build`` reads ``party_group`` from ``detail_by_ward`` unconditionally
    (never gated on ``spec.has_detail``), so a synthetic detail map handed in
    here is exactly what makes a derived front reach the output the same way
    a published one does on the other SEC-spine years.
    """
    out: dict[str, tuple[DetailCandidate, ...]] = {}
    for ward_code, rows in can_by_ward.items():
        entries: list[DetailCandidate] = []
        for row in rows:
            if row.candidate_code == INVALID_VOTES_CODE and not row.party_name:
                continue
            group = front_by_party.get(party_key(row.party_name), "")
            entries.append(
                DetailCandidate(
                    candidate_code=row.candidate_code,
                    name=row.candidate_name,
                    party=row.party_name,
                    votes=row.votes,
                    pos=0,
                    party_group=group,
                )
            )
        out[ward_code] = tuple(entries)
    return out


def _load(paths: Paths, *, pdf_cache_dir: Path | None) -> tuple[SecSpineInputs, dict[str, str]]:
    """Read 2025's caches and PDF, and also return ``front_by_party`` -- the
    one piece of derivation state :func:`load` has no reason to expose but
    :func:`build_year` needs again afterwards, to relabel the rows a party
    table lookup left unmapped."""
    spec = spec_for(YEAR)
    if not spec.sec_cache or not spec.member_cache:
        raise ValueError(f"{YEAR}: needs sec_cache and member_cache")

    with ResponseCache(paths.caches / spec.sec_cache) as sec:
        local_bodies = base._load_local_bodies(sec)
        ward_names = base._load_wv_ward_names(sec)
        can_by_ward = base._load_can(sec)

    with ResponseCache(paths.caches / spec.member_cache) as wiyr_cache:
        members, front_by_party = load_wiyr_members(wiyr_cache)

    detail_by_ward = _derive_detail(can_by_ward, front_by_party)

    pdf_text = extract(paths.sec_pdfs / spec.pdf, cache_dir=pdf_cache_dir)
    pdf_rows, _report = parse_patch(pdf_text.lines(), PDF_LAYOUT)

    inputs = SecSpineInputs(
        local_bodies=local_bodies,
        ward_names=ward_names,
        can_by_ward=can_by_ward,
        detail_by_ward=detail_by_ward,
        lsgd_members=members,
        pdf_patch=tuple(pdf_rows),
    )
    return inputs, front_by_party


def load(paths: Paths, *, pdf_cache_dir: Path | None = None) -> SecSpineInputs:
    """Read 2025's caches and PDF into :class:`SecSpineInputs`."""
    inputs, _front_by_party = _load(paths, pdf_cache_dir=pdf_cache_dir)
    return inputs


def _relabel_party_group_source(
    row: CandidateRow, front_by_party: Mapping[str, str]
) -> CandidateRow:
    """``base.build`` always stamps ``published`` (see its
    ``PARTY_GROUP_SOURCE`` constant) -- correct for 2015/2020's genuinely
    published front, wrong for 2025's derived one wherever the party was
    never actually mapped. ``base.build`` has no way to know that
    distinction structurally, so it is corrected here, once, after the fact.
    """
    if party_key(row["party_name"]) in front_by_party:
        return row
    relabelled = dict(row)
    relabelled["party_group_source"] = "unmapped"
    return relabelled


def build_year(paths: Paths, *, pdf_cache_dir: Path | None = None) -> BuildResult:
    """Read and assemble 2025 end to end."""
    spec = spec_for(YEAR)
    inputs, front_by_party = _load(paths, pdf_cache_dir=pdf_cache_dir)
    result = base.build(spec, inputs)
    candidates = tuple(
        _relabel_party_group_source(row, front_by_party) for row in result.candidates
    )
    return BuildResult(
        candidates=candidates,
        wards=result.wards,
        local_bodies=result.local_bodies,
        report=result.report,
    )
