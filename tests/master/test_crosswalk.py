"""The crosswalk, and specifically the guesses it must refuse to make.

A body left unmatched is loud: its meetings vanish from the join and the gate
fails. A body matched to the *wrong* counterpart is silent, and renders as a
page of somebody else's spending under the right name. Most of what is asserted
here is therefore about the second kind.

None of this touches the live database. The cascade is pure by construction so
that the awkward cases can be stated in six rows instead of five gigabytes.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from master.crosswalk import (
    apply_overrides,
    cascade,
    district_name_for,
    load_overrides,
    override_district_mismatches,
    plan_spine,
    resolve,
    sakarma_pool,
    sec_registry_bodies,
    unknown_override_codes,
    write_spine,
)
from master.normalise import nm_en, nm_en_body, nm_ml
from master.validate import Source, assess


def problems(result):
    """What the gate would say about a resolve run, with no database behind it."""
    return assess(result, Source()).failures()


GP = "Grama Panchayat"


# --- row builders -----------------------------------------------------------


def spine(code, *, en="", ml="", lb_type=GP, dist="1", first="2010", last="2025", key=1):
    """One row as ``read_spine`` would return it: every value a string."""
    return {
        "lb_key": key,
        "lb_code": code,
        "district_ord": dist,
        "dist_ord": dist,
        "district_name": "KOLLAM",
        "lb_type": lb_type,
        "lb_name_en": en,
        "lb_name_ml": ml,
        "first_cycle": first,
        "last_cycle": last,
        "k_ml": nm_ml(ml),
        "k_en": nm_en(en),
    }


def sakarma(id_, ml, *, lb_type=GP, dist="1"):
    return {"id": id_, "dist_ord": dist, "lb_type": lb_type, "name_ml": ml, "k_ml": nm_ml(ml)}


def sulekha(name, *, year="2023-2024", lb_type=GP, dist="1", sid="s1", di=1, li=1):
    return {
        "sulekha_lb_id": sid,
        "year_label": year,
        "district_index": di,
        "lb_index": li,
        "district_name": "Kollam",
        "lb_type_label": lb_type,
        "lb_type": lb_type,
        "lb_name": name,
        "dist_ord": dist,
        "k_en": nm_en_body(name),
    }


# --- normalisation ----------------------------------------------------------


def test_chillu_variants_normalise_to_one_key():
    """The atomic ർ and the older ര് + ZWJ sequence are the same letter.

    The two portals disagree about which to write. Folding them moved ten
    bodies out of fuzzy matching, which is ten bodies a reviewer no longer has
    to check by hand.
    """
    assert nm_ml("താനൂർ") == nm_ml("താനൂര്‍")


def test_tier_suffix_is_stripped_from_english_names():
    """Sulekha writes the tier into the name; the elections build does not."""
    assert nm_en_body("Kudayathoor Grama Panchayat") == nm_en_body("Kudayathoor")


def test_tier_word_inside_a_name_survives():
    """Only a *trailing* tier is a suffix. Stripping it anywhere else renames the body."""
    assert nm_en_body("Panchayat Nagar") == "panchayatnagar"


# --- the cascade ------------------------------------------------------------


def test_identical_names_match_exactly():
    left = [sakarma(1, "കൊട്ടാരക്കര")]
    right = [spine("G02001", ml="കൊട്ടാരക്കര")]
    matches, unmatched = cascade(left, right, "k_ml", "k_ml")
    assert not unmatched
    assert [(m[2], m[3]) for m in matches] == [("exact", 1.0)]


def test_chillu_variants_match_exact_not_similarity():
    """The point of the folding: these two spellings must never reach the fuzzy pass."""
    left = [sakarma(1, "താനൂർ")]
    right = [spine("G10050", ml="താനൂര്‍")]
    matches, _ = cascade(left, right, "k_ml", "k_ml")
    assert matches[0][2] == "exact"


def test_tier_suffix_variant_matches_exact():
    left = [sulekha("Kudayathoor Grama Panchayat")]
    right = [spine("G07021", en="Kudayathoor")]
    matches, _ = cascade(left, right, "k_en", "k_en")
    assert matches[0][2] == "exact"


def test_last_survivor_on_each_side_is_forced():
    """Elimination: nothing is guessed, the group simply has one place left."""
    left = [sakarma(1, "കൊട്ടാരക്കര"), sakarma(2, "ഊരകം")]
    right = [spine("G02001", ml="കൊട്ടാരക്കര"), spine("G02002", ml="ഉരഗം", key=2)]
    matches, unmatched = cascade(left, right, "k_ml", "k_ml")
    assert not unmatched
    by_method = {m[2]: m for m in matches}
    assert set(by_method) == {"exact", "elimination"}
    assert by_method["elimination"][1]["lb_code"] == "G02002"
    assert by_method["elimination"][3] == 1.0


def test_elimination_does_not_cross_a_group_boundary():
    """Two leftovers in different districts are not each other's counterpart."""
    left = [sakarma(1, "ആലപ്പാട്", dist="1"), sakarma(2, "വേങ്ങര", dist="2")]
    right = [spine("G02001", ml="വേങ്ങര", dist="2")]
    matches, unmatched = cascade(left, right, "k_ml", "k_ml")
    assert [m[2] for m in matches] == ["exact"]
    assert [r["id"] for r in unmatched] == [1]


