"""Integration tests for the SAKARMA alembic migration environment.

All tests require a real PostgreSQL instance and are therefore marked
``@pytest.mark.integration``.  They are skipped automatically when Postgres
is not reachable (the ``sakarma_migration_db_url`` fixture calls pytest.skip).

Tests use ``alembic.command`` / ``alembic.config.Config`` for programmatic
invocation — no subprocess calls.

Run with:
    pytest -m integration tests/sakarma/test_migrations.py
"""

from __future__ import annotations

import os
from typing import Generator

import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INI_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "alembic_sakarma.ini")
_INI_PATH = os.path.normpath(_INI_PATH)

EXPECTED_TABLES = {
    "district",
    "lb_type",
    "year",
    "scrape_run",
    "lb",
    "main_group_value",
    "lb_progress",
    "dashboard_kpi_snapshot",
    "meeting_manifest",
    "meeting_artifact",
    "reconciliation",
}

EXPECTED_ENUMS = {
    "sakarma_scrape_run_kind",
    "sakarma_scrape_run_status",
    "sakarma_lb_progress_status",
    "sakarma_lb_progress_stage",
    "sakarma_artifact_type",
    "sakarma_recon_status",
}


def _make_alembic_config(db_url: str) -> AlembicConfig:
    """Build a programmatic AlembicConfig for alembic_sakarma.ini."""
    cfg = AlembicConfig(_INI_PATH)
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def _get_sakarma_tables(conn) -> set[str]:
    """Return the set of table names in the sakarma schema."""
    rows = conn.execute(
        text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'sakarma'"
        )
    ).fetchall()
    return {r[0] for r in rows}


def _schema_exists(conn) -> bool:
    """Return True if the sakarma schema exists in pg_namespace."""
    result = conn.execute(
        text("SELECT 1 FROM pg_namespace WHERE nspname = 'sakarma'")
    ).scalar()
    return result is not None


