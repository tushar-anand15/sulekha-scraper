#!/usr/bin/env python3
"""CSV Data Export Script for Sulekha Pipeline.

Exports all scraped data to CSV files with a focus on a flattened PDF-centric view
that maintains the full hierarchy (Year -> LB Type -> District -> Local Body -> Project -> PDF).

Usage:
    uv run python scripts/export_data.py
    uv run python scripts/export_data.py --year 2024
    uv run python scripts/export_data.py --district "Thiruvananthapuram"
    uv run python scripts/export_data.py --only-flat
    uv run python scripts/export_data.py --upload  # Export and upload to GCS
    uv run python scripts/export_data.py --upload-only exports/20260215_133809  # Upload existing export
    
    # Or with docker:
    docker-compose exec worker python scripts/export_data.py
"""

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Set defaults for local connection
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha")

# GCS credentials - look for service account key in standard locations
# MUST be set before importing storage module
script_dir = Path(__file__).parent.parent
credentials_path = script_dir / "credentials" / "sulekha-487304-4f5a021dcc1c.json"
if credentials_path.exists():
    os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))

# Use GCS by default (user's production setup), fall back to S3/Minio for local dev
os.environ.setdefault("STORAGE_BACKEND", "gcs")
os.environ.setdefault("GCS_BUCKET_NAME", "sulekha-pdfs")
# S3/Minio fallback settings (for local development)
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("S3_BUCKET_NAME", "sulekha-pdfs")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def upload_exports_to_gcs(export_path: Path) -> dict[str, bool]:
    """Upload all CSV files from an export directory to GCS.
    
    Files are uploaded to: exports/{timestamp}/*.csv
    in the same bucket as PDFs (sulekha-pdfs).
    
    Args:
        export_path: Path to the local export directory (e.g., exports/20260215_133809)
        
    Returns:
        Dictionary of {filename: success} indicating upload status for each file
    """
    from sulekha.storage.gcs import get_storage
    
    print(f"Uploading exports to GCS...")
    print(f"  Source: {export_path}")
    
    # Get storage client
    storage = get_storage()
    print(f"  Bucket: {storage.bucket_name}")
    
    # Get the timestamp folder name from the path
    timestamp_folder = export_path.name
    gcs_prefix = f"exports/{timestamp_folder}"
    print(f"  GCS path: {gcs_prefix}/")
    print()
    
    results = {}
    csv_files = list(export_path.glob("*.csv"))
    
    if not csv_files:
        print("  No CSV files found to upload!")
        return results
    
    print(f"  Found {len(csv_files)} CSV files to upload")
    print()
    
    total_size = 0
    for csv_file in csv_files:
        gcs_path = f"{gcs_prefix}/{csv_file.name}"
        file_size = csv_file.stat().st_size
        file_size_mb = file_size / (1024 * 1024)
        
        print(f"  Uploading {csv_file.name} ({file_size_mb:.2f} MB)...")
        
        try:
            # Read file content
            with open(csv_file, 'rb') as f:
                content = f.read()
            
            # Upload to GCS
            storage.upload(gcs_path, content, content_type="text/csv")
            
            results[csv_file.name] = True
            total_size += file_size
            print(f"    -> {gcs_path}")
            
        except Exception as e:
            print(f"    ERROR: {e}")
            results[csv_file.name] = False
    
    # Print summary
    successful = sum(1 for v in results.values() if v)
    failed = len(results) - successful
    total_size_mb = total_size / (1024 * 1024)
    
    print()
    print(f"  Upload complete: {successful} succeeded, {failed} failed")
    print(f"  Total uploaded: {total_size_mb:.2f} MB")
    print(f"  GCS location: gs://{storage.bucket_name}/{gcs_prefix}/")
    
    return results


def get_session():
    """Create a database session."""
    engine = create_engine(os.environ["DATABASE_URL"], echo=False)
    Session = sessionmaker(bind=engine)
    return Session()


