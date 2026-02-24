"""Sulekha PDF Dashboard - Real-time monitoring of PDF download progress."""

import os
import time
from datetime import datetime, timedelta

import streamlit as st
from sqlalchemy import create_engine, text

# Page config
st.set_page_config(
    page_title="Sulekha PDF Dashboard",
    page_icon="📄",
    layout="wide",
)

# Database connection
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg://sulekha:sulekha@postgres:5432/sulekha"
)

@st.cache_resource
def get_engine():
    return create_engine(DATABASE_URL)


def get_pdf_stats():
    """Get overall PDF statistics."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT 
                pdf_status,
                COUNT(*) as count
            FROM projects
            GROUP BY pdf_status
        """))
        stats = {row[0]: row[1] for row in result}
    return stats


def get_stats_by_year():
    """Get PDF statistics broken down by year."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT 
                d.year_label,
                COUNT(*) as total,
                SUM(CASE WHEN p.pdf_status = 'DOWNLOADED' THEN 1 ELSE 0 END) as downloaded,
                SUM(CASE WHEN p.pdf_status = 'MISSING' THEN 1 ELSE 0 END) as missing,
                SUM(CASE WHEN p.pdf_status = 'ERROR' THEN 1 ELSE 0 END) as failed,
                SUM(CASE WHEN p.pdf_status IN ('DOWNLOADED', 'MISSING', 'ERROR') THEN 1 ELSE 0 END) as attempted
            FROM projects p
            JOIN local_bodies lb ON p.local_body_id = lb.id
            JOIN districts d ON lb.district_id = d.id
            GROUP BY d.year_label
            ORDER BY d.year_label DESC
        """))
        return [dict(row._mapping) for row in result]


def get_fail_rate_by_minutes(minutes: int):
    """Get fail rate (MISSING+ERROR)/attempted for projects completed in last N minutes."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(f"""
            SELECT
                COUNT(*) FILTER (WHERE pdf_status IN ('DOWNLOADED', 'MISSING', 'ERROR')) AS attempted,
                COUNT(*) FILTER (WHERE pdf_status IN ('MISSING', 'ERROR')) AS failed
            FROM projects
            WHERE pdf_last_attempt_at >= NOW() - INTERVAL '{minutes} minutes'
        """))
        row = result.fetchone()
        attempted = row[0] or 0
        failed = row[1] or 0
        pct = (failed / attempted * 100) if attempted > 0 else None
        return attempted, failed, pct


def get_upload_rate(minutes: int):
    """Get upload rate for the last N minutes."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text(f"""
            SELECT COUNT(*) as count
            FROM pdfs
            WHERE status = 'UPLOADED' AND uploaded_at > NOW() - INTERVAL '{minutes} minutes'
        """))
        count = result.scalar() or 0
        return count, round(count / minutes, 1) if minutes > 0 else 0


def get_hourly_rate():
    """Get uploads in the last hour."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT COUNT(*) as count
            FROM pdfs
            WHERE status = 'UPLOADED' AND uploaded_at > NOW() - INTERVAL '1 hour'
        """))
        return result.scalar() or 0


def get_total_uploaded():
    """Get total PDFs uploaded to GCS."""
    engine = get_engine()
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT COUNT(*) FROM pdfs WHERE status = 'UPLOADED'
        """))
        return result.scalar() or 0


def get_storage_stats():
    """Get storage statistics."""
    engine = get_engine()
    with engine.connect() as conn:
        # Total storage
        result = conn.execute(text("""
            SELECT COALESCE(SUM(file_size_bytes), 0) FROM pdfs WHERE status = 'UPLOADED'
        """))
        total_bytes = result.scalar() or 0
        
        # Storage added in last hour
        result = conn.execute(text("""
            SELECT COALESCE(SUM(file_size_bytes), 0) 
            FROM pdfs 
            WHERE status = 'UPLOADED' AND uploaded_at > NOW() - INTERVAL '1 hour'
        """))
        hourly_bytes = result.scalar() or 0
        
    return total_bytes, hourly_bytes


def format_bytes(bytes_val):
    """Format bytes to human readable."""
    if bytes_val >= 1024**3:
        return f"{bytes_val / 1024**3:.1f} GB"
    elif bytes_val >= 1024**2:
        return f"{bytes_val / 1024**2:.1f} MB"
    elif bytes_val >= 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val} B"