def test_a_name_matching_two_candidates_is_not_exact():
    """Ambiguity is not equality: two identical names cannot both be the match."""
    left = [sakarma(1, "പേരൂർ")]
    right = [spine("G02001", ml="പേരൂർ"), spine("G02002", ml="പേരൂർ", key=2)]
    matches, unmatched = cascade(left, right, "k_ml", "k_ml")
    assert [m[2] for m in matches] != ["exact"]
    assert len(matches) + len(unmatched) == 1


def test_unrelated_leftovers_stay_unmatched():
    """Below the similarity floor and with more than one survivor, nothing is forced."""
    left = [sakarma(1, "Alappad"), sakarma(2, "Chavara")]
    right = [
        spine("G02001", ml="Thiruvananthapuram"),
        spine("G02002", ml="Kasaragod", key=2),
        spine("G02003", ml="Palakkad", key=3),
    ]
    matches, unmatched = cascade(left, right, "k_ml", "k_ml")
    assert not matches
    assert len(unmatched) == 2


# --- the elimination guard --------------------------------------------------


def test_a_body_defunct_since_2010_is_outside_the_sakarma_pool():
    """Sakarma's meetings begin in 2016, so a body last contested in 2010 has none.

    This is the guard that stopped Mattannur Municipality being forced onto the
    defunct Kannur Municipality (M13052) purely to balance a group.
    """
    live = spine("M13057", en="Mattannur", last="2020")
    defunct = spine("M13052", en="Kannur", last="2010", key=2)
    assert sakarma_pool([live, defunct]) == [live]


def test_a_body_with_no_election_history_stays_in_the_pool():
    """Null cycles mean 'never in a result', not 'long gone' -- the case the guard must keep."""
    registry_only = spine("M13057", en="Mattannur", first="", last="")
    assert sakarma_pool([registry_only]) == [registry_only]


def test_the_guard_leaves_a_sakarma_body_unmatched_rather_than_forcing_it():
    """Without the pool filter this pair would be forced by elimination."""
    defunct = spine("M13052", en="Kannur", ml="കണ്ണൂർ", lb_type="Municipality", last="2010")
    orphan = [sakarma(9, "മട്ടന്നൂർ", lb_type="Municipality")]
    matches, unmatched = cascade(orphan, sakarma_pool([defunct]), "k_ml", "k_ml")
    assert not matches
    assert [r["id"] for r in unmatched] == [9]


# --- overrides --------------------------------------------------------------


OVERRIDE = {
    "source": "sakarma",
    "district_name": "KOLLAM",
    "lb_type": GP,
    "source_name": "കിഴക്കേക്കല്ലട",
    "lb_code": "G02046",
    "reason": "Sakarma writes 'east' in Malayalam; elections transliterates it.",
}


def test_an_override_pairs_two_names_that_share_no_characters():
    left = [sakarma(1, "കിഴക്കേക്കല്ലട")]
    right = [spine("G02046", ml="ഈസ്റ്റ് കല്ലട")]
    forced, rest, missing = apply_overrides(left, right, [OVERRIDE], "sakarma", "name_ml")
    assert not rest and not missing
    assert forced[0][1]["lb_code"] == "G02046"
    assert forced[0][2] == "override"


