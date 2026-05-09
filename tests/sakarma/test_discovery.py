"""Tests for sakarma.tasks.discovery.run.

Unit tests (no DB, no live HTTP) use MagicMock for SakarmaClient and monkeypatch
for parse_dropdown_options.  Integration tests require a live Postgres instance
and are marked ``@pytest.mark.integration``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator
from unittest.mock import MagicMock, call, patch

import pytest

import sakarma.tasks.discovery as discovery_module
from sakarma.scraper.protocol import FormState
from sakarma.tasks.discovery import _parse_year_int, run


# =============================================================================
# Unit helpers
# =============================================================================


def _make_form_state(page_url: str = "http://portal/page") -> FormState:
    return FormState(
        viewstate="VS",
        viewstate_generator="VG",
        event_validation="EV",
        form_fields={},
        page_url=page_url,
    )


def _make_raw_html() -> bytes:
    """Minimal HTML that parse_dropdown_options won't crash on."""
    return b"<html><body></body></html>"


# =============================================================================
# _parse_year_int
# =============================================================================


class TestParseYearInt:
    def test_plain_year(self):
        assert _parse_year_int("2024") == 2024

    def test_fiscal_year_label(self):
        assert _parse_year_int("2024-25") == 2024

    def test_with_surrounding_text(self):
        assert _parse_year_int("Year 2023-24") == 2023

    def test_no_year_returns_none(self):
        assert _parse_year_int("----") is None

    def test_empty_string(self):
        assert _parse_year_int("") is None


# =============================================================================
# Unit: happy-path — 2 districts × 2 LB types × 3 LBs = 12 upsert calls
# =============================================================================


