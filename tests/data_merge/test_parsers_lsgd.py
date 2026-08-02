"""The LSGD ward-table and person-page parser, over fragments carved from the
real 2010 cache."""

from __future__ import annotations

from pathlib import Path

from data_merge.parsers.lsgd import parse_member_page, parse_person

FIXTURES = Path(__file__).parent / "fixtures"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestWardTableHappyPath:
    """Athiyannoor Block Panchayat, trimmed to its first six wards."""

    def test_district_and_lb_name_are_split_from_the_header(self) -> None:
        page = parse_member_page(
            _html("lsgd_ward_table_athiyannoor.html"), "Block Panchayat"
        )
        assert page.district == "Thiruvananthapuram"
        assert page.lb_name == "Athiyannoor"

    def test_every_ward_row_is_returned(self) -> None:
        page = parse_member_page(
            _html("lsgd_ward_table_athiyannoor.html"), "Block Panchayat"
        )
        assert len(page.rows) == 6
        assert [r.ward_no for r in page.rows] == [1, 2, 3, 4, 5, 6]

    def test_role_and_reservation_are_carried_through(self) -> None:
        page = parse_member_page(
            _html("lsgd_ward_table_athiyannoor.html"), "Block Panchayat"
        )
        by_ward = {r.ward_no: r for r in page.rows}
        assert by_ward[1].role == "Member"
        assert by_ward[1].reservation == "General"
        assert by_ward[3].reservation == "SC"
        assert by_ward[2].reservation == "Woman"

    def test_member_name_and_party_are_carried_through(self) -> None:
        page = parse_member_page(
            _html("lsgd_ward_table_athiyannoor.html"), "Block Panchayat"
        )
        row = next(r for r in page.rows if r.ward_no == 1)
        assert row.member_name == "UCHAKKADA SURESH"
        assert row.ward_name == "MUTTAKKADU"
        assert row.party == "INC"

    def test_the_person_url_is_captured_for_joining_to_the_person_page(self) -> None:
        page = parse_member_page(
            _html("lsgd_ward_table_athiyannoor.html"), "Block Panchayat"
        )
        row = next(r for r in page.rows if r.ward_no == 1)
        assert row.person_url == (
            "/en/lbelection/electdmemberpersondet/2010/11/2010001100101"
        )


class TestPothencodeEmptyWardTable:
    """Pothencode Block Panchayat: the header row renders, no data rows do.

    This is a real, currently-published state, distinct from a fetch failure
    or a malformed page, and it must parse to an empty list instead of
    raising.
    """

    def test_the_header_still_yields_district_and_lb_name(self) -> None:
        page = parse_member_page(
            _html("lsgd_pothencode_bp_no_data_rows.html"), "Block Panchayat"
        )
        assert page.district == "Thiruvananthapuram"
        assert page.lb_name == "Pothencode"

    def test_no_data_rows_yields_an_empty_list_not_an_error(self) -> None:
        page = parse_member_page(
            _html("lsgd_pothencode_bp_no_data_rows.html"), "Block Panchayat"
        )
        assert page.rows == ()


class TestPersonPageBlankFields:
    """A real member whose Educational Qualification the form left blank."""

    def test_populated_fields_are_read_correctly(self) -> None:
        person = parse_person(_html("lsgd_person_blank_fields.html"))
        assert person.age == "52"
        assert person.gender == "Male"
        assert person.marital_status == "Married"
        assert person.occupation == "SOCIAL WORK"

    def test_a_blank_field_is_an_empty_string_not_none(self) -> None:
        person = parse_person(_html("lsgd_person_blank_fields.html"))
        assert person.education == ""
        assert person.education is not None

    def test_a_blank_field_does_not_drop_the_row(self) -> None:
        """The record is still returned whole -- a blank cell counts as data,
        and the person is not rejected because of it."""
        person = parse_person(_html("lsgd_person_blank_fields.html"))
        assert person.age != ""

    def test_address_and_phone_are_not_extracted(self) -> None:
        """The fixture's raw HTML carries a Phone row -- deliberately absent
        from PersonDetail's fields."""
        person = parse_person(_html("lsgd_person_blank_fields.html"))
        assert not hasattr(person, "phone")
        assert not hasattr(person, "address")


class TestPersonPageMissingEntirely:
    def test_a_page_with_none_of_the_known_labels_yields_all_blank_fields(self) -> None:
        person = parse_person("<tr><td>Unrelated</td><td>Value</td></tr>")
        assert person.age == ""
        assert person.gender == ""
        assert person.marital_status == ""
        assert person.education == ""
        assert person.occupation == ""