def test_an_override_for_another_source_is_ignored():
    left = [sakarma(1, "കിഴക്കേക്കല്ലട")]
    right = [spine("G02046", ml="ഈസ്റ്റ് കല്ലട")]
    forced, rest, _ = apply_overrides(left, right, [OVERRIDE], "sulekha", "name_ml")
    assert not forced and len(rest) == 1


def test_an_override_naming_an_unknown_lb_code_is_reported():
    """A typo in an override is worse than no override: the cascade carries on
    and produces some other pairing, so the file looks like it is working."""
    typo = {**OVERRIDE, "lb_code": "G02999"}
    assert unknown_override_codes([typo], ["G02046"]) == ["G02999"]


def test_a_reported_override_reaches_the_gate():
    typo = {**OVERRIDE, "lb_code": "G02999"}
    result = resolve([spine("G02046", ml="ഈസ്റ്റ് കല്ലട")], [], [], [typo])
    assert any("G02999" in problem for problem in problems(result))


def test_a_valid_override_leaves_the_gate_clean():
    result = resolve(
        [spine("G02046", ml="ഈസ്റ്റ് കല്ലട")],
        [sakarma(1, "കിഴക്കേക്കല്ലട")],
        [],
        [OVERRIDE],
    )
    assert problems(result) == []
    assert result.methods["override"] == 1


def test_an_override_does_not_fire_on_the_same_name_in_another_district():
    """Kerala repeats body names across districts, so tier and name are not a key.

    The override names one body in Kollam. A body of the same tier and the same
    name in Palakkad is a different place, and taking the override would key its
    meetings onto somebody else's spending -- silently, which is the failure
    mode this file is written against.
    """
    elsewhere = [sakarma(1, "കിഴക്കേക്കല്ലട", dist="9")]
    right = [spine("G02046", ml="ഈസ്റ്റ് കല്ലട", dist="2")]
    forced, rest, missing = apply_overrides(elsewhere, right, [OVERRIDE], "sakarma", "name_ml")
    assert not forced and not missing
    assert [r["id"] for r in rest] == [1]


def test_the_same_override_still_fires_in_its_own_district():
    """The other half of the discrimination: tightening must not stop it working."""
    here = [sakarma(1, "കിഴക്കേക്കല്ലട", dist="2")]
    right = [spine("G02046", ml="ഈസ്റ്റ് കല്ലട", dist="2")]
    forced, rest, _ = apply_overrides(here, right, [OVERRIDE], "sakarma", "name_ml")
    assert not rest
    assert forced[0][1]["lb_code"] == "G02046"


def test_a_district_written_as_02_and_as_2_are_the_same_district():
    """The two sides spell the ordinal differently; the comparison must not care."""
    here = [sakarma(1, "കിഴക്കേക്കല്ലട", dist="02")]
    right = [spine("G02046", ml="ഈസ്റ്റ് കല്ലട", dist="2")]
    forced, _, _ = apply_overrides(here, right, [OVERRIDE], "sakarma", "name_ml")
    assert forced and forced[0][2] == "override"


def test_an_override_whose_district_contradicts_the_body_it_names_is_reported():
    """Then one half of the row is a typo, and only a human knows which."""
    wrong = {**OVERRIDE, "district_name": "PALAKKAD"}
    reported = override_district_mismatches([wrong], [spine("G02046", ml="ഈസ്റ്റ് കല്ലട")])
    assert reported and "G02046" in reported[0] and "PALAKKAD" in reported[0]


def test_the_committed_overrides_agree_with_the_districts_they_name():
    """The file in data/reference/master, read as the build reads it."""
    from master.config import resolve_paths

    overrides = load_overrides(resolve_paths().overrides)
    kollam = spine("G02046", en="East Kallada", dist="2")
    malappuram = spine("G10077", en="Oorakam", dist="10")
    malappuram["district_name"] = "MALAPPURAM"
    assert overrides
    assert override_district_mismatches(overrides, [kollam, malappuram]) == []


def test_overrides_load_from_the_committed_file(tmp_path):
    path = tmp_path / "crosswalk_overrides.csv"
    path.write_text(
        "source,district_name,lb_type,source_name,lb_code,reason\n"
        "sakarma,KOLLAM,Grama Panchayat,കിഴക്കേക്കല്ലട,G02046,"
        '"reads east in Malayalam, not English"\n',
        encoding="utf-8",
    )
    rows = load_overrides(path)
    assert rows[0]["lb_code"] == "G02046"
    assert "," in rows[0]["reason"]


