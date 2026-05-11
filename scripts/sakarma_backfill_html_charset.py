"""Backfill UTF-8 charset <meta> tag into existing minutes_html / dr_html blobs.

Source HTML responses lack an in-document encoding declaration and rely on
the HTTP Content-Type header. Local viewers of the GCS-downloaded file see
mojibake because no header is in scope. This script re-uploads every
existing HTML blob with a meta charset injected, updates the DB row's
content_hash + byte_size to match the new content, and skips files that
are already self-describing.

Idempotent: rerunning skips blobs whose first KB already declares charset.
Run while the live scrape is active — only touches HTML artifacts for
already-done LBs which the scrape doesn't revisit (skips via
``MeetingArtifactRepository.exists``).
"""

from __future__ import annotations

import os
import sys
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg
from psycopg.rows import dict_row
from google.cloud import storage

os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

BUCKET = "your-meetings-bucket"
DB_DSN = "postgresql://sulekha:sulekha@10.160.0.3:5432/sulekha"
WORKERS = 32

_META = b'<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />'


def inject_charset(html_bytes: bytes) -> bytes:
    if not html_bytes:
        return html_bytes
    head_end = html_bytes.find(b"</head>")
    head_segment = html_bytes[:head_end] if head_end != -1 else html_bytes[:4096]
    if b"charset=" in head_segment.lower():
        return html_bytes  # already declares charset
    head_open_end = html_bytes.find(b">", html_bytes.lower().find(b"<head"))
    if head_open_end == -1:
        return _META + html_bytes
    return (
        html_bytes[: head_open_end + 1]
        + _META
        + html_bytes[head_open_end + 1 :]
    )


_thread_local = threading.local()


def _bucket():
    if not hasattr(_thread_local, "client"):
        _thread_local.client = storage.Client()
        _thread_local.bucket = _thread_local.client.bucket(BUCKET)
    return _thread_local.bucket


def process(row):
    """Return (id, status, new_hash, new_size) for one artifact row."""
    rid, gcs_path, old_hash = row["id"], row["gcs_path"], row["content_hash"]
    try:
        b = _bucket()
        blob = b.blob(gcs_path)
        data = blob.download_as_bytes()
        new = inject_charset(data)
        if new is data or new == data:
            return (rid, "skipped", None, None)
        # upload back to the SAME path with explicit content-type
        b.blob(gcs_path).upload_from_string(
            new, content_type="text/html; charset=utf-8"
        )
        new_hash = hashlib.sha256(new).hexdigest()
        return (rid, "updated", new_hash, len(new))
    except Exception as exc:
        return (rid, f"error:{type(exc).__name__}:{str(exc)[:60]}", None, None)


def main():
    with psycopg.connect(DB_DSN, row_factory=dict_row) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, gcs_path, content_hash FROM sakarma.meeting_artifact "
            "WHERE artifact_type IN ('minutes_html','dr_html') "
            "ORDER BY id"
        )
        rows = cur.fetchall()
    print(f"target rows: {len(rows)}")

    updated = skipped = errors = 0
    update_buf: list[tuple[str, int, int]] = []
    UPDATE_BATCH = 500
    error_samples: list[tuple[int, str]] = []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futures = [ex.submit(process, r) for r in rows]
        for i, fut in enumerate(as_completed(futures), 1):
            rid, status, new_hash, new_size = fut.result()
            if status == "updated":
                updated += 1
                update_buf.append((new_hash, new_size, rid))
            elif status == "skipped":
                skipped += 1
            else:
                errors += 1
                if len(error_samples) < 10:
                    error_samples.append((rid, status))

            # Flush UPDATE buffer
            if len(update_buf) >= UPDATE_BATCH:
                _flush(update_buf)
                update_buf.clear()

            if i % 1000 == 0:
                print(
                    f"  progress {i}/{len(rows)}  updated={updated}  "
                    f"skipped={skipped}  errors={errors}"
                )

    if update_buf:
        _flush(update_buf)
        update_buf.clear()

    print(f"\nDONE: updated={updated} skipped={skipped} errors={errors}")
    for rid, status in error_samples[:10]:
        print(f"  error sample: id={rid} {status}")


def _flush(updates):
    """Bulk-update content_hash + byte_size."""
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE sakarma.meeting_artifact "
                "SET content_hash=%s, byte_size=%s WHERE id=%s",
                updates,
            )


if __name__ == "__main__":
    sys.exit(main() or 0)
