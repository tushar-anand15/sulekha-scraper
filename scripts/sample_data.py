#!/usr/bin/env python3
"""Sample dataset viewer for the Sulekha pipeline.

Shows random sample records from each phase of the pipeline.
Run multiple times to see different samples.

Usage:
    uv run python scripts/sample_data.py
    uv run python scripts/sample_data.py --count 5  # Show 5 samples per phase
"""

import argparse
import os
import sys
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Set defaults for local connection
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def get_session():
    """Create a database session."""
    engine = create_engine(os.environ["DATABASE_URL"], echo=False)
    Session = sessionmaker(bind=engine)
    return Session()


def format_currency(value):
    """Format as Indian Rupees."""
    try:
        return f"₹{int(value):,}"
    except (TypeError, ValueError):
        return "₹0"


def truncate(text, length=50):
    """Truncate text with ellipsis."""
    if not text:
        return ""
    return text[:length] + "..." if len(text) > length else text


def print_header(title):
    """Print a section header."""
    print()
    print("=" * 80)
    print(f"  {title}")
    print("=" * 80)


def print_divider():
    """Print a light divider."""
    print("  " + "-" * 76)


def show_scrape_runs(session, count):
    """Show sample scrape runs."""
    print_header("SCRAPE RUNS (Pipeline Executions)")
    
    result = session.execute(text("""
        SELECT id, current_phase, status, started_at, completed_at, error_message, created_at
        FROM scrape_runs
        ORDER BY created_at DESC
        LIMIT :count
    """), {"count": count})
    
    rows = result.fetchall()
    if not rows:
        print("  No scrape runs found.")
        return
    
    for i, row in enumerate(rows):
        if i > 0:
            print_divider()
        print(f"""
  Run ID: {row[0]}
  Phase: {row[1]} | Status: {row[2]}
  Created: {row[6]}
  Started: {row[3] or 'Not started'}
  Completed: {row[4] or 'In progress'}
  Error: {truncate(row[5], 60) if row[5] else 'None'}""")


def show_districts(session, count):
    """Show random sample districts."""
    print_header(f"PHASE 1: DISTRICTS (Random {count} samples)")
    
    result = session.execute(text("""
        SELECT 
            d.id, d.year_label, d.lb_type_label, d.district_name,
            d.num_local_bodies, d.num_projects, d.status,
            d.postback_target, d.postback_argument,
            d.discovered_at, d.last_processed_at
        FROM districts d
        ORDER BY RANDOM()
        LIMIT :count
    """), {"count": count})
    
    rows = result.fetchall()
    if not rows:
        print("  No districts found.")
        return
    
    for i, row in enumerate(rows):
        if i > 0:
            print_divider()
        status_icon = {"PENDING": "⏳", "IN_PROGRESS": "🔄", "DONE": "✅", "ERROR": "❌"}.get(row[6], "❓")
        print(f"""
  {status_icon} District Record
  ├─ ID: {row[0]}
  ├─ Year: {row[1]}
  ├─ LB Type: {row[2]}
  ├─ District: {row[3]}
  ├─ Local Bodies: {row[4] or 0}
  ├─ Expected Projects: {row[5]:,} 
  ├─ Status: {row[6]}
  ├─ Postback: target={row[7]}, arg={row[8]}
  ├─ Discovered: {row[9]}
  └─ Last Processed: {row[10] or 'Never'}""")


def show_local_bodies(session, count):
    """Show random sample local bodies."""
    print_header(f"PHASE 2: LOCAL BODIES (Random {count} samples)")
    
    result = session.execute(text("""
        SELECT 
            lb.id, lb.lb_name, lb.lb_index,
            lb.expected_projects, lb.scraped_projects,
            lb.last_page_scraped, lb.total_pages,
            lb.status, lb.error_message,
            lb.discovered_at, lb.last_scraped_at,
            d.district_name, d.year_label, d.lb_type_label
        FROM local_bodies lb
        JOIN districts d ON lb.district_id = d.id
        ORDER BY RANDOM()
        LIMIT :count
    """), {"count": count})
    
    rows = result.fetchall()
    if not rows:
        print("  No local bodies found.")
        return
    
    for i, row in enumerate(rows):
        if i > 0:
            print_divider()
        status_icon = {"PENDING": "⏳", "IN_PROGRESS": "🔄", "PARTIAL": "📊", "DONE": "✅", "ERROR": "❌"}.get(row[7], "❓")
        progress = f"{row[4]}/{row[3]}" if row[3] else f"{row[4]}/?"
        pages = f"{row[5]}/{row[6]}" if row[6] else f"{row[5]}/?"
        print(f"""
  {status_icon} Local Body Record
  ├─ ID: {row[0]}
  ├─ Name: {row[1]}
  ├─ Index: {row[2]}
  ├─ Parent: {row[11]} ({row[13]}, {row[12]})
  ├─ Projects: {progress} scraped
  ├─ Pages: {pages} scraped
  ├─ Status: {row[7]}
  ├─ Error: {truncate(row[8], 50) if row[8] else 'None'}
  ├─ Discovered: {row[9]}
  └─ Last Scraped: {row[10] or 'Never'}""")