class TestRunHappyPath:
    """Stubbed client; no DB, no network."""

    @pytest.fixture
    def patched_env(self, monkeypatch):
        """Patch every external dependency of discovery.run."""
        raw_html = _make_raw_html()
        base_state = _make_form_state()

        # --- SakarmaClient stub ---
        mock_client = MagicMock()
        mock_client._abs.return_value = "http://portal/page"
        mock_client._request.return_value = MagicMock(content=raw_html)
        # select_dropdown always returns a clean FormState
        mock_client._build_postback_data.return_value = {}
        mock_client._request.return_value = MagicMock(content=raw_html)

        MockClientClass = MagicMock(return_value=mock_client)
        monkeypatch.setattr(discovery_module, "SakarmaClient", MockClientClass)

        # parse_form_state → return a dummy FormState
        monkeypatch.setattr(
            discovery_module, "parse_form_state", lambda html, page_url: base_state
        )

        # parse_dropdown_options stubs:
        #   DDL_DISTRICT  → 2 districts
        #   DDL_LB_TYPE   → 2 LB types
        #   DDL_YEAR      → 1 year
        #   DDL_LB_NAME   → 3 LBs (same set for every combo)
        from sakarma.scraper.protocol import DDL_DISTRICT, DDL_LB_NAME, DDL_LB_TYPE, DDL_YEAR

        def _fake_parse(html_or_state, ddl_name):
            if ddl_name == DDL_DISTRICT:
                return [(1, "District A"), (2, "District B")]
            if ddl_name == DDL_LB_TYPE:
                return [(10, "Type X"), (20, "Type Y")]
            if ddl_name == DDL_YEAR:
                return [(100, "2024")]
            if ddl_name == DDL_LB_NAME:
                return [(1001, "LB 1"), (1002, "LB 2"), (1003, "LB 3")]
            return []

        monkeypatch.setattr(discovery_module, "parse_dropdown_options", _fake_parse)

        # --- Repository stubs ---
        mock_district_repo = MagicMock()
        mock_lb_type_repo = MagicMock()
        mock_year_repo = MagicMock()
        mock_lb_repo = MagicMock()
        mock_progress_repo = MagicMock()
        # bulk_create returns dummy list of 6 rows
        mock_progress_repo.bulk_create.return_value = [MagicMock() for _ in range(6)]

        MockDistrictRepository = MagicMock(return_value=mock_district_repo)
        MockLBTypeRepository = MagicMock(return_value=mock_lb_type_repo)
        MockYearRepository = MagicMock(return_value=mock_year_repo)
        MockLBRepository = MagicMock(return_value=mock_lb_repo)
        MockLBProgressRepository = MagicMock(return_value=mock_progress_repo)

        monkeypatch.setattr(discovery_module, "DistrictRepository", MockDistrictRepository)
        monkeypatch.setattr(discovery_module, "LBTypeRepository", MockLBTypeRepository)
        monkeypatch.setattr(discovery_module, "YearRepository", MockYearRepository)
        monkeypatch.setattr(discovery_module, "LBRepository", MockLBRepository)
        monkeypatch.setattr(discovery_module, "LBProgressRepository", MockLBProgressRepository)

        # --- get_session stub: yield a MagicMock session ---
        mock_session = MagicMock()

        @contextmanager
        def _fake_get_session() -> Generator:
            yield mock_session

        monkeypatch.setattr(discovery_module, "get_session", _fake_get_session)

        # --- get_rate_limiter stub ---
        monkeypatch.setattr(discovery_module, "get_rate_limiter", MagicMock(return_value=MagicMock()))

        # --- settings stub ---
        mock_settings = MagicMock()
        mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
        monkeypatch.setattr(discovery_module, "settings", mock_settings)

        return {
            "mock_client": mock_client,
            "mock_lb_repo": mock_lb_repo,
            "mock_progress_repo": mock_progress_repo,
            "mock_district_repo": mock_district_repo,
            "mock_lb_type_repo": mock_lb_type_repo,
            "mock_year_repo": mock_year_repo,
        }

    def test_lb_upsert_called_12_times(self, patched_env):
        """2 districts × 2 lb_types × 3 LBs = 12 LBRepository.upsert calls."""
        result = run.run(scrape_run_id=42)
        mock_lb_repo = patched_env["mock_lb_repo"]
        assert mock_lb_repo.upsert.call_count == 12

    def test_lb_upsert_called_with_correct_tuples(self, patched_env):
        """Verify a representative upsert call has correct kwargs."""
        run.run(scrape_run_id=42)
        mock_lb_repo = patched_env["mock_lb_repo"]
        all_calls = mock_lb_repo.upsert.call_args_list
        # Every call should pass scrape_run_id=42
        for c in all_calls:
            assert c.kwargs.get("scrape_run_id") == 42 or (
                len(c.args) >= 5 and c.args[4] == 42
            )

    def test_summary_dict_shape(self, patched_env):
        result = run.run(scrape_run_id=42)
        assert result["districts"] == 2
        assert result["lb_types"] == 2
        assert result["years"] == 1
        assert result["lbs"] == 3  # 12 upserts but only 3 unique LB ids
        assert result["lb_progress_rows"] == 6

    def test_district_upsert_called_twice(self, patched_env):
        run.run(scrape_run_id=42)
        assert patched_env["mock_district_repo"].upsert.call_count == 2

    def test_year_upsert_called_once(self, patched_env):
        run.run(scrape_run_id=42)
        assert patched_env["mock_year_repo"].upsert.call_count == 1
        patched_env["mock_year_repo"].upsert.assert_called_once_with(
            id=100, year_int=2024
        )

    def test_bulk_create_receives_unique_lb_ids(self, patched_env):
        run.run(scrape_run_id=42)
        mock_progress_repo = patched_env["mock_progress_repo"]
        assert mock_progress_repo.bulk_create.call_count == 1
        call_kwargs = mock_progress_repo.bulk_create.call_args
        scrape_run_id_arg = call_kwargs.kwargs.get("scrape_run_id") or call_kwargs.args[0]
        assert scrape_run_id_arg == 42


# =============================================================================
# Unit: edge — empty LB list for a (district, lb_type) pair doesn't crash
# =============================================================================