def _get_enum_names(conn) -> set[str]:
    """Return ENUM type names defined inside the sakarma schema."""
    rows = conn.execute(
        text(
            "SELECT t.typname FROM pg_type t "
            "JOIN pg_namespace n ON t.typnamespace = n.oid "
            "WHERE n.nspname = 'sakarma' AND t.typtype = 'e'"
        )
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sakarma_migration_db_url() -> str:
    """Return the SAKARMA test DB URL, skipping if Postgres is unreachable."""
    url = os.environ.get(
        "SAKARMA_DATABASE_URL",
        "postgresql+psycopg://sulekha:sulekha@localhost:5432/sakarma_test",
    )
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        engine.dispose()
        pytest.skip(f"PostgreSQL not available: {exc}")
    engine.dispose()
    return url


@pytest.fixture(autouse=True)
def _clean_sakarma_schema(sakarma_migration_db_url: str) -> Generator[None, None, None]:
    """Ensure the sakarma schema is absent before each test and cleaned up after."""
    engine = create_engine(sakarma_migration_db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS sakarma CASCADE"))
    engine.dispose()

    yield

    engine = create_engine(sakarma_migration_db_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS sakarma CASCADE"))
    engine.dispose()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_upgrade_head_creates_schema_and_all_tables(sakarma_migration_db_url: str) -> None:
    """upgrade head creates the sakarma schema and all 11 expected tables."""
    cfg = _make_alembic_config(sakarma_migration_db_url)
    alembic_command.upgrade(cfg, "head")

    engine = create_engine(sakarma_migration_db_url)
    try:
        with engine.connect() as conn:
            assert _schema_exists(conn), "sakarma schema was not created"

            tables = _get_sakarma_tables(conn)
            # alembic_version lives in sakarma schema too — exclude it
            tables.discard("alembic_version")
            assert tables == EXPECTED_TABLES, (
                f"Table mismatch.\nExpected: {sorted(EXPECTED_TABLES)}\nGot:      {sorted(tables)}"
            )

            enums = _get_enum_names(conn)
            assert enums == EXPECTED_ENUMS, (
                f"ENUM mismatch.\nExpected: {sorted(EXPECTED_ENUMS)}\nGot:      {sorted(enums)}"
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_downgrade_base_removes_schema(sakarma_migration_db_url: str) -> None:
    """downgrade base cleanly removes all tables and the sakarma schema."""
    cfg = _make_alembic_config(sakarma_migration_db_url)
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "base")

    engine = create_engine(sakarma_migration_db_url)
    try:
        with engine.connect() as conn:
            assert not _schema_exists(conn), (
                "sakarma schema still exists after downgrade to base"
            )
    finally:
        engine.dispose()


@pytest.mark.integration
def test_upgrade_twice_is_idempotent(sakarma_migration_db_url: str) -> None:
    """Running upgrade head twice leaves the DB in the same state (no error)."""
    cfg = _make_alembic_config(sakarma_migration_db_url)
    alembic_command.upgrade(cfg, "head")
    # Second call must not raise; alembic_version table prevents re-applying
    alembic_command.upgrade(cfg, "head")

    engine = create_engine(sakarma_migration_db_url)
    try:
        with engine.connect() as conn:
            tables = _get_sakarma_tables(conn)
            tables.discard("alembic_version")
            assert tables == EXPECTED_TABLES
    finally:
        engine.dispose()


@pytest.mark.integration
def test_upgrade_downgrade_symmetry(sakarma_migration_db_url: str) -> None:
    """upgrade followed by downgrade leaves the database in the original state."""
    engine = create_engine(sakarma_migration_db_url)

    # Capture baseline: no sakarma schema
    with engine.connect() as conn:
        schema_before = _schema_exists(conn)

    cfg = _make_alembic_config(sakarma_migration_db_url)
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "base")

    with engine.connect() as conn:
        schema_after = _schema_exists(conn)

    engine.dispose()

    assert schema_before == schema_after == False, (  # noqa: E712
        "Schema existence mismatch: upgrade/downgrade cycle is not symmetric"
    )


@pytest.mark.integration
def test_sulekha_public_schema_unaffected(sakarma_migration_db_url: str) -> None:
    """Running sakarma migrations does not touch the sulekha public schema or tables.

    This test queries the sulekha database (same cluster, same DB for test
    environments) to confirm the public schema and any pre-existing tables
    are untouched.  It does not create any sulekha tables itself — it simply
    asserts that the sakarma migration did not DROP or ALTER anything in
    the public schema.
    """
    engine = create_engine(sakarma_migration_db_url)

    # Record public-schema table set before migration
    with engine.connect() as conn:
        pre_rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        ).fetchall()
    pre_tables = {r[0] for r in pre_rows}

    cfg = _make_alembic_config(sakarma_migration_db_url)
    alembic_command.upgrade(cfg, "head")
    alembic_command.downgrade(cfg, "base")

    # Record public-schema table set after migration cycle
    with engine.connect() as conn:
        post_rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        ).fetchall()
    post_tables = {r[0] for r in post_rows}
    engine.dispose()

    assert pre_tables == post_tables, (
        f"public schema tables changed during sakarma migration cycle.\n"
        f"Added:   {post_tables - pre_tables}\n"
        f"Removed: {pre_tables - post_tables}"
    )


@pytest.mark.integration
def test_alembic_version_lives_in_sakarma_schema(sakarma_migration_db_url: str) -> None:
    """The alembic_version tracking table is stored inside the sakarma schema."""
    cfg = _make_alembic_config(sakarma_migration_db_url)
    alembic_command.upgrade(cfg, "head")

    engine = create_engine(sakarma_migration_db_url)
    try:
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'sakarma' AND table_name = 'alembic_version'"
                )
            ).scalar()
            assert result == "alembic_version", (
                "alembic_version table not found in sakarma schema"
            )

            # Confirm it is NOT in the public schema
            result_public = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name = 'alembic_version'"
                )
            ).scalar()
            assert result_public is None, (
                "alembic_version unexpectedly found in public schema"
            )
    finally:
        engine.dispose()
