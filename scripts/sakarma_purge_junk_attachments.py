"""Purge junk SAKARMA attachment_pdf rows + their GCS objects.

Background: a bug in the attachment download flow caused 170K+
``attachment_pdf`` blobs to be stored as HTML dashboard pages (not real PDFs).
This one-shot script deletes those rows from ``sakarma.meeting_artifact`` and
removes the corresponding objects from GCS.

Safety:
- Defaults to ``--dry-run``. Pass ``--apply`` to actually delete.
- Filters strictly by ``artifact_type='attachment_pdf'`` so ``minutes_html``
  and ``dr_html`` are never touched.
- GCS deletes run in parallel via a thread pool (default 32 workers). Missing
  blobs (already gone) are tolerated.

Usage:
  uv run python scripts/sakarma_purge_junk_attachments.py --dry-run
  uv run python scripts/sakarma_purge_junk_attachments.py --apply
  uv run python scripts/sakarma_purge_junk_attachments.py --apply --scrape-run-id 1
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from sqlalchemy import text

from sakarma.config import settings
from sakarma.db.session import get_session
from sakarma.storage.gcs import get_storage


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="Report counts only.")
    g.add_argument("--apply", action="store_true", help="Actually delete.")
    p.add_argument(
        "--scrape-run-id",
        type=int,
        default=None,
        help="Scope to a single scrape_run_id (default: all runs).",
    )
    p.add_argument("--workers", type=int, default=32)
    p.add_argument("--batch-size", type=int, default=5000)
    return p.parse_args()


def _delete_blob(storage, gcs_path: str) -> tuple[str, bool, str]:
    """Best-effort delete; treat NotFound as success."""
    try:
        storage.delete(gcs_path)
        return (gcs_path, True, "")
    except Exception as exc:
        msg = str(exc)
        if "404" in msg or "Not Found" in msg or "NotFound" in msg:
            return (gcs_path, True, "missing")
        return (gcs_path, False, msg)


def main() -> int:
    args = parse_args()

    where_clause = "WHERE artifact_type = 'attachment_pdf'"
    params: dict = {}
    if args.scrape_run_id is not None:
        where_clause += " AND scrape_run_id = :run_id"
        params["run_id"] = args.scrape_run_id

    with get_session() as session:
        total = session.execute(
            text(f"SELECT COUNT(*) FROM sakarma.meeting_artifact {where_clause}"),
            params,
        ).scalar_one()
    print(f"target rows: {total}")

    if args.dry_run:
        print("dry-run: no deletes performed")
        return 0

    storage = get_storage()
    print(f"using GCS bucket: {settings.gcs_bucket_name}")

    deleted_blobs = 0
    missing_blobs = 0
    failed_blobs = 0

    while True:
        with get_session() as session:
            rows = session.execute(
                text(
                    f"SELECT id, gcs_path FROM sakarma.meeting_artifact "
                    f"{where_clause} ORDER BY id LIMIT :limit"
                ),
                {**params, "limit": args.batch_size},
            ).all()
        if not rows:
            break

        ids = [r.id for r in rows]
        paths = [r.gcs_path for r in rows]

        # Parallel GCS delete.
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futures = [ex.submit(_delete_blob, storage, p) for p in paths]
            for fut in as_completed(futures):
                _path, ok, msg = fut.result()
                if ok and msg == "missing":
                    missing_blobs += 1
                elif ok:
                    deleted_blobs += 1
                else:
                    failed_blobs += 1
                    if failed_blobs < 10:
                        print(f"  delete failed: {_path} -> {msg}", file=sys.stderr)

        # DB delete only after GCS delete attempt.
        with get_session() as session:
            session.execute(
                text(
                    "DELETE FROM sakarma.meeting_artifact WHERE id = ANY(:ids)"
                ),
                {"ids": ids},
            )
            session.commit()

        print(
            f"batch: rows_deleted={len(rows)}  "
            f"blobs_deleted={deleted_blobs}  "
            f"missing={missing_blobs}  failed={failed_blobs}"
        )

    print(
        f"DONE: blobs_deleted={deleted_blobs} missing={missing_blobs} "
        f"failed={failed_blobs}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
