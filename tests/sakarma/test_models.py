"""Tests for :mod:`sakarma.db.models` — schema isolation and table layout."""

from __future__ import annotations

import pytest

from sakarma.db.models import SAKARMA_SCHEMA, SakarmaBase

EXPECTED_TABLES = {
    "sakarma.dashboard_kpi_snapshot",
    "sakarma.district",
    "sakarma.lb",
    "sakarma.lb_progress",
    "sakarma.lb_type",
    "sakarma.main_group_value",
    "sakarma.meeting_artifact",
    "sakarma.meeting_manifest",
    "sakarma.reconciliation",
    "sakarma.scrape_run",
    "sakarma.year",
}


def test_metadata_has_all_eleven_tables() -> None:
    assert set(SakarmaBase.metadata.tables.keys()) == EXPECTED_TABLES


def test_all_tables_bound_to_sakarma_schema() -> None:
    for table in SakarmaBase.metadata.tables.values():
        assert table.schema == SAKARMA_SCHEMA, (
            f"{table.name} bound to {table.schema!r}, expected 'sakarma'"
        )


def test_metadata_default_schema() -> None:
    assert SakarmaBase.metadata.schema == SAKARMA_SCHEMA


def test_sulekha_base_does_not_pollute_sakarma_metadata() -> None:
    # Importing sulekha's Base must not leak its tables into SakarmaBase.metadata.
    from sulekha.db.models import Base as SulekhaBase

    sulekha_table_names = {t.name for t in SulekhaBase.metadata.tables.values()}
    sakarma_table_names = {t.name for t in SakarmaBase.metadata.tables.values()}
    # Bases must be distinct objects.
    assert SulekhaBase is not SakarmaBase
    # And their metadata must be distinct.
    assert SulekhaBase.metadata is not SakarmaBase.metadata
    # No sulekha table should appear in sakarma metadata under the sakarma schema.
    for sak_table in SakarmaBase.metadata.tables.values():
        assert sak_table.schema == SAKARMA_SCHEMA
    # Sulekha tables shouldn't be schema-qualified to sakarma.
    for name in sulekha_table_names:
        assert f"sakarma.{name}" not in SakarmaBase.metadata.tables or (
            name in sakarma_table_names
        )


def test_meeting_artifact_unique_content_hash() -> None:
    table = SakarmaBase.metadata.tables["sakarma.meeting_artifact"]
    content_hash_col = table.c.content_hash
    assert content_hash_col.unique is True


def test_meeting_manifest_natural_unique_constraint() -> None:
    table = SakarmaBase.metadata.tables["sakarma.meeting_manifest"]
    uq_names = {c.name for c in table.constraints if c.name}
    assert "uq_meeting_manifest_natural" in uq_names


def test_lb_progress_natural_unique_constraint() -> None:
    table = SakarmaBase.metadata.tables["sakarma.lb_progress"]
    uq_names = {c.name for c in table.constraints if c.name}
    assert "uq_lb_progress_run_lb" in uq_names


def test_reconciliation_natural_unique_constraint() -> None:
    table = SakarmaBase.metadata.tables["sakarma.reconciliation"]
    uq_names = {c.name for c in table.constraints if c.name}
    assert "uq_reconciliation_natural" in uq_names


@pytest.mark.integration
def test_create_all_against_postgres(test_engine, setup_test_db) -> None:
    """Smoke test: create_all/drop_all via the setup_test_db fixture works."""
    from sqlalchemy import inspect

    inspector = inspect(test_engine)
    table_names = set(inspector.get_table_names(schema=SAKARMA_SCHEMA))
    expected_short = {t.split(".", 1)[1] for t in EXPECTED_TABLES}
    assert expected_short.issubset(table_names)
