"""Re-enqueue artifact cells for LBs that already have manifest data.

For each LB whose ``lb_progress`` is ``done|in_progress|error`` (i.e. its
manifest stage already ran), enumerate the (year_id, main_group_value_id)
cells with Approved manifest rows and dispatch a chord:

    chord([scrape_artifacts_cell(...) per cell], _artifacts_complete.s())

This skips re-running the manifest stage (already correct) and re-walks only
the artifact stage with the now-fixed two-step attachment flow. Pending LBs
not yet started are left alone — they go through the normal ``scrape_lb``
path.

Usage:
  uv run python scripts/sakarma_reenqueue_cells.py --scrape-run-id 1
  uv run python scripts/sakarma_reenqueue_cells.py --scrape-run-id 1 --dry-run
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from sakarma.db.session import get_session


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--scrape-run-id", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--statuses",
        default="done,in_progress,error",
        help="Comma list of lb_progress.status values to re-enqueue.",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    statuses = [s.strip() for s in args.statuses.split(",") if s.strip()]

    with get_session() as session:
        rows = session.execute(
            text(
                """
                SELECT lb_id
                FROM sakarma.lb_progress
                WHERE scrape_run_id = :run_id
                  AND status = ANY(:statuses)
                ORDER BY lb_id
                """
            ),
            {"run_id": args.scrape_run_id, "statuses": statuses},
        ).all()
    lb_ids = [r.lb_id for r in rows]
    print(f"affected LBs ({','.join(statuses)}): {len(lb_ids)}")

    if args.dry_run:
        # Count cells per LB without dispatching.
        with get_session() as session:
            for lb_id in lb_ids[:5]:
                cells = session.execute(
                    text(
                        """
                        SELECT COUNT(*) FROM (
                          SELECT DISTINCT year_id, main_group_value_id
                          FROM sakarma.meeting_manifest
                          WHERE lb_id = :lb_id
                            AND scrape_run_id = :run_id
                            AND category = 2
                        ) c
                        """
                    ),
                    {"lb_id": lb_id, "run_id": args.scrape_run_id},
                ).scalar_one()
                print(f"  lb_id={lb_id} cells={cells}")
            if len(lb_ids) > 5:
                print(f"  ... ({len(lb_ids) - 5} more)")
        print("dry-run: no chords dispatched")
        return 0

    # Live path — import Celery tasks lazily so dry-run doesn't require broker.
    from celery import chord, group

    from sakarma.db.repositories import (
        LBProgressRepository,
        MeetingManifestRepository,
    )
    from sakarma.tasks.cell_artifacts import scrape_artifacts_cell
    from sakarma.tasks.orchestrator import _artifacts_complete

    dispatched_lbs = 0
    dispatched_cells = 0
    skipped_no_cells = 0

    with get_session() as session:
        manifest_repo = MeetingManifestRepository(session)
        progress_repo = LBProgressRepository(session)

        for lb_id in lb_ids:
            cells = manifest_repo.list_approved_cells_for_lb_run(
                lb_id, args.scrape_run_id
            )
            if not cells:
                skipped_no_cells += 1
                continue

            # Reset progress to artifacts/in_progress so the dashboard
            # reflects the re-run; final mark_done fires from chord cb.
            progress = progress_repo.get_by_run_lb(args.scrape_run_id, lb_id)
            if progress is not None:
                progress_repo.mark_in_progress(progress.id)
                progress_repo.mark_stage(progress.id, "artifacts")
            session.commit()

            sigs = [
                scrape_artifacts_cell.s(
                    args.scrape_run_id, lb_id, year_id, mg_id
                )
                for (year_id, mg_id) in cells
            ]
            chord(group(sigs))(
                _artifacts_complete.s(
                    scrape_run_id=args.scrape_run_id, lb_id=lb_id
                )
            )
            dispatched_lbs += 1
            dispatched_cells += len(sigs)
            if dispatched_lbs % 25 == 0:
                print(
                    f"  dispatched {dispatched_lbs} LBs / "
                    f"{dispatched_cells} cells so far"
                )

    print(
        f"DONE: dispatched_lbs={dispatched_lbs} "
        f"dispatched_cells={dispatched_cells} "
        f"skipped_no_cells={skipped_no_cells}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
