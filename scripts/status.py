#!/usr/bin/env python3
"""Pipeline status report script.

Usage:
    uv run python scripts/status.py
    
    # Or with docker:
    docker-compose exec worker python scripts/status.py
"""

import os
import sys
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

# Set defaults for local connection
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha")
os.environ.setdefault("STORAGE_BACKEND", "s3")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("S3_BUCKET_NAME", "sulekha-pdfs")

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def get_session():
    """Create a database session."""
    engine = create_engine(os.environ["DATABASE_URL"], echo=False)
    Session = sessionmaker(bind=engine)
    return Session()


def format_number(n):
    """Format number with commas."""
    if n is None:
        return "0"
    return f"{n:,}"


def format_pct(part, total):
    """Format percentage."""
    if total == 0:
        return "0%"
    return f"{(part / total * 100):.1f}%"


def print_header(title):
    """Print a section header."""
    print()
    print("=" * 60)
    print(f"  {title}")
    print("=" * 60)


def print_row(label, value, total=None):
    """Print a formatted row."""
    if total is not None:
        pct = format_pct(value, total)
        print(f"  {label:<30} {format_number(value):>10}  ({pct})")
    else:
        print(f"  {label:<30} {str(value):>10}")


def main():
    print()
    print("╔════════════════════════════════════════════════════════════╗")
    print("║           SULEKHA PIPELINE STATUS REPORT                   ║")
    print(f"║           {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<43}    ║")
    print("╚════════════════════════════════════════════════════════════╝")

    try:
        session = get_session()
    except Exception as e:
        print(f"\n❌ Failed to connect to database: {e}")
        print("\nMake sure PostgreSQL is running:")
        print("  docker-compose -f docker-compose.dev.yml up -d postgres")
        return 1

    # =========================================================================
    # Scrape Runs
    # =========================================================================
    print_header("SCRAPE RUNS")
    
    result = session.execute(text("""
        SELECT id, status, current_phase, created_at, started_at, completed_at, error_message
        FROM scrape_runs
        ORDER BY created_at DESC
        LIMIT 5
    """))
    runs = result.fetchall()
    
    if not runs:
        print("  No scrape runs found.")
    else:
        for run in runs:
            run_id = str(run[0])[:8]
            status_icon = {"PENDING": "⏳", "RUNNING": "🔄", "COMPLETED": "✅", "FAILED": "❌"}.get(run[1], "❓")
            print(f"  {status_icon} Run {run_id}... | Phase {run[2]} | {run[1]}")
            if run[3]:
                print(f"     Created: {run[3]}")
            if run[6]:
                print(f"     Error: {str(run[6])[:50]}...")

    # =========================================================================
    # Districts (Phase 1)
    # =========================================================================
    print_header("PHASE 1: DISTRICTS")
    
    result = session.execute(text("""
        SELECT status, COUNT(*) as count
        FROM districts
        GROUP BY status
    """))
    district_stats = {row[0]: row[1] for row in result.fetchall()}
    total_districts = sum(district_stats.values())
    
    print_row("Total Districts", total_districts)
    print_row("  PENDING", district_stats.get("PENDING", 0), total_districts)
    print_row("  IN_PROGRESS", district_stats.get("IN_PROGRESS", 0), total_districts)
    print_row("  DONE", district_stats.get("DONE", 0), total_districts)
    print_row("  ERROR", district_stats.get("ERROR", 0), total_districts)
    
    # District-wise completion status with actual scraped project counts
    result = session.execute(text("""
        SELECT 
            d.district_name,
            COUNT(DISTINCT d.id) as total_entries,
            SUM(CASE WHEN d.status = 'DONE' THEN 1 ELSE 0 END) as done,
            SUM(CASE WHEN d.status = 'PENDING' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN d.status = 'ERROR' THEN 1 ELSE 0 END) as errors,
            SUM(d.num_projects) as expected_projects,
            COALESCE((
                SELECT COUNT(*) 
                FROM projects p 
                JOIN local_bodies lb ON p.local_body_id = lb.id 
                JOIN districts d2 ON lb.district_id = d2.id 
                WHERE d2.district_name = d.district_name
            ), 0) as scraped_projects
        FROM districts d
        GROUP BY d.district_name
        ORDER BY d.district_name
    """))
    district_breakdown = result.fetchall()
    if district_breakdown:
        print()
        print("  By District (across all years/LB types):")
        print(f"    {'District':<22} {'Done':>6} {'Pend':>6} {'Err':>5} {'Scraped':>10} {'Expected':>10}")
        print("    " + "-" * 70)
        for row in district_breakdown:
            status_icon = "✅" if row[2] == row[1] else "🔄" if row[3] > 0 else "❌"
            print(f"    {status_icon} {row[0]:<20} {row[2]:>5} {row[3]:>6} {row[4]:>5} {format_number(row[6]):>10} {format_number(row[5]):>10}")

    # Year-wise breakdown with actual scraped counts
    result = session.execute(text("""
        SELECT 
            d.year_label,
            COUNT(DISTINCT d.id) as total,
            SUM(CASE WHEN d.status = 'DONE' THEN 1 ELSE 0 END) as done,
            SUM(d.num_projects) as expected_projects,
            COALESCE((
                SELECT COUNT(*) 
                FROM projects p 
                JOIN local_bodies lb ON p.local_body_id = lb.id 
                JOIN districts d2 ON lb.district_id = d2.id 
                WHERE d2.year_label = d.year_label
            ), 0) as scraped_projects
        FROM districts d
        GROUP BY d.year_label
        ORDER BY d.year_label DESC
    """))
    year_breakdown = result.fetchall()
    if year_breakdown:
        print()
        print("  By Year:")
        for row in year_breakdown:
            pct = f"{(row[2]/row[1]*100):.0f}%" if row[1] > 0 else "0%"
            scraped_pct = f"({row[4]/row[3]*100:.1f}%)" if row[3] > 0 else "(0%)"
            print(f"    {row[0]} | {row[2]:>3}/{row[1]:<3} done ({pct:>4}) | {format_number(row[4]):>10}/{format_number(row[3]):>10} scraped {scraped_pct}")

    # LB Type breakdown with actual scraped counts
    result = session.execute(text("""
        SELECT 
            d.lb_type_label,
            COUNT(DISTINCT d.id) as total,
            SUM(CASE WHEN d.status = 'DONE' THEN 1 ELSE 0 END) as done,
            SUM(d.num_projects) as expected_projects,
            COALESCE((
                SELECT COUNT(*) 
                FROM projects p 
                JOIN local_bodies lb ON p.local_body_id = lb.id 
                JOIN districts d2 ON lb.district_id = d2.id 
                WHERE d2.lb_type_label = d.lb_type_label
            ), 0) as scraped_projects
        FROM districts d
        GROUP BY d.lb_type_label
        ORDER BY d.lb_type_label
    """))
    lb_breakdown = result.fetchall()
    if lb_breakdown:
        print()
        print("  By LB Type:")
        for row in lb_breakdown:
            pct = f"{(row[2]/row[1]*100):.0f}%" if row[1] > 0 else "0%"
            scraped_pct = f"({row[4]/row[3]*100:.1f}%)" if row[3] > 0 else "(0%)"
            print(f"    {row[0]:<20} | {row[2]:>3}/{row[1]:<3} done ({pct:>4}) | {format_number(row[4]):>8}/{format_number(row[3]):>10} scraped")

    # =========================================================================
    # Local Bodies (Phase 2)
    # =========================================================================
    print_header("PHASE 2: LOCAL BODIES")
    
    result = session.execute(text("""
        SELECT status, COUNT(*) as count
        FROM local_bodies
        GROUP BY status
    """))
    lb_stats = {row[0]: row[1] for row in result.fetchall()}
    total_lbs = sum(lb_stats.values())
    
    print_row("Total Local Bodies", total_lbs)
    print_row("  PENDING", lb_stats.get("PENDING", 0), total_lbs)
    print_row("  IN_PROGRESS", lb_stats.get("IN_PROGRESS", 0), total_lbs)
    print_row("  PARTIAL", lb_stats.get("PARTIAL", 0), total_lbs)
    print_row("  DONE", lb_stats.get("DONE", 0), total_lbs)
    print_row("  ERROR", lb_stats.get("ERROR", 0), total_lbs)
    
    # Show scraped projects progress
    result = session.execute(text("""
        SELECT 
            SUM(scraped_projects) as scraped,
            SUM(expected_projects) as expected
        FROM local_bodies
    """))
    lb_progress = result.fetchone()
    if lb_progress:
        scraped = lb_progress[0] or 0
        expected = lb_progress[1] or 0
        print()
        print(f"  Projects Progress: {format_number(scraped)} scraped")
        if expected > 0:
            print(f"                     {format_number(expected)} expected ({scraped/expected*100:.1f}%)")

    # =========================================================================
    # Projects (Phase 3)
    # =========================================================================
    print_header("PHASE 3: PROJECTS")
    
    result = session.execute(text("""
        SELECT COUNT(*) FROM projects
    """))
    total_projects = result.scalar() or 0
    
    result = session.execute(text("""
        SELECT pdf_status, COUNT(*) as count
        FROM projects
        GROUP BY pdf_status
    """))
    project_stats = {row[0]: row[1] for row in result.fetchall()}
    
    print_row("Total Projects", total_projects)
    print_row("  PDF PENDING", project_stats.get("PENDING", 0), total_projects)
    print_row("  PDF DOWNLOADING", project_stats.get("DOWNLOADING", 0), total_projects)
    print_row("  PDF DOWNLOADED", project_stats.get("DOWNLOADED", 0), total_projects)
    print_row("  PDF MISSING", project_stats.get("MISSING", 0), total_projects)
    print_row("  PDF ERROR", project_stats.get("ERROR", 0), total_projects)

    # =========================================================================
    # PDFs (Phase 4)
    # =========================================================================
    print_header("PHASE 4: PDF UPLOADS")
    
    result = session.execute(text("""
        SELECT status, COUNT(*) as count
        FROM pdfs
        GROUP BY status
    """))
    pdf_stats = {row[0]: row[1] for row in result.fetchall()}
    total_pdfs = sum(pdf_stats.values())
    
    print_row("Total PDFs", total_pdfs)
    print_row("  PENDING", pdf_stats.get("PENDING", 0), total_pdfs)
    print_row("  UPLOADED", pdf_stats.get("UPLOADED", 0), total_pdfs)
    print_row("  FAILED", pdf_stats.get("FAILED", 0), total_pdfs)
    
    # Storage stats
    result = session.execute(text("""
        SELECT 
            COUNT(*) as count,
            COALESCE(SUM(file_size_bytes), 0) as total_size
        FROM pdfs
        WHERE status = 'UPLOADED'
    """))
    storage = result.fetchone()
    if storage and storage[1]:
        size_mb = storage[1] / (1024 * 1024)
        print()
        print(f"  Storage Used: {size_mb:,.1f} MB ({storage[0]} files)")

    # =========================================================================
    # Recent Errors
    # =========================================================================
    print_header("RECENT ERRORS")
    
    result = session.execute(text("""
        (SELECT 'District' as type, district_name as name, error_message, last_processed_at as ts
         FROM districts WHERE status = 'ERROR' ORDER BY last_processed_at DESC LIMIT 3)
        UNION ALL
        (SELECT 'LocalBody' as type, lb_name as name, error_message, last_scraped_at as ts
         FROM local_bodies WHERE status = 'ERROR' ORDER BY last_scraped_at DESC LIMIT 3)
        UNION ALL
        (SELECT 'Project' as type, project_name as name, pdf_error_message as error_message, pdf_last_attempt_at as ts
         FROM projects WHERE pdf_status = 'ERROR' ORDER BY pdf_last_attempt_at DESC LIMIT 3)
        ORDER BY ts DESC NULLS LAST
        LIMIT 5
    """))
    errors = result.fetchall()
    
    if not errors:
        print("  ✅ No recent errors!")
    else:
        for err in errors:
            err_msg = (err[2] or "Unknown error")[:60]
            print(f"  ❌ [{err[0]}] {err[1][:30]}...")
            print(f"     {err_msg}")

    # =========================================================================
    # Summary
    # =========================================================================
    print_header("SUMMARY")
    
    phase1_done = district_stats.get("DONE", 0) == total_districts and total_districts > 0
    phase2_done = lb_stats.get("DONE", 0) == total_lbs and total_lbs > 0
    phase3_done = total_projects > 0 and (lb_stats.get("DONE", 0) + lb_stats.get("ERROR", 0)) == total_lbs
    phase4_done = pdf_stats.get("UPLOADED", 0) == total_pdfs and total_pdfs > 0
    
    phase1_status = '✅ Complete' if phase1_done else '🔄 In Progress' if district_stats.get("IN_PROGRESS", 0) > 0 or district_stats.get("PENDING", 0) > 0 else '⏳ Not Started'
    phase2_in_progress = lb_stats.get("IN_PROGRESS", 0) + lb_stats.get("PARTIAL", 0) + lb_stats.get("PENDING", 0)
    phase2_status = '✅ Complete' if phase2_done else '🔄 In Progress' if phase2_in_progress > 0 else '⏳ Not Started'
    phase3_status = '✅ Complete' if phase3_done else '🔄 In Progress' if total_projects > 0 else '⏳ Not Started'
    phase4_status = '✅ Complete' if phase4_done else '🔄 In Progress' if total_pdfs > 0 else '⏳ Not Started'
    
    print(f"  Phase 1 (Districts):    {phase1_status}")
    print(f"  Phase 2 (Local Bodies): {phase2_status}")
    print(f"  Phase 3 (Projects):     {phase3_status}")
    print(f"  Phase 4 (PDFs):         {phase4_status}")
    
    # Try to get queue info from Redis
    try:
        import redis
        r = redis.from_url(os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/0"))
        queues = ["celery", "discovery", "scraper", "pdf_download"]
        total_queued = 0
        queue_info = []
        for q in queues:
            length = r.llen(q)
            if length > 0:
                queue_info.append(f"{q}: {format_number(length)}")
                total_queued += length
        if total_queued > 0:
            print()
            print(f"  Queued Tasks: {format_number(total_queued)} ({', '.join(queue_info)})")
    except Exception:
        pass  # Redis not available
    
    print()
    print("─" * 60)
    print()
    
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