def main():
    st.title("📄 Sulekha PDF Dashboard")
    
    # Auto-refresh every 30 seconds
    refresh_interval = 30
    
    # Get all stats
    pdf_stats = get_pdf_stats()
    total_uploaded = get_total_uploaded()
    hourly_rate = get_hourly_rate()
    total_bytes, hourly_bytes = get_storage_stats()
    
    # Calculate totals
    total_projects = sum(pdf_stats.values())
    downloaded = pdf_stats.get("DOWNLOADED", 0)
    missing = pdf_stats.get("MISSING", 0)
    pending = pdf_stats.get("PENDING", 0)
    downloading = pdf_stats.get("DOWNLOADING", 0)
    errors = pdf_stats.get("ERROR", 0)
    
    # Get rates at different intervals (uploads)
    count_5, rate_5 = get_upload_rate(5)
    count_10, rate_10 = get_upload_rate(10)
    count_15, rate_15 = get_upload_rate(15)
    count_30, rate_30 = get_upload_rate(30)
    
    # Get fail rates (project attempts in last N mins - different from uploads)
    attempted_5, failed_5, fail_pct_5 = get_fail_rate_by_minutes(5)
    attempted_10, failed_10, fail_pct_10 = get_fail_rate_by_minutes(10)
    attempted_15, failed_15, fail_pct_15 = get_fail_rate_by_minutes(15)
    
    # Calculate hours remaining
    remaining = pending + downloading + errors
    if rate_30 > 0:
        hours_remaining = remaining / (rate_30 * 60)
    else:
        hours_remaining = float('inf')
    
    # Row 1: Upload stats
    st.markdown("---")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    
    with col1:
        st.metric("A) UPLOADED SO FAR", f"{total_uploaded:,}", "PDFs in GCP")
    with col2:
        st.metric("B) UPLOADED (LAST HOUR)", f"{hourly_rate:,}", f"PDFs / hr")
    with col3:
        st.metric("LAST 5 MINS", f"{count_5:,}", "PDFs")
    with col4:
        st.metric("LAST 10 MINS", f"{count_10:,}", "PDFs")
    with col5:
        st.metric("LAST 15 MINS", f"{count_15:,}", "PDFs")
    with col6:
        st.metric("LAST 30 MINS", f"{count_30:,}", "PDFs")
    
    # Row 2: Storage and estimates
    st.markdown("---")
    col1, col2, col3, col4, col5, col6 = st.columns(6)
    
    with col1:
        st.metric("C) SPACE USED", format_bytes(total_bytes), "Total storage")
    with col2:
        st.metric("D) SPACE (LAST HOUR)", format_bytes(hourly_bytes), "Added in last hour")
    with col3:
        if hours_remaining != float('inf'):
            st.metric("E) HOURS REMAINING", f"{hours_remaining:.0f}", "At current rate")
        else:
            st.metric("E) HOURS REMAINING", "∞", "No recent activity")
    with col4:
        fail_rate_5 = fail_pct_5 if fail_pct_5 is not None else 0
        st.metric("FAIL RATE (5 MIN)", f"{fail_rate_5:.1f}%", f"{attempted_5:,} attempted")
    with col5:
        fail_rate_10 = fail_pct_10 if fail_pct_10 is not None else 0
        st.metric("FAIL RATE (10 MIN)", f"{fail_rate_10:.1f}%", f"{attempted_10:,} attempted")
    with col6:
        fail_rate_15 = fail_pct_15 if fail_pct_15 is not None else 0
        st.metric("FAIL RATE (15 MIN)", f"{fail_rate_15:.1f}%", f"{attempted_15:,} attempted")
    
    # Row 3: Status breakdown
    st.markdown("---")
    col1, col2, col3, col4, col5 = st.columns(5)
    
    with col1:
        st.metric("Total Projects", f"{total_projects:,}")
    with col2:
        pct = (downloaded / total_projects * 100) if total_projects > 0 else 0
        st.metric("Downloaded", f"{downloaded:,}", delta=f"{pct:.1f}%")
    with col3:
        st.metric("Missing", f"{missing:,}")
    with col4:
        st.metric("Pending", f"{pending:,}")
    with col5:
        st.metric("Errors", f"{errors:,}")
    
    # Year-by-year breakdown
    st.markdown("---")
    st.subheader("By Year")
    
    year_stats = get_stats_by_year()
    
    if year_stats:
        # Create table data - Fail % = (MISSING + ERROR) / Attempted
        table_data = []
        sum_total = sum_missing = sum_errors = sum_failed = sum_attempted = sum_downloaded = 0
        for row in year_stats:
            total = row['total'] or 0
            attempted = row['attempted'] or 0
            downloaded = row['downloaded'] or 0
            missing_count = row['missing'] or 0
            errors_count = row['failed'] or 0  # ERROR status only
            failed_total = missing_count + errors_count  # Total failures = MISSING + ERROR
            fail_pct = (failed_total / attempted * 100) if attempted > 0 else 0
            attempted_pct = (attempted / total * 100) if total > 0 else 0
            sum_total += total
            sum_downloaded += downloaded
            sum_missing += missing_count
            sum_errors += errors_count
            sum_failed += failed_total
            sum_attempted += attempted
            table_data.append({
                "Year": row['year_label'],
                "Total": f"{total:,}",
                "Downloaded": f"{downloaded:,}",
                "Missing": f"{missing_count:,}",
                "Errors": f"{errors_count:,}",
                "Failed (M+E)": f"{failed_total:,}",
                "Attempted": f"{attempted:,}",
                "Attempted %": f"{attempted_pct:.1f}%",
                "Fail %": f"{fail_pct:.1f}%"
            })
        
        # Totals row
        total_attempted_pct = (sum_attempted / sum_total * 100) if sum_total > 0 else 0
        total_fail_pct = (sum_failed / sum_attempted * 100) if sum_attempted > 0 else 0
        table_data.append({
            "Year": "TOTAL",
            "Total": f"{sum_total:,}",
            "Downloaded": f"{sum_downloaded:,}",
            "Missing": f"{sum_missing:,}",
            "Errors": f"{sum_errors:,}",
            "Failed (M+E)": f"{sum_failed:,}",
            "Attempted": f"{sum_attempted:,}",
            "Attempted %": f"{total_attempted_pct:.1f}%",
            "Fail %": f"{total_fail_pct:.1f}%"
        })
        
        st.dataframe(
            table_data,
            use_container_width=True,
            hide_index=True,
        )
    
    # Footer with last update time
    st.markdown("---")
    st.caption(f"Last updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | Auto-refresh every {refresh_interval}s")
    
    # Auto-refresh
    time.sleep(refresh_interval)
    st.rerun()


if __name__ == "__main__":
    main()
