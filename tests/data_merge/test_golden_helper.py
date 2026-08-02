"""The golden comparator itself.

A comparator that cannot fail proves nothing, so these tests are mostly about
making sure it does fail -- on unexplained cells, on unexpected rows, and on an
allow-list entry that quietly covers more than it claimed.
"""

from __future__ import annotations

from tests.data_merge.golden import AllowedDifference, compare, row_key


def _row(ward: str, votes: str, name: str, **extra: str) -> dict[str, str]:
    row = {
        "ward_code": ward,
        "total_votes": votes,
        "candidate_name": name,
        "candidate_gender": "M",
        "gender_source": "pdf",
        "ward_reservation": "General",
    }
    row.update(extra)
    return row


class TestIdentical:
    def test_identical_row_sets_compare_clean(self) -> None:
        rows = [_row("G01001001", "500", "ALPHA"), _row("G01001001", "400", "BETA")]
        result = compare(rows, [dict(r) for r in rows])
        assert result.ok
        assert result.unexplained == []
        assert result.allowed == {}


class TestUnexplainedDifferences:
    def test_an_unexplained_cell_change_fails_and_is_named(self) -> None:
        shipped = [_row("G01001001", "500", "ALPHA")]
        rebuilt = [_row("G01001001", "500", "ALPHA", ward_reservation="")]
        result = compare(shipped, rebuilt)
        assert not result.ok
        assert len(result.unexplained) == 1
        diff = result.unexplained[0]
        assert diff.column == "ward_reservation"
        assert diff.shipped == "General"
        assert diff.rebuilt == ""
        assert "G01001001" in result.describe()

    def test_a_row_only_in_the_rebuild_is_reported(self) -> None:
        shipped = [_row("G01001001", "500", "ALPHA")]
        rebuilt = [*shipped, _row("G05049001", "393", "KURIAN JOSEPH")]
        result = compare(shipped, rebuilt)
        assert result.only_in_rebuilt == ["G05049001|393|KURIAN JOSEPH"]

    def test_a_row_only_in_the_shipped_file_is_reported(self) -> None:
        shipped = [_row("G01001001", "500", "ALPHA"), _row("G01001002", "10", "GONE")]
        result = compare(shipped, [shipped[0]])
        assert result.only_in_shipped == ["G01001002|10|GONE"]


class TestAllowList:
    def test_a_named_column_difference_is_allowed_and_counted(self) -> None:
        shipped = [_row("G01001001", "500", "ALPHA", ward_reservation="")]
        rebuilt = [_row("G01001001", "500", "ALPHA", ward_reservation="Woman")]
        result = compare(
            shipped,
            rebuilt,
            allow=[AllowedDifference("ward_reservation", "restored in 30 wards", max_cells=30)],
        )
        assert result.ok
        assert result.allowed == {"ward_reservation": 1}

    def test_exceeding_the_cell_cap_fails_even_though_the_column_is_allowed(self) -> None:
        """An allow-list is a capped budget: 30 restored wards is a fix;
        3,000 is a regression wearing the same label."""
        shipped = [_row(f"G0100100{i}", "500", f"C{i}", ward_reservation="") for i in range(5)]
        rebuilt = [_row(f"G0100100{i}", "500", f"C{i}", ward_reservation="Woman") for i in range(5)]
        result = compare(
            shipped,
            rebuilt,
            allow=[AllowedDifference("ward_reservation", "restored", max_cells=3)],
        )
        assert not result.ok
        assert result.over_cap == ["ward_reservation"]
        assert "cell cap" in result.describe()

    def test_an_uncapped_allowance_permits_any_number(self) -> None:
        shipped = [
            _row(f"G0100100{i}", "500", f"C{i}", gender_source="honorific") for i in range(5)
        ]
        rebuilt = [_row(f"G0100100{i}", "500", f"C{i}", gender_source="pdf") for i in range(5)]
        result = compare(
            shipped, rebuilt, allow=[AllowedDifference("gender_source", "PDF sex applied")]
        )
        assert result.ok
        assert result.allowed == {"gender_source": 5}

    def test_an_allowance_does_not_cover_other_columns(self) -> None:
        shipped = [_row("G01001001", "500", "ALPHA", candidate_gender="M")]
        rebuilt = [_row("G01001001", "500", "ALPHA", candidate_gender="F")]
        result = compare(
            shipped, rebuilt, allow=[AllowedDifference("gender_source", "unrelated")]
        )
        assert not result.ok
        assert result.unexplained[0].column == "candidate_gender"


class TestIgnoredRows:
    def test_an_ignored_key_is_excluded_from_both_sides(self) -> None:
        """The recovered 2010 candidate: a known row-count change should not
        drown the signal it sits beside."""
        recovered = _row("G05049001", "393", "KURIAN JOSEPH")
        shipped = [_row("G01001001", "500", "ALPHA")]
        rebuilt = [*shipped, recovered]
        result = compare(shipped, rebuilt, ignore_rows=[row_key(recovered)])
        assert result.ok
        assert result.only_in_rebuilt == []
        assert result.rebuilt_rows == 2, "the count still reflects reality"


class TestRowKey:
    def test_two_candidates_polling_identically_get_distinct_keys(self) -> None:
        """(ward, votes) collides in real data, so the name is part of the key."""
        first = _row("G01001001", "500", "ALPHA")
        second = _row("G01001001", "500", "BETA")
        assert row_key(first) != row_key(second)
