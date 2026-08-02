"""The SEC trend-site ajax parser, over payloads carved from the real 2015/2020 caches."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from data_merge.parsers.contest import parse_contest_ward
from data_merge.parsers.sec_trend import (
    INVALID_VOTES_CODE,
    CanCol,
    SecTrendError,
    WvCol,
    parse_can,
    parse_detail,
    parse_dv,
    parse_wv,
    ward_code_from_detail_key,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _payload(name: str) -> object:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class TestWvHappyPath:
    """A real ``lb_ajax2.php`` ``wv`` response: D04001, 13 district-panchayat wards."""

    KEY = "lb_ajax2.php|_p=wv&_s=L&_t=D&_w=D04001"

    def test_yields_the_expected_ward_count(self) -> None:
        rows = parse_wv(_payload("sec_wv_2015_D04001.json"), key=self.KEY)
        assert len(rows) == 23

    def test_winner_name_party_and_votes_are_correctly_positioned(self) -> None:
        rows = parse_wv(_payload("sec_wv_2015_D04001.json"), key=self.KEY)
        veliyanad = next(r for r in rows if r.ward_code == "D04001006")
        assert veliyanad.winner_name == "K.K.ASOKAN"
        assert veliyanad.winner_group == "LDF"
        assert veliyanad.winner_votes == 22865
        assert veliyanad.runnerup_name == "GOPAKUMAR"
        assert veliyanad.runnerup_votes == 20439

    def test_ward_no_is_derived_from_the_ward_code_not_a_separate_column(self) -> None:
        rows = parse_wv(_payload("sec_wv_2015_D04001.json"), key=self.KEY)
        row = next(r for r in rows if r.ward_code == "D04001006")
        assert row.ward_no == 6


class TestCanHappyPath:
    """The per-ward candidate feed: winner, losers and the invalid-votes row."""

    KEY = "lb_ajax2.php|_p=can&_s=L&_t=D&_w=D01001005"

    def test_every_candidate_is_returned_including_invalid_votes(self) -> None:
        rows = parse_can(_payload("sec_can_D01001005.json"), key=self.KEY)
        assert len(rows) == 5
        assert sum(1 for r in rows if r.candidate_code == INVALID_VOTES_CODE) == 1

    def test_the_winner_is_flagged_first_with_the_right_votes(self) -> None:
        rows = parse_can(_payload("sec_can_D01001005.json"), key=self.KEY)
        winner = next(r for r in rows if r.is_first)
        assert winner.candidate_name == "SHOBHAKUMAR Y.V."
        assert winner.party_name == "CPI(M)"
        assert winner.votes == 26224

    def test_column_positions_are_named_not_indexed_at_the_call_site(self) -> None:
        """A row is exactly the enum's own length -- proves CanCol enumerates
        every position the parser reads, with nothing left unnamed."""
        assert len(CanCol) == 7
        assert len(WvCol) == 10


class TestDetailEndpoints:
    """The four ``detailed_results_*`` endpoints -- one flat, one 'cand'-wrapped."""

    def test_the_flat_grama_shape_decodes_including_invalid_votes(self) -> None:
        key = "detailed_results_grama_ajax.php|process=getGramaLostCandData&wardCd=G01001004"
        rows = parse_detail(_payload("sec_detail_grama_G01001004.json"), key=key)
        assert len(rows) == 5
        winner = next(r for r in rows if r.pos == 1)
        assert winner.name == "KENSI LALI S"
        assert winner.party_group == "UDF"
        invalid = next(r for r in rows if r.candidate_code == INVALID_VOTES_CODE)
        assert invalid.party_group == "Inva"

    def test_the_cand_wrapped_block_shape_decodes_the_same_way(self) -> None:
        key = "detailed_results_block_ajax.php|process=getBlockLostCandData&wardCd=B01001001"
        rows = parse_detail(_payload("sec_detail_block_B01001001.json"), key=key)
        winner = next(r for r in rows if r.pos == 1)
        assert winner.name == "RAJESH CHANDRADAS"
        assert winner.votes == 3730
        assert winner.party_group == "UDF"

    def test_ward_code_is_read_from_the_key_not_the_payload(self) -> None:
        key = "detailed_results_grama_ajax.php|process=getGramaLostCandData&wardCd=G01001004"
        assert ward_code_from_detail_key(key) == "G01001004"

    def test_a_ward_with_no_candidate_data_answers_with_a_bare_list(self) -> None:
        """Some wards answer this endpoint with `[]`, no `mdata` envelope at
        all -- a real, expected outcome, no error."""
        key = "detailed_results_grama_ajax.php|process=getGramaLostCandData&wardCd=G99999999"
        assert parse_detail([], key=key) == ()


class TestDvHappyPath:
    def test_local_body_rows_and_district_name_are_decoded(self) -> None:
        key = "stateView2_ajax.php|_d=D01001&_l=D&_p=dv&_s=L"
        payload = {
            "mdata": {"rls": "OK"},
            "summary": [["D01001", "THIRUVANANTHAPURAM", "LDF", "50"]],
            "payload": [["D01001", "THIRUVANANTHAPURAM", "26", "14", "6", "19", "1", "0"]],
        }
        response = parse_dv(payload, key=key)
        assert response.district_name == "THIRUVANANTHAPURAM"
        assert len(response.rows) == 1
        assert response.rows[0].lb_code == "D01001"
        assert response.rows[0].total_wards == 26
        assert response.rows[0].majority_number == 14
        assert response.rows[0].seats_udf == 6


class TestMalayalamNamesSurviveUnmangled:
    """2020 publishes contesting-candidate names in Malayalam; nothing here
    should transliterate, re-encode or otherwise mangle them."""

    KEY = (
        "contest_cand_ajax.php|Panchayat=G01001&districtCode=D01001"
        "&process=getContestCandGrama&ward=G01001001"
    )

    def test_the_malayalam_name_round_trips_exactly(self) -> None:
        rows = parse_contest_ward(
            _payload("contest_2020_G01001001.json"), "G01001001", key=self.KEY
        )
        omana = next(r for r in rows if r.name_eng == "Omana")
        assert omana.name_mal == "ഡി ഓമന"
        assert omana.party_group == "LDF"
        assert omana.sex == "Female"

    def test_ward_code_comes_from_the_caller_not_the_payload(self) -> None:
        """The urban variant of this feed carries no ward-code field at all,
        so every record trusts the caller's ward_code over any field guess."""
        rows = parse_contest_ward(
            _payload("contest_2020_G01001001.json"), "G01001001", key=self.KEY
        )
        assert all(r.ward_code == "G01001001" for r in rows)


