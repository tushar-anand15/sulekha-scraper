"""The response-cache reader, over a fixture carved from the real caches."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from data_merge.sources.cache import CacheError, ResponseCache

FIXTURE = Path(__file__).parent / "fixtures" / "mini_cache.sqlite"

SEC_KEY = "lb_ajax2.php|_p=can&_s=L&_t=B&_w=B01001001"
LSGD_KEY = "GET|https://lsgkerala.gov.in/en/lbelection/electdistrict/2010/1"


@pytest.fixture
def cache() -> ResponseCache:
    with ResponseCache(FIXTURE) as open_cache:
        yield open_cache


class TestReading:
    def test_the_fixture_holds_the_expected_number_of_rows(self, cache: ResponseCache) -> None:
        assert cache.count() == 16

    def test_endpoints_are_counted_per_key_prefix(self, cache: ResponseCache) -> None:
        assert cache.endpoints() == {
            "lb_ajax2.php": 6,
            "detailed_results_grama_ajax.php": 3,
            "GET": 3,
            "stateView2_ajax.php": 2,
            "contest_cand_ajax.php": 2,
        }

    def test_prefix_filtering_returns_only_matching_endpoints(self, cache: ResponseCache) -> None:
        keys = list(cache.keys("lb_ajax2.php|"))
        assert len(keys) == 6
        assert all(k.startswith("lb_ajax2.php|") for k in keys)
        assert cache.count("lb_ajax2.php|") == 6

    def test_keys_come_back_in_a_stable_order(self, cache: ResponseCache) -> None:
        assert list(cache.keys()) == sorted(cache.keys())

    def test_a_known_sec_key_decodes_to_a_payload(self, cache: ResponseCache) -> None:
        value = cache.get(SEC_KEY)
        assert value["mdata"]["rls"] == "OK"
        first_candidate = value["payload"][0]
        assert first_candidate[0] == "INC"
        assert first_candidate[3] == "RAJESH CHANDRADAS"
        assert first_candidate[4] == 3730

    def test_items_streams_decoded_responses(self, cache: ResponseCache) -> None:
        items = list(cache.items("stateView2_ajax.php|"))
        assert len(items) == 2
        assert all(item.endpoint == "stateView2_ajax.php" for item in items)
        assert all(item.fetched_at > 0 for item in items)


class TestValueShapes:
    """The reader must not assume one shape -- SEC gives dicts, LSGD gives HTML."""

    def test_sec_values_decode_to_dicts(self, cache: ResponseCache) -> None:
        assert isinstance(cache.get(SEC_KEY), dict)

    def test_lsgd_values_decode_to_html_strings(self, cache: ResponseCache) -> None:
        value = cache.get(LSGD_KEY)
        assert isinstance(value, str)
        assert "<" in value

    def test_a_lsgd_key_is_reported_under_the_get_endpoint(self, cache: ResponseCache) -> None:
        item = next(cache.items("GET|"))
        assert item.endpoint == "GET"


class TestMissingAndBroken:
    def test_an_absent_key_returns_none_rather_than_raising(self, cache: ResponseCache) -> None:
        assert cache.get("lb_ajax2.php|_p=can&_s=L&_t=B&_w=NOPE") is None

    def test_require_raises_naming_the_key(self, cache: ResponseCache) -> None:
        with pytest.raises(CacheError, match="NOPE"):
            cache.require("lb_ajax2.php|_p=can&_s=L&_t=B&_w=NOPE")

    def test_a_corrupt_value_surfaces_an_error_naming_the_key(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE resp (key TEXT PRIMARY KEY, json TEXT, fetched_at REAL)")
        conn.execute("INSERT INTO resp VALUES ('lb_ajax2.php|_w=X', '{not json', 0.0)")
        conn.commit()
        conn.close()

        with ResponseCache(path) as cache:
            with pytest.raises(CacheError, match="_w=X"):
                cache.get("lb_ajax2.php|_w=X")

    def test_a_missing_file_is_reported_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(CacheError, match="no cache at"):
            ResponseCache(tmp_path / "absent.sqlite")

    def test_a_database_without_a_resp_table_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "other.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE something_else (x INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(CacheError, match="no 'resp' table"):
            ResponseCache(path)


class TestReadOnly:
    def test_the_cache_cannot_be_written_through(self, cache: ResponseCache) -> None:
        """The caches are the sources of record; a build must not be able to
        touch them even by accident."""
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            cache._conn.execute("DELETE FROM resp")


class TestPrefixEscaping:
    def test_an_underscore_in_a_prefix_is_literal_not_a_wildcard(
        self, tmp_path: Path
    ) -> None:
        """LSGD keys are URLs, and SQL LIKE treats ``_`` as any character."""
        path = tmp_path / "c.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE resp (key TEXT PRIMARY KEY, json TEXT, fetched_at REAL)")
        conn.executemany(
            "INSERT INTO resp VALUES (?, '\"x\"', 0.0)",
            [("GET|a_b",), ("GET|axb",)],
        )
        conn.commit()
        conn.close()

        with ResponseCache(path) as cache:
            assert list(cache.keys("GET|a_b")) == ["GET|a_b"]