def test_a_missing_override_file_is_not_an_error(tmp_path):
    """Overrides are a residue, not a requirement; a build without any is normal."""
    assert load_overrides(tmp_path / "absent.csv") == []


# --- the SEC registry -------------------------------------------------------


@pytest.fixture()
def paths(tmp_path):
    """A data root laid out the way the repo's is."""
    from master.config import resolve_paths

    return resolve_paths(tmp_path)


@pytest.fixture()
def registry_cache(paths):
    """A two-row stand-in for ``data/raw/caches/raw_cache_2020.sqlite``."""
    path = paths.sec_registry_cache
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE resp (key text, json text)")
    con.execute(
        "INSERT INTO resp VALUES (?, ?)",
        (
            "detailed_results_urban_ajax.php|13",
            json.dumps({"data": [{"UrbanCd": "M13057", "UrbanNameEng": "Mattannur"}]}),
        ),
    )
    con.execute(
        "INSERT INTO resp VALUES (?, ?)", ("detailed_results_urban_ajax.php|99", "not json")
    )
    con.commit()
    con.close()
    return path


def test_the_registry_yields_tier_and_district_from_the_code(registry_cache):
    assert sec_registry_bodies(registry_cache) == {"M13057": (13, "Municipality", "Mattannur")}


def test_an_unparseable_cache_entry_is_a_failed_scrape_not_a_body(registry_cache):
    assert len(sec_registry_bodies(registry_cache)) == 1


def test_a_missing_cache_yields_no_registry_bodies(tmp_path):
    """A checkout without the scrape cache builds a spine from the cycles alone."""
    assert sec_registry_bodies(tmp_path / "absent.sqlite") == {}


