#!/usr/bin/env python3
"""Download test PDFs from S3 for extraction testing.

This script downloads 5 PDFs per (district, lb_type) combination for year 2025-2026
from S3 to a local test_pdfs/ directory.

Usage:
    uv run python scripts/download_test_pdfs.py
    uv run python scripts/download_test_pdfs.py --force  # Re-download even if exists
    uv run python scripts/download_test_pdfs.py --samples 10  # 10 per group
"""

import argparse
import os
import random
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Set defaults for local development
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha"
)
os.environ.setdefault("STORAGE_BACKEND", "s3")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("S3_BUCKET_NAME", "sulekha-pdfs")


def get_pdfs_by_district_lb_type(year_label: str = "2025-2026"):
    """Query database for PDFs grouped by district and LB type.
    
    Returns:
        Dict mapping (district_name, lb_type_label) -> list of (pdf_id, gcs_path)
    """
    from sqlalchemy import select
    from sulekha.db.session import get_session
    from sulekha.db.models import Pdf, Project, LocalBody, District, GcsUploadStatus

    grouped = defaultdict(list)
    
    with get_session() as session:
        stmt = (
            select(
                Pdf.id,
                Pdf.gcs_path,
                District.district_name,
                District.lb_type_label,
            )
            .join(Project, Pdf.project_id == Project.id)
            .join(LocalBody, Project.local_body_id == LocalBody.id)
            .join(District, LocalBody.district_id == District.id)
            .where(
                Pdf.status == GcsUploadStatus.UPLOADED,
                District.year_label == year_label,
                Pdf.gcs_path.isnot(None),
            )
        )
        results = session.execute(stmt).all()
        
        for pdf_id, gcs_path, district_name, lb_type_label in results:
            key = (district_name, lb_type_label)
            grouped[key].append((pdf_id, gcs_path))
    
    return grouped


def sample_pdfs(grouped: dict, samples_per_group: int = 5) -> list:
    """Sample N random PDFs from each (district, lb_type) group.
    
    Returns:
        List of (pdf_id, gcs_path, district_name, lb_type_label) tuples
    """
    sampled = []
    
    for (district_name, lb_type_label), pdf_list in grouped.items():
        n = min(samples_per_group, len(pdf_list))
        selected = random.sample(pdf_list, n)
        for pdf_id, gcs_path in selected:
            sampled.append((pdf_id, gcs_path, district_name, lb_type_label))
    
    return sampled


def download_pdf(storage, gcs_path: str, output_dir: Path) -> tuple[bool, str]:
    """Download a single PDF from S3 to local directory.
    
    Returns:
        (success, message) tuple
    """
    try:
        local_path = output_dir / gcs_path
        local_path.parent.mkdir(parents=True, exist_ok=True)
        
        if local_path.exists():
            return True, f"Already exists: {gcs_path}"
        
        content = storage.download(gcs_path)
        if content is None:
            return False, f"Not found in S3: {gcs_path}"
        
        local_path.write_bytes(content)
        return True, f"Downloaded: {gcs_path}"
        
    except Exception as e:
        return False, f"Error downloading {gcs_path}: {str(e)}"


def download_pdfs_parallel(
    pdf_records: list,
    output_dir: Path,
    max_workers: int = 10,
) -> dict:
    """Download PDFs in parallel using ThreadPoolExecutor.
    
    Returns:
        Stats dict with success/failed counts
    """
    from sulekha.storage import get_storage
    
    storage = get_storage()
    stats = {"success": 0, "failed": 0, "skipped": 0, "errors": []}
    
    print(f"\nDownloading {len(pdf_records)} PDFs with {max_workers} workers...")
    print("-" * 60)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_pdf = {
            executor.submit(download_pdf, storage, gcs_path, output_dir): (pdf_id, gcs_path, district, lb_type)
            for pdf_id, gcs_path, district, lb_type in pdf_records
        }
        
        for i, future in enumerate(as_completed(future_to_pdf), 1):
            pdf_id, gcs_path, district, lb_type = future_to_pdf[future]
            try:
                success, message = future.result()
                if success:
                    if "Already exists" in message:
                        stats["skipped"] += 1
                    else:
                        stats["success"] += 1
                    print(f"  [{i}/{len(pdf_records)}] {message}")
                else:
                    stats["failed"] += 1
                    stats["errors"].append(message)
                    print(f"  [{i}/{len(pdf_records)}] FAILED: {message}")
            except Exception as e:
                stats["failed"] += 1
                stats["errors"].append(f"{gcs_path}: {str(e)}")
                print(f"  [{i}/{len(pdf_records)}] ERROR: {gcs_path} - {str(e)}")
    
    return stats


def count_existing_pdfs(output_dir: Path) -> int:
    """Count existing PDF files in the output directory."""
    if not output_dir.exists():
        return 0
    return len(list(output_dir.rglob("*.pdf")))


def main():
    parser = argparse.ArgumentParser(
        description="Download test PDFs from S3 for extraction testing"
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=5,
        help="Number of PDFs to sample per (district, lb_type) group (default: 5)",
    )
    parser.add_argument(
        "--year",
        type=str,
        default="2025-2026",
        help="Year label to filter PDFs (default: 2025-2026)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).parent.parent / "test_pdfs",
        help="Output directory for downloaded PDFs",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force re-download even if test_pdfs/ has files",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=10,
        help="Number of parallel download workers (default: 10)",
    )
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  SULEKHA TEST PDF DOWNLOADER")
    print("=" * 60)
    print()
    print(f"  Year:           {args.year}")
    print(f"  Samples/group:  {args.samples}")
    print(f"  Output dir:     {args.output_dir}")
    print(f"  Workers:        {args.workers}")
    print()

    # Check if we should skip
    existing_count = count_existing_pdfs(args.output_dir)
    if existing_count > 0 and not args.force:
        print(f"  Found {existing_count} existing PDFs in {args.output_dir}")
        print("  Use --force to re-download")
        print()
        print("  Skipping download - using existing files")
        print("=" * 60)
        return 0

    # Query database
    print("  Querying database for PDFs...")
    grouped = get_pdfs_by_district_lb_type(args.year)
    
    if not grouped:
        print(f"  No PDFs found for year {args.year}")
        return 1
    
    print(f"  Found {sum(len(v) for v in grouped.values())} PDFs across {len(grouped)} groups")
    print()
    
    # Show group breakdown
    print("  Groups (district, lb_type):")
    for (district, lb_type), pdfs in sorted(grouped.items()):
        print(f"    - {district:20} | {lb_type:25} | {len(pdfs):>5} PDFs")
    print()

    # Sample PDFs
    sampled = sample_pdfs(grouped, args.samples)
    print(f"  Sampled {len(sampled)} PDFs ({args.samples} per group)")

    # Download
    stats = download_pdfs_parallel(sampled, args.output_dir, args.workers)

    # Report
    print()
    print("=" * 60)
    print("  DOWNLOAD COMPLETE")
    print("=" * 60)
    print(f"  Downloaded: {stats['success']}")
    print(f"  Skipped:    {stats['skipped']}")
    print(f"  Failed:     {stats['failed']}")
    print(f"  Total PDFs: {count_existing_pdfs(args.output_dir)}")
    print()
    
    if stats["errors"]:
        print("  Errors:")
        for err in stats["errors"][:10]:
            print(f"    - {err}")
        if len(stats["errors"]) > 10:
            print(f"    ... and {len(stats['errors']) - 10} more")
        print()

    print(f"  Output directory: {args.output_dir}")
    print("=" * 60)
    print()

    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
