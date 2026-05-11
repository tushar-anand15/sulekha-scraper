"""Phase 2 dispatcher — fire artifact chord for every manifest-done LB.

After Phase 1 of the two-phase scrape (``scrape_lb`` with
``phase='manifest_only'``) completes for every LB, this script reads
``lb_progress`` for rows at ``current_stage='manifest_done'`` and dispatches
the existing ``chord(scrape_artifacts_cell × N, _artifacts_complete)`` per
LB. The chord callback runs reconciliation + ``mark_done`` as it does in the
single-phase flow, so no code duplication.

Usage:
    uv run python scripts/sakarma_dispatch_artifacts.py --scrape-run-id 1
    uv run python scripts/sakarma_dispatch_artifacts.py --scrape-run-id 1 --dry-run
    uv run python scripts/sakarma_dispatch_artifacts.py --scrape-run-id 1 --limit 5
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import text


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--scrape-run-id", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of LBs dispatched (useful for testing).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    from sakarma.db.session import get_session

    with get_session() as s:
        rows = s.execute(
            text(
                """
                SELECT lb_id
                FROM sakarma.lb_progress
                WHERE scrape_run_id = :run
                  AND status = 'in_progress'
                  AND current_stage = 'manifest_done'
                  AND manifest_completed_at IS NOT NULL
                ORDER BY lb_id
                """
            ),
            {"run": args.scrape_run_id},
        ).all()
    lb_ids = [r.lb_id for r in rows]
    if args.limit is not None:
        lb_ids = lb_ids[: args.limit]
    print(f"manifest_done LBs eligible for phase-2 dispatch: {len(lb_ids)}")

    if args.dry_run:
        from sakarma.db.repositories import MeetingManifestRepository

        with get_session() as s:
            mr = MeetingManifestRepository(s)
            preview = lb_ids[:5]
            for lb_id in preview:
                cells = mr.list_approved_cells_for_lb_run(
                    lb_id, args.scrape_run_id
                )
                print(f"  lb_id={lb_id} cells={len(cells)}")
            if len(lb_ids) > 5:
                print(f"  ... ({len(lb_ids) - 5} more)")
        print("dry-run: no chords dispatched")
        return 0

    # Live path — import Celery + chord tasks lazily so dry-run doesn't
    # need broker connectivity.
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
            progress = progress_repo.get_by_run_lb(args.scrape_run_id, lb_id)
            if not cells:
                # No artifacts to fetch — finish the LB now so we don't
                # leave it stuck at manifest_done forever.
                if progress is not None:
                    progress_repo.mark_done(progress.id)
                session.commit()
                skipped_no_cells += 1
                continue

            # Flip current_stage to 'artifacts' so the dashboard reflects
            # the transition into phase 2 before the chord fans out.
            if progress is not None:
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
            if dispatched_lbs % 50 == 0:
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