def show_projects(session, count):
    """Show random sample projects."""
    print_header(f"PHASE 3: PROJECTS (Random {count} samples)")
    
    result = session.execute(text("""
        SELECT 
            p.id, p.project_no, p.project_name,
            p.formulation, p.expense,
            p.page_number, p.select_argument,
            p.pdf_status, p.pdf_retry_count, p.pdf_error_message,
            p.scraped_at,
            lb.lb_name,
            d.district_name, d.year_label, d.lb_type_label
        FROM projects p
        JOIN local_bodies lb ON p.local_body_id = lb.id
        JOIN districts d ON lb.district_id = d.id
        ORDER BY RANDOM()
        LIMIT :count
    """), {"count": count})
    
    rows = result.fetchall()
    if not rows:
        print("  No projects found.")
        return
    
    for i, row in enumerate(rows):
        if i > 0:
            print_divider()
        status_icon = {"PENDING": "⏳", "DOWNLOADING": "🔄", "DOWNLOADED": "✅", "MISSING": "⚠️", "ERROR": "❌"}.get(row[7], "❓")
        print(f"""
  {status_icon} Project Record
  ├─ ID: {row[0]}
  ├─ Project No: {row[1]}
  ├─ Name: {truncate(row[2], 60)}
  ├─ Formulation: {format_currency(row[3])}
  ├─ Expense: {format_currency(row[4])}
  ├─ Page: {row[5]} | Select: {row[6]}
  ├─ Local Body: {row[11]}
  ├─ Location: {row[12]} ({row[14]}, {row[13]})
  ├─ PDF Status: {row[7]} (retries: {row[8]})
  ├─ PDF Error: {truncate(row[9], 50) if row[9] else 'None'}
  └─ Scraped At: {row[10]}""")


def show_pdfs(session, count):
    """Show random sample PDFs."""
    print_header(f"PHASE 4: PDFs (Random {count} samples)")
    
    # First check if there are any PDFs
    result = session.execute(text("SELECT COUNT(*) FROM pdfs"))
    total = result.scalar()
    
    if total == 0:
        print("  No PDFs downloaded yet (Phase 4 not started)")
        return
    
    result = session.execute(text("""
        SELECT 
            pdf.id, pdf.gcs_path, pdf.gcs_bucket,
            pdf.original_url, pdf.original_filename,
            pdf.file_size_bytes, pdf.content_hash,
            pdf.status, pdf.upload_error,
            pdf.downloaded_at, pdf.uploaded_at,
            p.project_name, p.project_no,
            lb.lb_name,
            d.district_name, d.year_label
        FROM pdfs pdf
        JOIN projects p ON pdf.project_id = p.id
        JOIN local_bodies lb ON p.local_body_id = lb.id
        JOIN districts d ON lb.district_id = d.id
        ORDER BY RANDOM()
        LIMIT :count
    """), {"count": count})
    
    rows = result.fetchall()
    for i, row in enumerate(rows):
        if i > 0:
            print_divider()
        status_icon = {"PENDING": "⏳", "UPLOADED": "✅", "FAILED": "❌"}.get(row[7], "❓")
        size_kb = (row[5] or 0) / 1024
        print(f"""
  {status_icon} PDF Record
  ├─ ID: {row[0]}
  ├─ Project: {row[12]} - {truncate(row[11], 40)}
  ├─ Local Body: {row[13]}
  ├─ Location: {row[14]} ({row[15]})
  ├─ GCS Path: {row[1] or 'Not uploaded'}
  ├─ Bucket: {row[2] or 'N/A'}
  ├─ Original URL: {truncate(row[3], 50) if row[3] else 'N/A'}
  ├─ Filename: {row[4] or 'N/A'}
  ├─ Size: {size_kb:.1f} KB
  ├─ Hash: {row[6][:16] + '...' if row[6] else 'N/A'}
  ├─ Status: {row[7]}
  ├─ Error: {truncate(row[8], 50) if row[8] else 'None'}
  ├─ Downloaded: {row[9] or 'Never'}
  └─ Uploaded: {row[10] or 'Never'}""")


def show_counts(session):
    """Show summary counts."""
    print_header("SUMMARY COUNTS")
    
    counts = {}
    for table in ["scrape_runs", "districts", "local_bodies", "projects", "pdfs"]:
        result = session.execute(text(f"SELECT COUNT(*) FROM {table}"))
        counts[table] = result.scalar()
    
    print(f"""
  Scrape Runs:   {counts['scrape_runs']:>10,}
  Districts:     {counts['districts']:>10,}
  Local Bodies:  {counts['local_bodies']:>10,}
  Projects:      {counts['projects']:>10,}
  PDFs:          {counts['pdfs']:>10,}
""")


def main():
    parser = argparse.ArgumentParser(description="Show random sample data from each pipeline phase")
    parser.add_argument("-c", "--count", type=int, default=3, help="Number of samples per phase (default: 3)")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3, 4], help="Show only specific phase (1-4)")
    args = parser.parse_args()
    
    print()
    print("╔════════════════════════════════════════════════════════════════════════════╗")
    print("║              SULEKHA PIPELINE - SAMPLE DATA VIEWER                         ║")
    print(f"║              {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<58}      ║")
    print("╚════════════════════════════════════════════════════════════════════════════╝")
    
    try:
        session = get_session()
    except Exception as e:
        print(f"\n❌ Failed to connect to database: {e}")
        print("\nMake sure PostgreSQL is running:")
        print("  docker-compose -f docker-compose.dev.yml up -d postgres")
        return 1
    
    # Show counts first
    show_counts(session)
    
    # Show samples based on phase filter
    if args.phase is None or args.phase == 0:
        show_scrape_runs(session, args.count)
    
    if args.phase is None or args.phase == 1:
        show_districts(session, args.count)
    
    if args.phase is None or args.phase == 2:
        show_local_bodies(session, args.count)
    
    if args.phase is None or args.phase == 3:
        show_projects(session, args.count)
    
    if args.phase is None or args.phase == 4:
        show_pdfs(session, args.count)
    
    print()
    print("─" * 80)
    print("  Run again for different random samples!")
    print("  Use --count N to show N samples per phase")
    print("  Use --phase N to show only phase N (1-4)")
    print()
    
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
