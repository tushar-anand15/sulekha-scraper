"""Repository layer for the SAKARMA scraper.

One repository class per entity. All upserts use Postgres
``INSERT ... ON CONFLICT DO UPDATE`` (or ``DO NOTHING`` + SELECT) on the
natural unique keys declared in :mod:`sakarma.db.models` so every operation
is idempotent across retries.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from sakarma.db.models import (
    LB,
    DashboardKPISnapshot,
    District,
    LBProgress,
    LBType,
    MainGroupValue,
    MeetingArtifact,
    MeetingManifest,
    Reconciliation,
    ScrapeRun,
    Year,
)

logger = structlog.get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# =============================================================================
# Dimension repositories
# =============================================================================


class DistrictRepository:
    """Repository for :class:`District` (PK = ddlDistrict value)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, id: int, name_ml: str, name_en: Optional[str] = None) -> District:
        stmt = insert(District).values(id=id, name_ml=name_ml, name_en=name_en)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"], set_={"name_ml": name_ml, "name_en": name_en}
        )
        self.session.execute(stmt)
        self.session.flush()
        return self.session.get(District, id)

    def get(self, district_id: int) -> Optional[District]:
        return self.session.get(District, district_id)


class LBTypeRepository:
    """Repository for :class:`LBType` (PK = ddlLBType value)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, id: int, name_ml: str, name_en: Optional[str] = None) -> LBType:
        stmt = insert(LBType).values(id=id, name_ml=name_ml, name_en=name_en)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"], set_={"name_ml": name_ml, "name_en": name_en}
        )
        self.session.execute(stmt)
        self.session.flush()
        return self.session.get(LBType, id)

    def get(self, lb_type_id: int) -> Optional[LBType]:
        return self.session.get(LBType, lb_type_id)


class YearRepository:
    """Repository for :class:`Year` (PK = ddlYear value, year_int = calendar year)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, id: int, year_int: int) -> Year:
        stmt = insert(Year).values(id=id, year_int=year_int)
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"], set_={"year_int": year_int}
        )
        self.session.execute(stmt)
        self.session.flush()
        return self.session.get(Year, id)

    def get(self, year_id: int) -> Optional[Year]:
        return self.session.get(Year, year_id)

    def get_by_year_int(self, year_int: int) -> Optional[Year]:
        stmt = select(Year).where(Year.year_int == year_int)
        return self.session.execute(stmt).scalar_one_or_none()


class LBRepository:
    """Repository for :class:`LB` (PK = ddlLBName value)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        id: int,
        district_id: int,
        lb_type_id: int,
        name_ml: str,
        scrape_run_id: Optional[int] = None,
    ) -> LB:
        stmt = insert(LB).values(
            id=id,
            district_id=district_id,
            lb_type_id=lb_type_id,
            name_ml=name_ml,
            scrape_run_id=scrape_run_id,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "district_id": district_id,
                "lb_type_id": lb_type_id,
                "name_ml": name_ml,
            },
        )
        self.session.execute(stmt)
        self.session.flush()
        return self.session.get(LB, id)

    def get(self, lb_id: int) -> Optional[LB]:
        return self.session.get(LB, lb_id)

    def list_all(self) -> list[LB]:
        """Return all LBs ordered by id."""
        stmt = select(LB).order_by(LB.id)
        return list(self.session.execute(stmt).scalars().all())

    def list_by_district(self, district_id: int) -> list[LB]:
        """Return all LBs belonging to the given district, ordered by id."""
        stmt = select(LB).where(LB.district_id == district_id).order_by(LB.id)
        return list(self.session.execute(stmt).scalars().all())


class MainGroupValueRepository:
    """Repository for :class:`MainGroupValue` (per-LB Main Group ddl values)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, lb_id: int, ddl_value: int, name_ml: str) -> MainGroupValue:
        stmt = insert(MainGroupValue).values(
            lb_id=lb_id, ddl_value=ddl_value, name_ml=name_ml
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["lb_id", "ddl_value"], set_={"name_ml": name_ml}
        )
        self.session.execute(stmt)
        self.session.flush()
        return self.get_by_natural_key(lb_id, ddl_value)

    def get(self, main_group_value_id: int) -> Optional[MainGroupValue]:
        """Fetch a :class:`MainGroupValue` by its surrogate PK."""
        return self.session.get(MainGroupValue, main_group_value_id)

    def get_by_natural_key(
        self, lb_id: int, ddl_value: int
    ) -> Optional[MainGroupValue]:
        stmt = select(MainGroupValue).where(
            MainGroupValue.lb_id == lb_id, MainGroupValue.ddl_value == ddl_value
        )
        return self.session.execute(stmt).scalar_one_or_none()