class TestRunEmptyLBList:
    @pytest.fixture
    def patched_env(self, monkeypatch):
        raw_html = _make_raw_html()
        base_state = _make_form_state()

        mock_client = MagicMock()
        mock_client._abs.return_value = "http://portal/page"
        mock_client._request.return_value = MagicMock(content=raw_html)
        mock_client._build_postback_data.return_value = {}
        MockClientClass = MagicMock(return_value=mock_client)
        monkeypatch.setattr(discovery_module, "SakarmaClient", MockClientClass)

        monkeypatch.setattr(
            discovery_module, "parse_form_state", lambda html, page_url: base_state
        )

        from sakarma.scraper.protocol import DDL_DISTRICT, DDL_LB_NAME, DDL_LB_TYPE, DDL_YEAR

        def _fake_parse(html_or_state, ddl_name):
            if ddl_name == DDL_DISTRICT:
                return [(1, "District A")]
            if ddl_name == DDL_LB_TYPE:
                return [(10, "Type X")]
            if ddl_name == DDL_YEAR:
                return [(100, "2024")]
            if ddl_name == DDL_LB_NAME:
                # Empty — simulates e.g. Wayanad+Corporation
                return []
            return []

        monkeypatch.setattr(discovery_module, "parse_dropdown_options", _fake_parse)

        mock_lb_repo = MagicMock()
        mock_progress_repo = MagicMock()
        mock_progress_repo.bulk_create.return_value = []

        monkeypatch.setattr(discovery_module, "DistrictRepository", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(discovery_module, "LBTypeRepository", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(discovery_module, "YearRepository", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(discovery_module, "LBRepository", MagicMock(return_value=mock_lb_repo))
        monkeypatch.setattr(discovery_module, "LBProgressRepository", MagicMock(return_value=mock_progress_repo))

        mock_session = MagicMock()

        @contextmanager
        def _fake_get_session() -> Generator:
            yield mock_session

        monkeypatch.setattr(discovery_module, "get_session", _fake_get_session)
        monkeypatch.setattr(discovery_module, "get_rate_limiter", MagicMock(return_value=MagicMock()))

        mock_settings = MagicMock()
        mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
        monkeypatch.setattr(discovery_module, "settings", mock_settings)

        return {"mock_lb_repo": mock_lb_repo, "mock_progress_repo": mock_progress_repo}

    def test_no_crash_on_empty_lb_list(self, patched_env):
        """Should complete without error."""
        result = run.run(scrape_run_id=99)
        assert result["lbs"] == 0

    def test_lb_upsert_not_called(self, patched_env):
        run.run(scrape_run_id=99)
        patched_env["mock_lb_repo"].upsert.assert_not_called()

    def test_lb_progress_rows_zero(self, patched_env):
        result = run.run(scrape_run_id=99)
        assert result["lb_progress_rows"] == 0


# =============================================================================
# Unit: idempotency — re-running with same scrape_run_id calls upserts again
#       but bulk_create is idempotent (ON CONFLICT DO NOTHING)
# =============================================================================


class TestRunIdempotency:
    @pytest.fixture
    def patched_env(self, monkeypatch):
        raw_html = _make_raw_html()
        base_state = _make_form_state()

        mock_client = MagicMock()
        mock_client._abs.return_value = "http://portal/page"
        mock_client._request.return_value = MagicMock(content=raw_html)
        mock_client._build_postback_data.return_value = {}
        MockClientClass = MagicMock(return_value=mock_client)
        monkeypatch.setattr(discovery_module, "SakarmaClient", MockClientClass)

        monkeypatch.setattr(
            discovery_module, "parse_form_state", lambda html, page_url: base_state
        )

        from sakarma.scraper.protocol import DDL_DISTRICT, DDL_LB_NAME, DDL_LB_TYPE, DDL_YEAR

        def _fake_parse(html_or_state, ddl_name):
            if ddl_name == DDL_DISTRICT:
                return [(1, "District A")]
            if ddl_name == DDL_LB_TYPE:
                return [(10, "Type X")]
            if ddl_name == DDL_YEAR:
                return [(100, "2024")]
            if ddl_name == DDL_LB_NAME:
                return [(500, "LB Z")]
            return []

        monkeypatch.setattr(discovery_module, "parse_dropdown_options", _fake_parse)

        mock_lb_repo = MagicMock()
        mock_progress_repo = MagicMock()
        # Second call returns same rows — simulating DB returning existing rows
        mock_progress_repo.bulk_create.return_value = [MagicMock()]

        monkeypatch.setattr(discovery_module, "DistrictRepository", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(discovery_module, "LBTypeRepository", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(discovery_module, "YearRepository", MagicMock(return_value=MagicMock()))
        monkeypatch.setattr(discovery_module, "LBRepository", MagicMock(return_value=mock_lb_repo))
        monkeypatch.setattr(discovery_module, "LBProgressRepository", MagicMock(return_value=mock_progress_repo))

        mock_session = MagicMock()

        @contextmanager
        def _fake_get_session() -> Generator:
            yield mock_session

        monkeypatch.setattr(discovery_module, "get_session", _fake_get_session)
        monkeypatch.setattr(discovery_module, "get_rate_limiter", MagicMock(return_value=MagicMock()))

        mock_settings = MagicMock()
        mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
        monkeypatch.setattr(discovery_module, "settings", mock_settings)

        return {
            "mock_lb_repo": mock_lb_repo,
            "mock_progress_repo": mock_progress_repo,
        }

    def test_second_run_still_calls_upsert(self, patched_env):
        """Re-running upserts LBs again (idempotent at DB level)."""
        run.run(scrape_run_id=7)
        run.run(scrape_run_id=7)
        assert patched_env["mock_lb_repo"].upsert.call_count == 2

    def test_bulk_create_called_both_times(self, patched_env):
        """bulk_create is called both times; DB skips existing rows silently."""
        run.run(scrape_run_id=7)
        run.run(scrape_run_id=7)
        assert patched_env["mock_progress_repo"].bulk_create.call_count == 2


# =============================================================================
# Integration: full discovery against a real DB (mock HTTP)
# =============================================================================


@pytest.mark.integration
class TestFullDiscoveryAgainstDB:
    """Uses db_session from conftest; SakarmaClient is fully mocked (no HTTP)."""

    @pytest.fixture
    def patched_http(self, monkeypatch, db_session):
        """Monkeypatch the HTTP layer; wire repositories to db_session."""
        raw_html = _make_raw_html()
        base_state = _make_form_state()

        mock_client = MagicMock()
        mock_client._abs.return_value = "http://portal/page"
        mock_client._request.return_value = MagicMock(content=raw_html)
        mock_client._build_postback_data.return_value = {}
        MockClientClass = MagicMock(return_value=mock_client)
        monkeypatch.setattr(discovery_module, "SakarmaClient", MockClientClass)

        monkeypatch.setattr(
            discovery_module, "parse_form_state", lambda html, page_url: base_state
        )

        from sakarma.scraper.protocol import DDL_DISTRICT, DDL_LB_NAME, DDL_LB_TYPE, DDL_YEAR

        def _fake_parse(html_or_state, ddl_name):
            if ddl_name == DDL_DISTRICT:
                return [(1, "തിരുവനന്തപുരം"), (2, "കൊല്ലം")]
            if ddl_name == DDL_LB_TYPE:
                return [(3, "Grama Panchayat"), (4, "Municipality")]
            if ddl_name == DDL_YEAR:
                return [(10, "2023-24"), (11, "2024-25")]
            if ddl_name == DDL_LB_NAME:
                # Return distinct LBs per combo to test unique-id dedup.
                # We key on the DDL_LB_TYPE value in form_fields — but since
                # form_fields is empty on our stub state, always return 2 LBs.
                return [(100, "LB Alpha"), (200, "LB Beta")]
            return []

        monkeypatch.setattr(discovery_module, "parse_dropdown_options", _fake_parse)
        monkeypatch.setattr(discovery_module, "get_rate_limiter", MagicMock(return_value=MagicMock()))

        mock_settings = MagicMock()
        mock_settings.scraper_lbwise_path = "/Pages/LBWiseDashBoard.aspx"
        monkeypatch.setattr(discovery_module, "settings", mock_settings)

        # Wire get_session to yield the test's transactional db_session.
        @contextmanager
        def _db_session_ctx() -> Generator:
            yield db_session

        monkeypatch.setattr(discovery_module, "get_session", _db_session_ctx)

        return db_session

    def test_full_discovery_against_db(self, patched_http):
        """Row counts in dimension + progress tables match expectations."""
        from sqlalchemy import func, select

        from sakarma.db.models import LB, District, LBProgress, LBType, Year

        db = patched_http
        scrape_run_id = 999

        result = run.run(scrape_run_id=scrape_run_id)

        # Summary dict
        assert result["districts"] == 2
        assert result["lb_types"] == 2
        assert result["years"] == 2
        # 2 dist × 2 lb_type × 2 LBs = 8, but LB ids {100, 200} repeat → 2 unique
        assert result["lbs"] == 2
        assert result["lb_progress_rows"] == 2

        # DB row counts
        assert db.execute(select(func.count(District.id))).scalar() == 2
        assert db.execute(select(func.count(LBType.id))).scalar() == 2
        assert db.execute(select(func.count(Year.id))).scalar() == 2
        assert db.execute(select(func.count(LB.id))).scalar() == 2

        progress_rows = db.execute(
            select(func.count(LBProgress.id)).where(
                LBProgress.scrape_run_id == scrape_run_id
            )
        ).scalar()
        assert progress_rows == 2

    def test_idempotent_rerun_against_db(self, patched_http):
        """Re-running with the same scrape_run_id doesn't create duplicate rows."""
        from sqlalchemy import func, select

        from sakarma.db.models import LB, LBProgress

        db = patched_http
        run.run(scrape_run_id=888)
        run.run(scrape_run_id=888)

        lb_count = db.execute(select(func.count(LB.id))).scalar()
        progress_count = db.execute(
            select(func.count(LBProgress.id)).where(LBProgress.scrape_run_id == 888)
        ).scalar()
        # Idempotent: same counts both times
        assert lb_count == 2
        assert progress_count == 2