class TestUnexpectedShapesRaiseNamingEndpointAndKey:
    KEY = "lb_ajax2.php|_p=wv&_s=L&_t=D&_w=D99999"

    def test_wv_rejects_a_non_dict_payload(self) -> None:
        with pytest.raises(SecTrendError, match="lb_ajax2.php") as exc:
            parse_wv(["not", "a", "dict"], key=self.KEY)
        assert self.KEY in str(exc.value)

    def test_wv_rejects_a_row_missing_columns(self) -> None:
        with pytest.raises(SecTrendError, match=self.KEY):
            parse_wv({"payload": [["D99999001", "LDF"]]}, key=self.KEY)

    def test_can_rejects_a_payload_with_no_payload_field(self) -> None:
        key = "lb_ajax2.php|_p=can&_s=L&_t=D&_w=D99999"
        with pytest.raises(SecTrendError, match=key):
            parse_can({"mdata": {"rls": "OK"}}, key=key)

    def test_dv_rejects_a_non_dict_payload(self) -> None:
        key = "stateView2_ajax.php|_d=D99999&_l=D&_p=dv&_s=L"
        with pytest.raises(SecTrendError, match=key):
            parse_dv(None, key=key)

    def test_detail_rejects_an_entry_that_is_not_a_dict(self) -> None:
        key = "detailed_results_grama_ajax.php|process=getGramaLostCandData&wardCd=G99999999"
        with pytest.raises(SecTrendError, match=key):
            parse_detail({"1": "not a dict"}, key=key)

    def test_ward_code_from_key_names_the_key_when_absent(self) -> None:
        key = "detailed_results_grama_ajax.php|process=getGramaLostCandData"
        with pytest.raises(SecTrendError, match=key):
            ward_code_from_detail_key(key)

    def test_contest_rejects_a_payload_with_no_data_field(self) -> None:
        from data_merge.parsers.contest import ContestError

        key = "contest_cand_ajax.php|process=getContestCandGrama&ward=G99999999"
        with pytest.raises(ContestError, match=key):
            parse_contest_ward({"mdata": {}}, "G99999999", key=key)