# =============================================================================
# Operational repositories
# =============================================================================


class ScrapeRunRepository:
    """Repository for :class:`ScrapeRun` lifecycle."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, kind: str) -> ScrapeRun:
        run = ScrapeRun(kind=kind, status="running", started_at=_utcnow())
        self.session.add(run)
        self.session.flush()
        return run

    def create_backfill(self) -> ScrapeRun:
        """Create a new backfill scrape run (status=running)."""
        return self.create(kind="backfill")

    def create_diff(self) -> ScrapeRun:
        """Create a new diff scrape run (status=running)."""
        return self.create(kind="diff")

    def list_recent(self, limit: int = 10) -> list[ScrapeRun]:
        """Return the most recent scrape runs, newest first."""
        stmt = (
            select(ScrapeRun)
            .order_by(ScrapeRun.started_at.desc())
            .limit(limit)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get(self, run_id: int) -> Optional[ScrapeRun]:
        return self.session.get(ScrapeRun, run_id)

    def mark_done(self, run_id: int) -> None:
        stmt = (
            update(ScrapeRun)
            .where(ScrapeRun.id == run_id)
            .values(status="done", completed_at=_utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_failed(
        self, run_id: int, error_summary: Optional[dict] = None
    ) -> None:
        stmt = (
            update(ScrapeRun)
            .where(ScrapeRun.id == run_id)
            .values(
                status="failed",
                completed_at=_utcnow(),
                error_summary=error_summary,
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()


class LBProgressRepository:
    """Repository for :class:`LBProgress` with explicit state-machine helpers."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def bulk_create(
        self, scrape_run_id: int, lb_ids: Iterable[int]
    ) -> list[LBProgress]:
        rows = [
            {"scrape_run_id": scrape_run_id, "lb_id": lb_id, "status": "pending"}
            for lb_id in lb_ids
        ]
        if not rows:
            return []
        stmt = insert(LBProgress).values(rows)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["scrape_run_id", "lb_id"]
        )
        self.session.execute(stmt)
        self.session.flush()
        return self.list_for_run(scrape_run_id)

    def list_for_run(self, scrape_run_id: int) -> list[LBProgress]:
        stmt = (
            select(LBProgress)
            .where(LBProgress.scrape_run_id == scrape_run_id)
            .order_by(LBProgress.lb_id)
        )
        return list(self.session.execute(stmt).scalars().all())

    def get(self, lb_progress_id: int) -> Optional[LBProgress]:
        return self.session.get(LBProgress, lb_progress_id)

    def get_by_run_lb(
        self, scrape_run_id: int, lb_id: int
    ) -> Optional[LBProgress]:
        """Fetch the progress row for a specific (scrape_run_id, lb_id) pair."""
        stmt = select(LBProgress).where(
            LBProgress.scrape_run_id == scrape_run_id,
            LBProgress.lb_id == lb_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def mark_in_progress(self, lb_progress_id: int) -> None:
        stmt = (
            update(LBProgress)
            .where(LBProgress.id == lb_progress_id)
            .values(status="in_progress", started_at=_utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_stage(self, lb_progress_id: int, stage: str) -> None:
        stmt = (
            update(LBProgress)
            .where(LBProgress.id == lb_progress_id)
            .values(current_stage=stage)
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_done(self, lb_progress_id: int) -> None:
        stmt = (
            update(LBProgress)
            .where(LBProgress.id == lb_progress_id)
            .values(status="done", completed_at=_utcnow())
        )
        self.session.execute(stmt)
        self.session.expire_all()

    def mark_error(self, lb_progress_id: int, error_message: str) -> None:
        stmt = (
            update(LBProgress)
            .where(LBProgress.id == lb_progress_id)
            .values(
                status="error",
                error_message=error_message,
                completed_at=_utcnow(),
            )
        )
        self.session.execute(stmt)
        self.session.expire_all()


# =============================================================================
# Universe repositories
# =============================================================================


class DashboardKPISnapshotRepository:
    """Repository for :class:`DashboardKPISnapshot`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        lb_id: int,
        year_id: int,
        main_group_value_id: int,
        scrape_run_id: int,
        total_meetings: int,
        ongoing: int,
        minutes_complete: int,
        minutes_incomplete: int,
        cancelled: int,
        snapshot_html_hash: Optional[str] = None,
        snapshot_html_gcs_path: Optional[str] = None,
    ) -> DashboardKPISnapshot:
        values: dict[str, Any] = {
            "lb_id": lb_id,
            "year_id": year_id,
            "main_group_value_id": main_group_value_id,
            "scrape_run_id": scrape_run_id,
            "total_meetings": total_meetings,
            "ongoing": ongoing,
            "minutes_complete": minutes_complete,
            "minutes_incomplete": minutes_incomplete,
            "cancelled": cancelled,
            "snapshot_html_hash": snapshot_html_hash,
            "snapshot_html_gcs_path": snapshot_html_gcs_path,
            "fetched_at": _utcnow(),
        }
        stmt = insert(DashboardKPISnapshot).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "lb_id",
                "year_id",
                "main_group_value_id",
                "scrape_run_id",
            ],
            set_={
                "total_meetings": total_meetings,
                "ongoing": ongoing,
                "minutes_complete": minutes_complete,
                "minutes_incomplete": minutes_incomplete,
                "cancelled": cancelled,
                "snapshot_html_hash": snapshot_html_hash,
                "snapshot_html_gcs_path": snapshot_html_gcs_path,
                "fetched_at": _utcnow(),
            },
        )
        self.session.execute(stmt)
        self.session.flush()
        return self._get(lb_id, year_id, main_group_value_id, scrape_run_id)

    def _get(
        self,
        lb_id: int,
        year_id: int,
        main_group_value_id: int,
        scrape_run_id: int,
    ) -> Optional[DashboardKPISnapshot]:
        stmt = select(DashboardKPISnapshot).where(
            DashboardKPISnapshot.lb_id == lb_id,
            DashboardKPISnapshot.year_id == year_id,
            DashboardKPISnapshot.main_group_value_id == main_group_value_id,
            DashboardKPISnapshot.scrape_run_id == scrape_run_id,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_lb_run(
        self, lb_id: int, scrape_run_id: int
    ) -> list[DashboardKPISnapshot]:
        """Return all KPI snapshots for a given (lb_id, scrape_run_id) pair."""
        stmt = (
            select(DashboardKPISnapshot)
            .where(
                DashboardKPISnapshot.lb_id == lb_id,
                DashboardKPISnapshot.scrape_run_id == scrape_run_id,
            )
            .order_by(
                DashboardKPISnapshot.year_id,
                DashboardKPISnapshot.main_group_value_id,
            )
        )
        return list(self.session.execute(stmt).scalars().all())


class MeetingManifestRepository:
    """Repository for :class:`MeetingManifest` rows."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, rows: Iterable[dict[str, Any]]) -> int:
        """Bulk-upsert manifest rows. Returns number of rows submitted.

        Each row dict must include the natural key columns
        (lb_id, year_id, main_group_value_id, category, meeting_no_label,
        meeting_date) plus the remaining manifest fields. ``last_seen_at`` is
        always refreshed; ``first_seen_at`` is preserved on conflict.
        """
        rows = list(rows)
        if not rows:
            return 0

        now = _utcnow()
        for row in rows:
            row.setdefault("first_seen_at", now)
            row["last_seen_at"] = now

        stmt = insert(MeetingManifest).values(rows)
        update_set = {
            "dashboard_grid_select_index": stmt.excluded.dashboard_grid_select_index,
            "dr_postback_target": stmt.excluded.dr_postback_target,
            "meeting_type": stmt.excluded.meeting_type,
            "meeting_nature": stmt.excluded.meeting_nature,
            "meeting_venue": stmt.excluded.meeting_venue,
            "last_seen_at": stmt.excluded.last_seen_at,
            "scrape_run_id": stmt.excluded.scrape_run_id,
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "lb_id",
                "year_id",
                "main_group_value_id",
                "category",
                "meeting_no_label",
                "meeting_date",
            ],
            set_=update_set,
        )
        self.session.execute(stmt)
        self.session.flush()
        return len(rows)

    def get_by_natural_key(
        self,
        lb_id: int,
        year_id: int,
        main_group_value_id: int,
        category: int,
        meeting_no_label: str,
        meeting_date: date,
    ) -> Optional[MeetingManifest]:
        stmt = select(MeetingManifest).where(
            MeetingManifest.lb_id == lb_id,
            MeetingManifest.year_id == year_id,
            MeetingManifest.main_group_value_id == main_group_value_id,
            MeetingManifest.category == category,
            MeetingManifest.meeting_no_label == meeting_no_label,
            MeetingManifest.meeting_date == meeting_date,
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def count_by_lb_year_group_category(
        self,
        lb_id: int,
        year_id: int,
        main_group_value_id: int,
        category: int,
    ) -> int:
        stmt = select(func.count(MeetingManifest.id)).where(
            MeetingManifest.lb_id == lb_id,
            MeetingManifest.year_id == year_id,
            MeetingManifest.main_group_value_id == main_group_value_id,
            MeetingManifest.category == category,
        )
        return int(self.session.execute(stmt).scalar() or 0)

    def list_approved_for_lb_run(
        self, lb_id: int, scrape_run_id: int
    ) -> list[MeetingManifest]:
        """Return all Approved-category manifest rows for (lb_id, scrape_run_id).

        Ordered by (year_id, main_group_value_id, dashboard_grid_select_index)
        to mirror the iteration order used during manifest collection.
        """
        from sakarma.db.models import CATEGORY_APPROVED as _CAT_APPROVED

        stmt = (
            select(MeetingManifest)
            .where(
                MeetingManifest.lb_id == lb_id,
                MeetingManifest.scrape_run_id == scrape_run_id,
                MeetingManifest.category == _CAT_APPROVED,
            )
            .order_by(
                MeetingManifest.year_id,
                MeetingManifest.main_group_value_id,
                MeetingManifest.dashboard_grid_select_index,
            )
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_approved_cells_for_lb_run(
        self, lb_id: int, scrape_run_id: int
    ) -> list[tuple[int, int]]:
        """Distinct ``(year_id, main_group_value_id)`` cells that have at
        least one Approved manifest row for this (lb, run). Used by the
        orchestrator to fan out one cell-task per cell.
        """
        from sakarma.db.models import CATEGORY_APPROVED as _CAT_APPROVED

        stmt = (
            select(
                MeetingManifest.year_id,
                MeetingManifest.main_group_value_id,
            )
            .where(
                MeetingManifest.lb_id == lb_id,
                MeetingManifest.scrape_run_id == scrape_run_id,
                MeetingManifest.category == _CAT_APPROVED,
            )
            .distinct()
            .order_by(
                MeetingManifest.year_id, MeetingManifest.main_group_value_id
            )
        )
        return [
            (int(r.year_id), int(r.main_group_value_id))
            for r in self.session.execute(stmt).all()
        ]

    def list_approved_for_cell(
        self,
        lb_id: int,
        scrape_run_id: int,
        year_id: int,
        main_group_value_id: int,
    ) -> list[MeetingManifest]:
        """Approved manifest rows for a single cell, ordered by row index."""
        from sakarma.db.models import CATEGORY_APPROVED as _CAT_APPROVED

        stmt = (
            select(MeetingManifest)
            .where(
                MeetingManifest.lb_id == lb_id,
                MeetingManifest.scrape_run_id == scrape_run_id,
                MeetingManifest.category == _CAT_APPROVED,
                MeetingManifest.year_id == year_id,
                MeetingManifest.main_group_value_id == main_group_value_id,
            )
            .order_by(MeetingManifest.dashboard_grid_select_index)
        )
        return list(self.session.execute(stmt).scalars().all())

    def list_groups_for_lb_run(
        self, lb_id: int, scrape_run_id: int
    ) -> list[tuple[int, int, int]]:
        """Return distinct (year_id, main_group_value_id, category) tuples
        for manifest rows belonging to a given (lb_id, scrape_run_id).

        Used by reconciliation to detect manifest groups that have no
        corresponding KPI snapshot.
        """
        stmt = (
            select(
                MeetingManifest.year_id,
                MeetingManifest.main_group_value_id,
                MeetingManifest.category,
            )
            .where(
                MeetingManifest.lb_id == lb_id,
                MeetingManifest.scrape_run_id == scrape_run_id,
            )
            .distinct()
            .order_by(
                MeetingManifest.year_id,
                MeetingManifest.main_group_value_id,
                MeetingManifest.category,
            )
        )
        return [
            (int(row.year_id), int(row.main_group_value_id), int(row.category))
            for row in self.session.execute(stmt).all()
        ]


# =============================================================================
# Artifact repository
# =============================================================================


class MeetingArtifactRepository:
    """Repository for :class:`MeetingArtifact`.

    De-dupes on ``content_hash`` (UNIQUE). On hash collision returns the
    existing row instead of raising — captures the get-or-create pattern.
    """

    def __init__(self, session: Session) -> None:
        self.session = session

    def get_or_create(
        self,
        meeting_manifest_id: int,
        artifact_type: str,
        content_hash: str,
        gcs_path: str,
        byte_size: int,
        source_page_url: str,
        scrape_run_id: int,
        decision_index: Optional[int] = None,
        original_filename: Optional[str] = None,
    ) -> MeetingArtifact:
        stmt = (
            insert(MeetingArtifact)
            .values(
                meeting_manifest_id=meeting_manifest_id,
                artifact_type=artifact_type,
                decision_index=decision_index,
                original_filename=original_filename,
                content_hash=content_hash,
                gcs_path=gcs_path,
                byte_size=byte_size,
                source_page_url=source_page_url,
                fetched_at=_utcnow(),
                scrape_run_id=scrape_run_id,
            )
            .on_conflict_do_nothing(index_elements=["content_hash"])
        )
        self.session.execute(stmt)
        self.session.flush()
        return self.get_by_hash(content_hash)

    def get_by_hash(self, content_hash: str) -> Optional[MeetingArtifact]:
        stmt = select(MeetingArtifact).where(
            MeetingArtifact.content_hash == content_hash
        )
        return self.session.execute(stmt).scalar_one_or_none()

    def list_for_manifest(
        self, meeting_manifest_id: int
    ) -> list[MeetingArtifact]:
        stmt = (
            select(MeetingArtifact)
            .where(MeetingArtifact.meeting_manifest_id == meeting_manifest_id)
            .order_by(MeetingArtifact.artifact_type, MeetingArtifact.decision_index)
        )
        return list(self.session.execute(stmt).scalars().all())

    def exists(self, meeting_manifest_id: int, artifact_type: str) -> bool:
        """Return True if at least one artifact of *artifact_type* exists for the manifest row."""
        stmt = select(MeetingArtifact.id).where(
            MeetingArtifact.meeting_manifest_id == meeting_manifest_id,
            MeetingArtifact.artifact_type == artifact_type,
        ).limit(1)
        return self.session.execute(stmt).scalar_one_or_none() is not None


# =============================================================================
# Reconciliation repository
# =============================================================================


class ReconciliationRepository:
    """Repository for :class:`Reconciliation`."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert_many(self, rows: Iterable[dict[str, Any]]) -> int:
        rows = list(rows)
        if not rows:
            return 0

        now = _utcnow()
        for row in rows:
            row["computed_at"] = now

        stmt = insert(Reconciliation).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "scrape_run_id",
                "lb_id",
                "year_id",
                "main_group_value_id",
                "category",
            ],
            set_={
                "dashboard_kpi_count": stmt.excluded.dashboard_kpi_count,
                "manifest_row_count": stmt.excluded.manifest_row_count,
                "delta": stmt.excluded.delta,
                "status": stmt.excluded.status,
                "computed_at": stmt.excluded.computed_at,
            },
        )
        self.session.execute(stmt)
        self.session.flush()
        return len(rows)

    def list_for_run(
        self,
        scrape_run_id: int,
        status_filter: Optional[str] = None,
    ) -> list[Reconciliation]:
        """Return reconciliation rows for *scrape_run_id*, optionally filtered by status."""
        stmt = select(Reconciliation).where(
            Reconciliation.scrape_run_id == scrape_run_id
        )
        if status_filter is not None:
            stmt = stmt.where(Reconciliation.status == status_filter)
        stmt = stmt.order_by(
            Reconciliation.lb_id,
            Reconciliation.year_id,
            Reconciliation.main_group_value_id,
            Reconciliation.category,
        )
        return list(self.session.execute(stmt).scalars().all())


__all__ = [
    "DistrictRepository",
    "LBTypeRepository",
    "YearRepository",
    "LBRepository",
    "MainGroupValueRepository",
    "ScrapeRunRepository",
    "LBProgressRepository",
    "DashboardKPISnapshotRepository",
    "MeetingManifestRepository",
    "MeetingArtifactRepository",
    "ReconciliationRepository",
]
