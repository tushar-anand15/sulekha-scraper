"""Tests for sakarma.config module."""

import os

import pytest


def test_settings_loads_with_sakarma_prefix(monkeypatch):
    """SAKARMA_* env vars populate the Settings instance."""
    monkeypatch.setenv("SAKARMA_GCS_BUCKET_NAME", "fixture-bucket")
    monkeypatch.setenv("SAKARMA_DATABASE_URL", "postgresql+psycopg://x:y@h:5432/d")
    monkeypatch.setenv("SAKARMA_RATE_LIMIT_MAX_CONCURRENT", "7")

    # Bypass the module-level lru_cache by importing fresh
    from sakarma.config import Settings

    s = Settings()
    assert s.gcs_bucket_name == "fixture-bucket"
    assert s.database_url == "postgresql+psycopg://x:y@h:5432/d"
    assert s.rate_limit_max_concurrent == 7


def test_settings_uses_meeting_lsgkerala_default():
    """The scraper base URL points at the SAKARMA portal by default."""
    from sakarma.config import Settings

    s = Settings()
    assert s.scraper_base_url == "https://meeting.lsgkerala.gov.in"
    assert s.scraper_lbwise_path == "/Pages/LBWiseDashBoard.aspx"
    assert s.scraper_minutes_path == "/Pages/PublicMinutes.aspx"
    assert s.scraper_dregister_path == "/Pages/PublicDRegister.aspx"


def test_settings_does_not_consume_sulekha_prefix(monkeypatch, tmp_path):
    """Settings ignores SULEKHA_*-prefixed env vars (no cross-tenant leakage).

    Isolated from the repo's .env file by chdir-ing into a tmp directory so
    pydantic-settings doesn't pick up SAKARMA_* values from dev configuration.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql://wrong:wrong@h/d")
    monkeypatch.setenv("GCS_BUCKET_NAME", "wrong-bucket")
    monkeypatch.delenv("SAKARMA_DATABASE_URL", raising=False)
    monkeypatch.delenv("SAKARMA_GCS_BUCKET_NAME", raising=False)
    monkeypatch.chdir(tmp_path)

    from sakarma.config import Settings

    s = Settings()
    # Defaults, not the un-prefixed env values
    assert s.database_url.endswith("/sulekha")  # default DSN
    assert s.gcs_bucket_name == ""  # default empty (must be set via SAKARMA_*)
