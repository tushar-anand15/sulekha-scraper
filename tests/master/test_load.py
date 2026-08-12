"""The source loaders' one job: get every row in without reshaping it.

What can go wrong here is quiet. A column present in 2010 and absent from 2025
that is dropped from the union takes a cycle's worth of a field with it; a
free-text reason with a comma in it, re-emitted carelessly, truncates the
justification for a hand-recorded override into a fragment. Both are asserted.

The COPY itself needs a server and is exercised by the runbook's numbers, not
here -- these are the decisions made in Python before a byte is sent.
"""

from __future__ import annotations

import csv
import json

import pytest

from master.load.elections import cycle_paths, header, union_columns
from master.load.geo import feature_lines, fold_surplus_fields

# --- election CSVs ----------------------------------------------------------


def test_union_keeps_first_seen_order():
    """Not sorted: the table should read the way the CSVs do."""
    assert union_columns([["lb_code", "ward_no"], ["lb_code", "ward_no"]]) == ["lb_code", "ward_no"]


def test_union_carries_a_column_only_one_cycle_has():
    """2010 carries different columns from the later cycles, and loses none of them."""
    assert union_columns([["lb_code", "sex"], ["lb_code", "front"]]) == ["lb_code", "sex", "front"]


def test_header_strips_the_byte_order_mark(tmp_path):
    """A BOM left on the first column name silently renames it."""
    path = tmp_path / "candidates_2010.csv"
    path.write_text("lb_code,ward_no\nG02001,1\n", encoding="utf-8-sig")
    assert header(path) == ["lb_code", "ward_no"]


def test_every_cycle_is_looked_for_in_its_own_directory(tmp_path):
    from master.config import resolve_paths

    files = cycle_paths(resolve_paths(tmp_path), "wards")
    assert files[2010].name == "wards_2010.csv"
    assert files[2010].parent.name == "2010"
    assert set(files) == {2010, 2015, 2020, 2025}


def test_a_missing_cycle_file_stops_the_load(tmp_path):
    """Loading three cycles of four and reporting success is worse than failing."""
    from master.config import resolve_paths
    from master.load.elections import load

    with pytest.raises(FileNotFoundError):
        load(_RefusesToTalk(), resolve_paths(tmp_path))


class _RefusesToTalk:
    """The loader must give up on the filesystem before it touches the database."""

    def execute(self, sql, params=None):
        return None


# --- reference crosswalks ---------------------------------------------------


COLUMNS = ["lb_code", "ksmart_lb_code", "reason"]


def test_a_well_formed_row_is_left_alone():
    assert fold_surplus_fields([["G02046", "G020403", "transliteration"]], COLUMNS) == [
        ["G02046", "G020403", "transliteration"]
    ]


def test_an_unquoted_comma_folds_back_into_the_reason():
    """The reason is the whole value of an override; a truncated one reads as a fragment."""
    folded = fold_surplus_fields([["G02046", "G020403", "Oo- vs U-", " -kam vs -gam"]], COLUMNS)
    assert folded == [["G02046", "G020403", "Oo- vs U-, -kam vs -gam"]]


def test_a_short_row_is_padded_rather_than_rejected():
    assert fold_surplus_fields([["G02046", "G020403"]], COLUMNS) == [["G02046", "G020403", ""]]


def test_a_folded_row_survives_a_csv_round_trip():
    """It is re-emitted through the writer, so the comma must come back quoted."""
    import io

    folded = fold_surplus_fields([["G02046", "G020403", "one", "two"]], COLUMNS)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(folded)
    assert next(csv.reader(io.StringIO(buf.getvalue())))[2] == "one,two"


# --- GeoJSON layers ---------------------------------------------------------


def feature(geometry=None, **props):
    return {"properties": props, "geometry": geometry or {"type": "Point", "coordinates": [0, 0]}}


def test_a_feature_becomes_one_tab_separated_line():
    line = feature_lines("lb_2025", [feature(lb_code="G02046", ward_code="W1")])[0]
    layer, lb_code, ward_code, props, geometry = line.split("\t")
    assert (layer, lb_code, ward_code) == ("lb_2025", "G02046", "W1")
    assert json.loads(props)["lb_code"] == "G02046"
    assert json.loads(geometry)["type"] == "Point"


def test_an_absent_code_is_postgres_null_not_the_empty_string():
    """A local-body layer has no ward code; storing '' would make it look present."""
    line = feature_lines("lb_2025", [feature(lb_code="G02046")])[0]
    assert line.split("\t")[2] == "\\N"


def test_the_payload_never_contains_a_tab_or_a_newline():
    """The delimiter is only safe because compact JSON has neither."""
    line = feature_lines("lb_2025", [feature(lb_code="G02046", name="a\tb\nc")])[0]
    assert len(line.split("\t")) == 5


def test_malayalam_names_are_not_escaped_away():
    """``ensure_ascii`` would store \\u0d15 sequences the site then has to decode."""
    line = feature_lines("lb_2025", [feature(lb_code="G02046", name_ml="കൊല്ലം")])[0]
    assert "കൊല്ലം" in line
