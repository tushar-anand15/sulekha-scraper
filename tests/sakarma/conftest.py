"""Pytest configuration and shared fixtures for SAKARMA tests.

Mirrors the env-vars-set-before-import pattern from ``tests/conftest.py``,
but uses ``SAKARMA_*`` variables and a separate ``sakarma_test`` Postgres
database with the ``sakarma`` schema.

Subsequent units add fixtures (db_session, storage, mock_responses,
HTML golden fixtures). This skeleton bootstraps env vars only so each
unit's tests can import ``sakarma.*`` modules cleanly.
"""

import os
from typing import Generator
from unittest.mock import MagicMock
from urllib.parse import urlparse, urlunparse

import pytest

# Set test environment with fallbacks BEFORE any sakarma module imports
os.environ.setdefault("SAKARMA_STORAGE_BACKEND", "s3")
os.environ.setdefault("SAKARMA_S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("SAKARMA_S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("SAKARMA_S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("SAKARMA_S3_BUCKET_NAME", "sakarma-test")
os.environ.setdefault("SAKARMA_GCS_BUCKET_NAME", "sakarma-test")
os.environ.setdefault(
    "SAKARMA_DATABASE_URL",
    "postgresql+psycopg://sulekha:sulekha@localhost:5432/sakarma_test",
)
os.environ.setdefault("SAKARMA_REDIS_URL", "redis://localhost:6379/2")
os.environ.setdefault("SAKARMA_LOG_FORMAT", "console")
os.environ.setdefault("SAKARMA_LOG_LEVEL", "DEBUG")
os.environ.setdefault("SAKARMA_RATE_LIMIT_ENABLED", "false")


# =============================================================================
# Database Fixtures (Unit 2)
# =============================================================================


def _ensure_test_database_exists() -> None:
    """Create the ``sakarma_test`` Postgres database if it doesn't exist.

    Connects to the cluster's ``postgres`` maintenance DB to create the test
    DB. Silently no-ops if Postgres isn't reachable; the fixture below will
    detect that and skip dependent tests.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError, ProgrammingError

    target_url = os.environ["SAKARMA_DATABASE_URL"]
    parsed = urlparse(target_url)
    target_db = parsed.path.lstrip("/") or "sakarma_test"
    admin_url = urlunparse(parsed._replace(path="/postgres"))

    try:
        admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": target_db},
            ).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{target_db}"'))
        admin_engine.dispose()
    except (OperationalError, ProgrammingError):
        # Postgres not reachable / insufficient perms: tests will be skipped.
        return


@pytest.fixture(scope="session")
def test_engine():
    """Session-scoped engine bound to ``SAKARMA_DATABASE_URL``.

    Skips the entire test if PostgreSQL isn't reachable.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import OperationalError

    _ensure_test_database_exists()

    url = os.environ["SAKARMA_DATABASE_URL"]
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as e:
        engine.dispose()
        pytest.skip(f"PostgreSQL not available: {e}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def setup_test_db(test_engine) -> Generator[None, None, None]:
    """Create the ``sakarma`` schema and all SAKARMA tables; tear down on exit."""
    from sqlalchemy import text

    from sakarma.db.models import SakarmaBase

    with test_engine.begin() as conn:
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS sakarma"))
    SakarmaBase.metadata.create_all(bind=test_engine)
    try:
        yield
    finally:
        SakarmaBase.metadata.drop_all(bind=test_engine)
        with test_engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS sakarma CASCADE"))


@pytest.fixture
def db_session(test_engine, setup_test_db) -> Generator:
    """Per-test transactional session that rolls back on teardown."""
    from sqlalchemy.orm import Session

    connection = test_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


# =============================================================================
# Generic mock fixtures available to every test module
# =============================================================================


@pytest.fixture
def mock_storage():
    """Provide a mock storage for unit tests that don't need real storage."""
    mock = MagicMock()
    mock.bucket_name = "sakarma-test"
    mock.upload_document.return_value = ("artifacts/test/path", "abc123hash", 1000)
    mock.download.return_value = b"<html>mock</html>"
    mock.exists.return_value = True
    mock.delete.return_value = True
    mock.list_objects.return_value = []
    return mock


@pytest.fixture
def mock_rate_limiter():
    """Provide a no-op rate limiter for tests that don't exercise concurrency."""
    from contextlib import contextmanager

    class _Stub:
        @contextmanager
        def acquire(self):
            yield

        def get_stats(self):
            return {"enabled": False}

    return _Stub()