def create_export_dir(base_dir: str = "exports") -> Path:
    """Create a timestamped export directory."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    export_path = Path(base_dir) / timestamp
    export_path.mkdir(parents=True, exist_ok=True)
    return export_path


def get_available_years(session) -> list[tuple[int, str]]:
    """Get all available years from the database.
    
    Returns:
        List of (year_val, year_label) tuples, sorted descending
    """
    result = session.execute(text("""
        SELECT DISTINCT year_val, year_label 
        FROM districts 
        ORDER BY year_val DESC
    """))
    return [(row[0], row[1]) for row in result]


def export_flat_pdfs_for_year(session, export_path: Path, year_val: int, year_label: str, district: str = None) -> int:
    """Export flattened PDF view for a single year.
    
    Args:
        session: Database session
        export_path: Directory to write CSV to
        year_val: Year value to filter
        year_label: Year label for filename
        district: Optional district name filter
        
    Returns:
        Number of rows exported
    """
    # Sanitize year_label for filename (remove special chars)
    safe_year_label = year_label.replace("/", "-").replace("\\", "-").replace(" ", "_")
    filename = f"pdfs_flat_{safe_year_label}.csv"
    
    print(f"  Exporting year {year_label}...")
    
    # Build query
    query = """
        SELECT 
            d.year_val, d.year_label, d.lb_type_val, d.lb_type_label,
            d.district_index, d.district_name,
            lb.lb_index, lb.lb_name,
            p.project_no, p.project_name, p.formulation, p.expense,
            p.pdf_status, p.scraped_at,
            pdf.gcs_path, pdf.gcs_bucket, pdf.original_url,
            pdf.file_size_bytes, pdf.content_hash,
            pdf.downloaded_at, pdf.uploaded_at
        FROM projects p
        JOIN local_bodies lb ON p.local_body_id = lb.id
        JOIN districts d ON lb.district_id = d.id
        LEFT JOIN pdfs pdf ON pdf.project_id = p.id
        WHERE d.year_val = :year
    """
    
    params = {"year": year_val}
    if district:
        query += " AND d.district_name ILIKE :district"
        params["district"] = f"%{district}%"
    
    query += """
        ORDER BY d.lb_type_val, d.district_index, 
                 lb.lb_index, p.project_no
    """
    
    # Column headers
    headers = [
        "year_val", "year_label", "lb_type_val", "lb_type_label",
        "district_index", "district_name",
        "local_body_index", "local_body_name",
        "project_no", "project_name", "formulation", "expense",
        "pdf_status", "pdf_downloaded",
        "pdf_gcs_path", "pdf_gcs_bucket", "pdf_original_url",
        "pdf_file_size_bytes", "pdf_content_hash",
        "project_scraped_at", "pdf_downloaded_at", "pdf_uploaded_at"
    ]
    
    filepath = export_path / filename
    row_count = 0
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        result = session.execute(text(query), params)
        for row in result:
            # Transform row to add derived pdf_downloaded field
            row_data = list(row)
            pdf_status = row_data[12]  # pdf_status column
            pdf_downloaded = pdf_status == "DOWNLOADED"
            
            # Reorder to match headers (insert pdf_downloaded after pdf_status)
            output_row = [
                row_data[0],   # year_val
                row_data[1],   # year_label
                row_data[2],   # lb_type_val
                row_data[3],   # lb_type_label
                row_data[4],   # district_index
                row_data[5],   # district_name
                row_data[6],   # local_body_index (lb_index)
                row_data[7],   # local_body_name (lb_name)
                row_data[8],   # project_no
                row_data[9],   # project_name
                row_data[10],  # formulation
                row_data[11],  # expense
                row_data[12],  # pdf_status
                pdf_downloaded,  # pdf_downloaded (derived)
                row_data[14],  # pdf_gcs_path
                row_data[15],  # pdf_gcs_bucket
                row_data[16],  # pdf_original_url
                row_data[17],  # pdf_file_size_bytes
                row_data[18],  # pdf_content_hash
                row_data[13],  # project_scraped_at (scraped_at)
                row_data[19],  # pdf_downloaded_at
                row_data[20],  # pdf_uploaded_at
            ]
            writer.writerow(output_row)
            row_count += 1
            
            if row_count % 50000 == 0:
                print(f"    {row_count:,} rows...")
    
    print(f"    {year_label}: {row_count:,} rows -> {filename}")
    return row_count


def export_flat_pdfs(session, export_path: Path, year: int = None, district: str = None) -> dict[str, int]:
    """Export flattened PDF view with full hierarchy, organized by year.
    
    Args:
        session: Database session
        export_path: Directory to write CSV to
        year: Optional single year filter (e.g., 29 for 2025-2026)
        district: Optional district name filter
        
    Returns:
        Dictionary of {filename: row_count}
    """
    print("Exporting flattened PDF views (year-wise)...")
    print()
    
    results = {}
    
    if year:
        # Single year specified - get its label
        result = session.execute(
            text("SELECT year_label FROM districts WHERE year_val = :year LIMIT 1"),
            {"year": year}
        )
        row = result.fetchone()
        if row:
            year_label = row[0]
            safe_label = year_label.replace("/", "-").replace("\\", "-").replace(" ", "_")
            filename = f"pdfs_flat_{safe_label}.csv"
            count = export_flat_pdfs_for_year(session, export_path, year, year_label, district)
            results[filename] = count
        else:
            print(f"  Warning: No data found for year_val={year}")
    else:
        # Export all years
        years = get_available_years(session)
        print(f"  Found {len(years)} years to export")
        print()
        
        for year_val, year_label in years:
            safe_label = year_label.replace("/", "-").replace("\\", "-").replace(" ", "_")
            filename = f"pdfs_flat_{safe_label}.csv"
            count = export_flat_pdfs_for_year(session, export_path, year_val, year_label, district)
            results[filename] = count
    
    print()
    total = sum(results.values())
    print(f"  Total: {total:,} rows across {len(results)} files")
    
    return results


def export_districts(session, export_path: Path) -> int:
    """Export raw districts table."""
    print("Exporting districts...")
    
    query = """
        SELECT 
            id, year_val, year_label, lb_type_val, lb_type_label,
            district_index, district_name, num_local_bodies, num_projects,
            postback_target, postback_argument, status, retry_count,
            error_message, discovered_at, last_processed_at
        FROM districts
        ORDER BY year_val DESC, lb_type_val, district_index
    """
    
    headers = [
        "id", "year_val", "year_label", "lb_type_val", "lb_type_label",
        "district_index", "district_name", "num_local_bodies", "num_projects",
        "postback_target", "postback_argument", "status", "retry_count",
        "error_message", "discovered_at", "last_processed_at"
    ]
    
    filepath = export_path / "districts.csv"
    row_count = 0
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        result = session.execute(text(query))
        for row in result:
            writer.writerow(row)
            row_count += 1
    
    print(f"  Exported {row_count:,} rows to {filepath}")
    return row_count


def export_local_bodies(session, export_path: Path) -> int:
    """Export raw local_bodies table with district context."""
    print("Exporting local bodies...")
    
    query = """
        SELECT 
            lb.id, lb.district_id,
            d.district_name, d.year_label, d.lb_type_label,
            lb.lb_index, lb.lb_name, lb.expected_projects, lb.scraped_projects,
            lb.last_page_scraped, lb.total_pages,
            lb.postback_target, lb.postback_argument,
            lb.status, lb.retry_count, lb.error_message,
            lb.discovered_at, lb.last_scraped_at
        FROM local_bodies lb
        JOIN districts d ON lb.district_id = d.id
        ORDER BY d.year_val DESC, d.lb_type_val, d.district_index, lb.lb_index
    """
    
    headers = [
        "id", "district_id",
        "district_name", "year_label", "lb_type_label",
        "lb_index", "lb_name", "expected_projects", "scraped_projects",
        "last_page_scraped", "total_pages",
        "postback_target", "postback_argument",
        "status", "retry_count", "error_message",
        "discovered_at", "last_scraped_at"
    ]
    
    filepath = export_path / "local_bodies.csv"
    row_count = 0
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        result = session.execute(text(query))
        for row in result:
            writer.writerow(row)
            row_count += 1
    
    print(f"  Exported {row_count:,} rows to {filepath}")
    return row_count


def export_projects(session, export_path: Path) -> int:
    """Export raw projects table with context."""
    print("Exporting projects...")
    
    query = """
        SELECT 
            p.id, p.local_body_id,
            lb.lb_name, d.district_name, d.year_label, d.lb_type_label,
            p.project_no, p.project_name, p.formulation, p.expense,
            p.page_number, p.select_argument,
            p.pdf_status, p.pdf_retry_count, p.pdf_error_message,
            p.pdf_last_attempt_at, p.scraped_at
        FROM projects p
        JOIN local_bodies lb ON p.local_body_id = lb.id
        JOIN districts d ON lb.district_id = d.id
        ORDER BY d.year_val DESC, d.lb_type_val, d.district_index, 
                 lb.lb_index, p.project_no
    """
    
    headers = [
        "id", "local_body_id",
        "local_body_name", "district_name", "year_label", "lb_type_label",
        "project_no", "project_name", "formulation", "expense",
        "page_number", "select_argument",
        "pdf_status", "pdf_retry_count", "pdf_error_message",
        "pdf_last_attempt_at", "scraped_at"
    ]
    
    filepath = export_path / "projects.csv"
    row_count = 0
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        result = session.execute(text(query))
        for row in result:
            writer.writerow(row)
            row_count += 1
            
            if row_count % 50000 == 0:
                print(f"  Exported {row_count:,} rows...")
    
    print(f"  Exported {row_count:,} rows to {filepath}")
    return row_count


def export_pdfs(session, export_path: Path) -> int:
    """Export raw pdfs table with project context."""
    print("Exporting PDFs...")
    
    query = """
        SELECT 
            pdf.id, pdf.project_id,
            p.project_no, p.project_name,
            lb.lb_name, d.district_name, d.year_label,
            pdf.gcs_path, pdf.gcs_bucket,
            pdf.original_url, pdf.original_filename, pdf.redirect_url,
            pdf.file_size_bytes, pdf.content_hash,
            pdf.status, pdf.upload_error,
            pdf.downloaded_at, pdf.uploaded_at
        FROM pdfs pdf
        JOIN projects p ON pdf.project_id = p.id
        JOIN local_bodies lb ON p.local_body_id = lb.id
        JOIN districts d ON lb.district_id = d.id
        ORDER BY d.year_val DESC, d.lb_type_val, d.district_index, 
                 lb.lb_index, p.project_no
    """
    
    headers = [
        "id", "project_id",
        "project_no", "project_name",
        "local_body_name", "district_name", "year_label",
        "gcs_path", "gcs_bucket",
        "original_url", "original_filename", "redirect_url",
        "file_size_bytes", "content_hash",
        "status", "upload_error",
        "downloaded_at", "uploaded_at"
    ]
    
    filepath = export_path / "pdfs.csv"
    row_count = 0
    
    with open(filepath, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        
        result = session.execute(text(query))
        for row in result:
            writer.writerow(row)
            row_count += 1
            
            if row_count % 50000 == 0:
                print(f"  Exported {row_count:,} rows...")
    
    print(f"  Exported {row_count:,} rows to {filepath}")
    return row_count


def print_summary(export_path: Path, counts: dict):
    """Print export summary."""
    print()
    print("=" * 60)
    print("  EXPORT SUMMARY")
    print("=" * 60)
    print(f"  Output directory: {export_path}")
    print()
    
    total_rows = 0
    for name, count in counts.items():
        print(f"  {name:<20} {count:>10,} rows")
        total_rows += count
    
    print("  " + "-" * 40)
    print(f"  {'Total':<20} {total_rows:>10,} rows")
    print()
    
    # Show file sizes
    print("  File sizes:")
    for file in sorted(export_path.glob("*.csv")):
        size_mb = file.stat().st_size / (1024 * 1024)
        print(f"    {file.name:<25} {size_mb:>8.2f} MB")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Export Sulekha pipeline data to CSV files"
    )
    parser.add_argument(
        "--year", type=int, 
        help="Filter by year (e.g., 2024)"
    )
    parser.add_argument(
        "--district", type=str,
        help="Filter by district name (partial match)"
    )
    parser.add_argument(
        "--only-flat", action="store_true",
        help="Export only the flattened PDF view"
    )
    parser.add_argument(
        "--output-dir", type=str, default="exports",
        help="Base output directory (default: exports)"
    )
    parser.add_argument(
        "--upload", action="store_true",
        help="Upload exported CSVs to GCS after export"
    )
    parser.add_argument(
        "--upload-only", type=str, metavar="PATH",
        help="Upload existing export directory to GCS without re-exporting (e.g., exports/20260215_133809)"
    )
    args = parser.parse_args()
    
    # Handle upload-only mode
    if args.upload_only:
        upload_path = Path(args.upload_only)
        if not upload_path.exists():
            print(f"Error: Export directory not found: {upload_path}")
            return 1
        if not upload_path.is_dir():
            print(f"Error: Path is not a directory: {upload_path}")
            return 1
        
        print()
        print("╔════════════════════════════════════════════════════════════╗")
        print("║           SULEKHA EXPORT UPLOAD TO GCS                     ║")
        print(f"║           {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<43}    ║")
        print("╚════════════════════════════════════════════════════════════╝")
        print()
        
        try:
            results = upload_exports_to_gcs(upload_path)
            if all(results.values()):
                print()
                print("✅ Upload completed successfully!")
                return 0
            else:
                print()
                print("⚠️  Upload completed with some failures")
                return 1
        except Exception as e:
            print(f"\n❌ Upload failed: {e}")
            import traceback
            traceback.print_exc()
            return 1
    
    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║           SULEKHA DATA EXPORT                              ║")
    print(f"║           {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<43}    ║")
    print("╚════════════════════════════════════════════════════════════╝")
    print()
    
    # Show filters if any
    if args.year or args.district:
        print("  Filters:")
        if args.year:
            print(f"    Year: {args.year}")
        if args.district:
            print(f"    District: {args.district}")
        print()
    
    # Connect to database
    try:
        session = get_session()
    except Exception as e:
        print(f"❌ Failed to connect to database: {e}")
        print("\nMake sure PostgreSQL is running:")
        print("  docker-compose up -d postgres")
        return 1
    
    # Create export directory
    export_path = create_export_dir(args.output_dir)
    print(f"  Exporting to: {export_path}")
    print()
    
    counts = {}
    
    try:
        # Always export the flat view (now returns dict of year-wise files)
        flat_counts = export_flat_pdfs(
            session, export_path, 
            year=args.year, 
            district=args.district
        )
        counts.update(flat_counts)
        
        # Export raw tables if not --only-flat
        if not args.only_flat:
            counts["districts.csv"] = export_districts(session, export_path)
            counts["local_bodies.csv"] = export_local_bodies(session, export_path)
            counts["projects.csv"] = export_projects(session, export_path)
            counts["pdfs.csv"] = export_pdfs(session, export_path)
        
        print_summary(export_path, counts)
        print("✅ Export completed successfully!")
        print()
        
        # Upload to GCS if requested
        if args.upload:
            print()
            print("-" * 60)
            print()
            upload_results = upload_exports_to_gcs(export_path)
            if not all(upload_results.values()):
                print()
                print("⚠️  Some uploads failed")
                return 1
            print()
            print("✅ Upload to GCS completed successfully!")
            print()
        
    except Exception as e:
        print(f"\n❌ Export failed: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        session.close()
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