class StubDatabase:
    """Just enough of ``Database`` for ``plan_spine``: one query, one answer."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, sql):
        return [dict(r) for r in self._rows]


def test_a_registry_only_body_joins_the_spine_with_no_cycles(registry_cache, paths):
    """Mattannur is in the SEC's registry and in no cycle of the elections build.

    It still has fourteen years of projects and 366 meetings, so it belongs on
    the spine -- flagged as absent from the results feed, not dropped.
    """
    db = StubDatabase(
        [
            {
                "lb_code": "M13052",
                "district_ord": "13",
                "district_name": "KANNUR",
                "lb_type": "Municipality",
                "lb_name_en": "Kannur",
                "lb_name_ml": "കണ്ണൂർ",
                "first_cycle": "2010",
                "last_cycle": "2010",
            }
        ]
    )
    rows, registry_only = plan_spine(db, paths)

    assert registry_only == 1
    added = next(r for r in rows if r["lb_code"] == "M13057")
    assert added["first_cycle"] == "" and added["last_cycle"] == ""
    assert added["lb_key"] is None
    # It inherits the district it belongs to, so it groups with its neighbours.
    assert added["district_name"] == "KANNUR"


class RecordingDatabase:
    """Just enough of ``Database`` for ``write_spine``: it remembers the inserts."""

    def __init__(self, districts=()):
        self.districts = [dict(d) for d in districts]
        self.inserts: list[list] = []

    def execute(self, sql, params=None):
        return None

    def query(self, sql):
        return [dict(d) for d in self.districts]

    def scalar(self, sql, params=None):
        self.inserts.append(params)
        return 1


def test_a_registry_body_is_inserted_with_a_district_name(registry_cache, paths):
    """``district_name`` is NOT NULL, and this insert is the one that could break it.

    The name used to come from ``max(district_name) … WHERE district_ord = 13``
    over the rows just written, which is NULL for a district whose only body in
    the SEC's registry returned no result -- the exact case this insert exists
    to rescue. It now comes from the district table, which always has an answer.
    """
    db = RecordingDatabase(districts=[])  # no elections body anywhere in Kannur
    assert write_spine(db, paths) == 1
    code, dist_ord, district_name, tier, name, _ = db.inserts[0]
    assert (code, dist_ord, tier, name) == ("M13057", 13, "Municipality", "Mattannur")
    assert district_name == "KANNUR"


def test_a_district_the_elections_build_knows_keeps_its_spelling(registry_cache, paths):
    """One district, one spelling in core.local_body, whichever side wrote the row."""
    db = RecordingDatabase([{"district_ord": "13", "district_name": "KANNUR DISTRICT"}])
    write_spine(db, paths)
    assert db.inserts[0][2] == "KANNUR DISTRICT"


def test_an_ordinal_outside_kerala_is_refused_rather_than_written_as_null():
    with pytest.raises(ValueError, match="99"):
        district_name_for(99, {})


def test_the_spine_states_which_side_of_the_registry_each_body_came_from(registry_cache, paths):
    """``in_elections`` is a fact both callers know outright, so neither guesses it.

    Guessing means reading an empty ``first_cycle``, and every value ``db.query``
    returns is a string: a body the SEC never published and a body whose cycles
    failed to load are indistinguishable that way. The flag is what
    ``core.lb_coverage`` and the elections page read.
    """
    from master.crosswalk import is_in_elections

    db = StubDatabase(
        [
            {
                "lb_code": "M13052",
                "district_ord": "13",
                "district_name": "KANNUR",
                "lb_type": "Municipality",
                "lb_name_en": "Kannur",
                "lb_name_ml": "കണ്ണൂർ",
                "first_cycle": "2010",
                "last_cycle": "2010",
            }
        ]
    )
    rows, _ = plan_spine(db, paths)
    by_code = {r["lb_code"]: r for r in rows}
    assert by_code["M13052"]["in_elections"] is True
    assert by_code["M13057"]["in_elections"] is False
    assert is_in_elections(by_code["M13052"])
    assert not is_in_elections(by_code["M13057"])


def test_the_flag_beats_the_cycles_when_the_two_disagree():
    """Read from ``core.local_body`` the flag arrives as 'f', and it is the fact.
    A body carrying cycles and a false flag is a build bug worth seeing, not one
    to paper over by preferring the evidence to the record."""
    from master.crosswalk import is_in_elections

    assert not is_in_elections({"in_elections": "f", "first_cycle": "2020"})
    assert is_in_elections({"in_elections": "t", "first_cycle": ""})


def test_a_hand_built_row_falls_back_to_its_cycles():
    """Fixtures in this file predate the flag and must keep meaning what they meant."""
    from master.crosswalk import is_in_elections

    assert is_in_elections(spine("G02001", en="Kottarakkara"))
    assert not is_in_elections(spine("M13057", en="Mattannur", first="", last=""))


def test_a_registry_body_already_in_a_cycle_is_not_duplicated(registry_cache, paths):
    db = StubDatabase(
        [
            {
                "lb_code": "M13057",
                "district_ord": "13",
                "district_name": "KANNUR",
                "lb_type": "Municipality",
                "lb_name_en": "Mattannur",
                "lb_name_ml": "മട്ടന്നൂർ",
                "first_cycle": "2020",
                "last_cycle": "2020",
            }
        ]
    )
    rows, registry_only = plan_spine(db, paths)
    assert registry_only == 0
    assert len(rows) == 1


# --- resolve, end to end on a fixture ---------------------------------------


def test_resolve_reports_both_sides_and_gates_on_the_unmatched():
    bodies = [
        spine("G02001", en="Kottarakkara", ml="കൊട്ടാരക്കര", key=1),
        spine("G02002", en="Uragam", ml="ഉരഗം", key=2),
    ]
    result = resolve(
        bodies,
        [sakarma(1, "കൊട്ടാരക്കര"), sakarma(2, "ഊരകം")],
        [sulekha("Kottarakkara Grama Panchayat", sid="a"), sulekha("Oorakam", sid="b", li=2)],
        [],
    )
    assert len(result.sakarma_matches) == 2
    assert result.sakarma_total == 2
    assert len(result.sulekha_rows) == 2
    assert result.sulekha_total == 2
    assert problems(result) == []
    # Every written year-row carries the lb_key of the body it resolved to.
    assert {row[0] for row in result.sulekha_rows} == {1, 2}


def test_an_unmatched_sakarma_body_fails_the_gate():
    result = resolve([spine("G02001", ml="കൊട്ടാരക്കര")], [sakarma(1, "വേങ്ങര", dist="9")], [], [])
    failures = problems(result)
    assert failures and "sakarma" in failures[0]
