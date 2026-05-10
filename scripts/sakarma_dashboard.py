"""SAKARMA scrape progress dashboard.

Streamlit UI for live monitoring of the SAKARMA scraper:
  - scrape_run summary
  - LB progress totals (done / in_progress / error / pending) with per-district breakdown
  - artifact totals by type + GCS storage
  - reconciliation diff status
  - recent activity timeline (last 30 LB transitions)
  - per-LB drill-down table

Auto-refreshes every 5 seconds.

Bind to 127.0.0.1 only — exposed via SSH tunnel, never to the public internet:
    gcloud compute ssh sakarma-scraper -- -L 8766:localhost:8766
    open http://localhost:8766
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="SAKARMA Scraper",
    page_icon="📊",
    layout="wide",
)

DATABASE_URL = os.environ.get(
    "SAKARMA_DATABASE_URL",
    os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg://sulekha:sulekha@postgres:5432/sulekha",
    ),
)
REFRESH_SECONDS = int(os.environ.get("SAKARMA_DASHBOARD_REFRESH_SECONDS", "30"))


@st.cache_resource
def get_engine():
    return create_engine(DATABASE_URL, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def latest_run() -> dict | None:
    with get_engine().connect() as c:
        r = c.execute(
            text(
                """
                SELECT id, kind, status, started_at, completed_at, error_summary
                FROM sakarma.scrape_run ORDER BY id DESC LIMIT 1
                """
            )
        ).mappings().first()
        return dict(r) if r else None


def progress_totals(run_id: int) -> dict:
    with get_engine().connect() as c:
        rows = c.execute(
            text(
                """
                SELECT status, count(*) AS n
                FROM sakarma.lb_progress
                WHERE scrape_run_id = :run
                GROUP BY status
                """
            ),
            {"run": run_id},
        ).all()
    out = {"pending": 0, "in_progress": 0, "done": 0, "error": 0}
    for status, n in rows:
        out[status] = n
    out["total"] = sum(out.values())
    return out


def active_lb_count() -> int | None:
    """Count distinct LBs across all currently-running Celery tasks.

    Returns ``None`` if the Celery broker can't be reached. Counts each LB
    once even if it has multiple cell tasks executing in parallel.
    """
    try:
        from celery import Celery
    except ImportError:
        return None
    try:
        broker = os.environ.get("SAKARMA_REDIS_URL", "redis://redis:6379/0")
        app = Celery("sakarma_dashboard_inspect", broker=broker, backend=broker)
        active = app.control.inspect(timeout=2).active() or {}
    except Exception:
        return None
    lbs: set[int] = set()
    for tasks in active.values():
        for t in tasks or []:
            args = t.get("args") or []
            name = t.get("name", "")
            # scrape_lb(run, lb)            -> args[1]
            # scrape_artifacts_cell(run, lb, year, mg) -> args[1]
            # _artifacts_complete(group_results, scrape_run_id=, lb_id=) -> kwargs
            if name.endswith(".scrape_lb") or name.endswith(".scrape_artifacts_cell"):
                if len(args) >= 2:
                    try:
                        lbs.add(int(args[1]))
                    except (TypeError, ValueError):
                        pass
            elif name.endswith("._artifacts_complete"):
                kwargs = t.get("kwargs") or {}
                if "lb_id" in kwargs:
                    try:
                        lbs.add(int(kwargs["lb_id"]))
                    except (TypeError, ValueError):
                        pass
    return len(lbs)


def recent_artifact_rate(run_id: int, minutes: int = 15) -> dict:
    """Return artifacts/min in the trailing window + total artifacts so far."""
    sql = text(
        """
        WITH window_arts AS (
          SELECT a.id, a.byte_size
          FROM sakarma.meeting_artifact a
          WHERE a.scrape_run_id = :run
            AND a.fetched_at > NOW() - make_interval(mins => :mins)
        ),
        all_arts AS (
          SELECT COUNT(*) AS total_artifacts,
                 SUM(byte_size) AS total_bytes
          FROM sakarma.meeting_artifact a
          WHERE a.scrape_run_id = :run
        )
        SELECT
          (SELECT COUNT(*) FROM window_arts) AS recent_count,
          (SELECT SUM(byte_size) FROM window_arts) AS recent_bytes,
          (SELECT total_artifacts FROM all_arts) AS total_artifacts,
          (SELECT total_bytes FROM all_arts) AS total_bytes
        """
    )
    with get_engine().connect() as c:
        row = c.execute(sql, {"run": run_id, "mins": minutes}).mappings().first()
    return dict(row or {})


def expected_total_meetings(run_id: int) -> int:
    """Sum of total_meetings across all KPI snapshots — what the source
    *says* exists for this run. Used as the denominator for an ETA based
    on meetings scraped rather than LBs done.
    """
    with get_engine().connect() as c:
        return int(
            c.execute(
                text(
                    "SELECT COALESCE(SUM(total_meetings), 0) "
                    "FROM sakarma.dashboard_kpi_snapshot WHERE scrape_run_id = :run"
                ),
                {"run": run_id},
            ).scalar()
            or 0
        )


def meetings_with_artifacts(run_id: int) -> int:
    """Distinct manifest rows that have at least one artifact uploaded."""
    with get_engine().connect() as c:
        return int(
            c.execute(
                text(
                    "SELECT COUNT(DISTINCT a.meeting_manifest_id) "
                    "FROM sakarma.meeting_artifact a "
                    "WHERE a.scrape_run_id = :run"
                ),
                {"run": run_id},
            ).scalar()
            or 0
        )


def stage_breakdown(run_id: int) -> pd.DataFrame:
    sql = text(
        """
        SELECT
            COALESCE(current_stage::text, '(no stage)') AS stage,
            count(*) AS n
        FROM sakarma.lb_progress
        WHERE scrape_run_id = :run AND status = 'in_progress'
        GROUP BY current_stage
        ORDER BY n DESC
        """
    )
    return pd.read_sql(sql, get_engine(), params={"run": run_id})


def per_district(run_id: int) -> pd.DataFrame:
    sql = text(
        """
        SELECT
            d.name_ml AS district,
            count(*) AS total,
            sum(CASE WHEN p.status='done' THEN 1 ELSE 0 END) AS done,
            sum(CASE WHEN p.status='in_progress' THEN 1 ELSE 0 END) AS in_progress,
            sum(CASE WHEN p.status='error' THEN 1 ELSE 0 END) AS error,
            sum(CASE WHEN p.status='pending' THEN 1 ELSE 0 END) AS pending
        FROM sakarma.lb_progress p
        JOIN sakarma.lb lb ON lb.id = p.lb_id
        JOIN sakarma.district d ON d.id = lb.district_id
        WHERE p.scrape_run_id = :run
        GROUP BY d.name_ml, d.id
        ORDER BY d.id
        """
    )
    return pd.read_sql(sql, get_engine(), params={"run": run_id})


def artifact_totals(run_id: int) -> pd.DataFrame:
    sql = text(
        """
        SELECT
            artifact_type::text AS artifact_type,
            count(*) AS rows,
            count(DISTINCT content_hash) AS distinct_hashes,
            sum(byte_size) AS total_bytes
        FROM sakarma.meeting_artifact
        WHERE scrape_run_id = :run
        GROUP BY artifact_type
        ORDER BY artifact_type
        """
    )
    return pd.read_sql(sql, get_engine(), params={"run": run_id})


def reconciliation_breakdown(run_id: int) -> pd.DataFrame:
    sql = text(
        """
        SELECT status::text AS status, count(*) AS n
        FROM sakarma.reconciliation
        WHERE scrape_run_id = :run
        GROUP BY status
        ORDER BY n DESC
        """
    )
    return pd.read_sql(sql, get_engine(), params={"run": run_id})


def recent_activity(run_id: int, limit: int = 25) -> pd.DataFrame:
    sql = text(
        """
        SELECT
            COALESCE(p.completed_at, p.started_at) AS ts,
            p.lb_id,
            lb.name_ml AS lb_name,
            d.name_ml AS district,
            p.status::text AS status,
            COALESCE(p.current_stage::text, '') AS stage,
            CASE WHEN p.completed_at IS NOT NULL AND p.started_at IS NOT NULL
                 THEN EXTRACT(EPOCH FROM (p.completed_at - p.started_at))::int
                 ELSE NULL END AS elapsed_s,
            COALESCE(p.error_message, '') AS error_message
        FROM sakarma.lb_progress p
        JOIN sakarma.lb lb ON lb.id = p.lb_id
        JOIN sakarma.district d ON d.id = lb.district_id
        WHERE p.scrape_run_id = :run AND p.status IN ('done', 'error', 'in_progress')
        ORDER BY COALESCE(p.completed_at, p.started_at) DESC NULLS LAST
        LIMIT :lim
        """
    )
    return pd.read_sql(sql, get_engine(), params={"run": run_id, "lim": limit})


def errored_lbs(run_id: int) -> pd.DataFrame:
    sql = text(
        """
        SELECT
            p.lb_id,
            lb.name_ml AS lb_name,
            d.name_ml AS district,
            p.current_stage::text AS stage,
            p.error_message,
            p.started_at,
            p.completed_at
        FROM sakarma.lb_progress p
        JOIN sakarma.lb lb ON lb.id = p.lb_id
        JOIN sakarma.district d ON d.id = lb.district_id
        WHERE p.scrape_run_id = :run AND p.status = 'error'
        ORDER BY p.completed_at DESC NULLS LAST
        LIMIT 200
        """
    )
    return pd.read_sql(sql, get_engine(), params={"run": run_id})


def kpi_vs_manifest(run_id: int) -> dict:
    """Top-line: total dashboard KPI count vs total parsed manifest count."""
    with get_engine().connect() as c:
        kpi = c.execute(
            text(
                """
                SELECT
                    COALESCE(sum(total_meetings),0) AS total_kpi,
                    COALESCE(sum(minutes_complete),0) AS approved_kpi
                FROM sakarma.dashboard_kpi_snapshot
                WHERE scrape_run_id = :run
                """
            ),
            {"run": run_id},
        ).mappings().first()
        manifest = c.execute(
            text(
                """
                SELECT count(*) AS approved_manifest
                FROM sakarma.meeting_manifest
                WHERE scrape_run_id = :run AND category = 2
                """
            ),
            {"run": run_id},
        ).mappings().first()
    return {**dict(kpi or {}), **dict(manifest or {})}


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def fmt_dt(dt) -> str:
    """Format any datetime-like value (Python datetime, pandas Timestamp, str,
    None/NaT) as a local-timezone string."""
    if dt is None:
        return "—"
    # pandas NaT short-circuit
    if pd.isna(dt):
        return "—"
    if isinstance(dt, str):
        return dt
    # pandas.Timestamp.astimezone() requires an explicit tz argument; the
    # Python-datetime variant doesn't. Normalise to Python datetime first.
    if hasattr(dt, "to_pydatetime"):
        dt = dt.to_pydatetime()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(dt)


# Header
run = latest_run()
if run is None:
    st.title("SAKARMA Scraper")
    st.warning("No scrape runs yet. Trigger one with `sakarma backfill`.")
    st.stop()

elapsed = (
    (run["completed_at"] or datetime.now(timezone.utc)) - run["started_at"]
    if run["started_at"] else None
)
elapsed_h = elapsed.total_seconds() / 3600 if elapsed else None

st.title("SAKARMA Scraper")
hdr = st.container()
with hdr:
    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    c1.metric("Run", f"#{run['id']}")
    c2.metric("Kind", run["kind"])
    status_color = {"running": "🟢", "done": "✅", "failed": "🔴"}.get(run["status"], "⚪")
    c3.metric("Status", f"{status_color} {run['status']}")
    if elapsed_h is not None:
        c4.metric("Elapsed", f"{elapsed_h:.2f} h")

# --- LB progress ---
st.divider()
totals = progress_totals(run["id"])

active_count = active_lb_count()
active_display = active_count if active_count is not None else "—"

st.subheader("Local Body progress")
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total LBs", totals["total"])
c2.metric("✅ Done", totals["done"])
c3.metric(
    "🟢 Active",
    active_display,
    help="LBs with at least one Celery task currently executing on a worker fork.",
)
c4.metric("🔴 Error", totals["error"], delta_color="inverse")
c5.metric("⏳ Pending", totals["pending"])

# Throughput — measured at multiple grains because LB/hr is misleading
# during multi-year runs (each LB takes much longer than during a single-
# year run, so the same wall-clock effort can show 10× lower LB/hr).
window_min = 15
recent = recent_artifact_rate(run["id"], minutes=window_min)
recent_count = int(recent.get("recent_count") or 0)
recent_bytes = int(recent.get("recent_bytes") or 0)
total_artifacts = int(recent.get("total_artifacts") or 0)
total_bytes = int(recent.get("total_bytes") or 0)

art_per_hr = (recent_count / window_min) * 60 if recent_count else 0
bytes_per_hr = (recent_bytes / window_min) * 60 if recent_bytes else 0

# Coverage progress against what the source claims exists
expected = expected_total_meetings(run["id"])
captured_meetings = meetings_with_artifacts(run["id"])
coverage_pct = (captured_meetings / expected * 100) if expected > 0 else 0

# ETA based on artifact rate vs estimated total artifacts (~3 per meeting:
# minutes_html + dr_html + ~1 attachment on average)
expected_artifacts = expected * 3 if expected > 0 else 0
remaining_artifacts = max(expected_artifacts - total_artifacts, 0)
eta_hr_artifacts = (
    remaining_artifacts / art_per_hr if art_per_hr > 0 else None
)

cc1, cc2, cc3, cc4 = st.columns(4)
cc1.metric(
    f"Artifacts / hr (last {window_min} min)",
    f"{art_per_hr:,.0f}",
    help="Real upload rate — most accurate measure of forward progress.",
)
cc2.metric(
    "Bytes / hr (recent)",
    fmt_bytes(int(bytes_per_hr)) if bytes_per_hr else "0 B",
)
cc3.metric(
    "Meetings captured",
    f"{captured_meetings:,} / {expected:,}",
    help=(
        f"Distinct meetings with at least one artifact uploaded vs the "
        f"sum of total_meetings across all KPI snapshots ({coverage_pct:.1f}% complete)."
    ),
)
if eta_hr_artifacts is not None:
    cc4.metric("ETA (artifact pace)", f"{eta_hr_artifacts:.1f} h")
else:
    cc4.metric("ETA (artifact pace)", "—")

# --- in-flight stage breakdown ---
stages = stage_breakdown(run["id"])
if not stages.empty:
    st.caption("In-flight LBs by stage")
    st.dataframe(stages, use_container_width=True, hide_index=True)

# --- artifact totals ---
st.divider()
st.subheader("Artifacts uploaded to GCS")
art = artifact_totals(run["id"])
if not art.empty:
    art["total_bytes"] = art["total_bytes"].apply(fmt_bytes)
    art["dedup_ratio"] = (
        (art["distinct_hashes"] / art["rows"]).round(4).astype(str)
    )
    st.dataframe(art, use_container_width=True, hide_index=True)
    total_rows = int(art["rows"].sum())
    st.caption(f"Total artifacts: {total_rows:,}")
else:
    st.info("No artifacts uploaded yet.")

# --- reconciliation ---
st.divider()
st.subheader("Reconciliation")
recon = reconciliation_breakdown(run["id"])
kpi_vs = kpi_vs_manifest(run["id"])
if not recon.empty:
    st.dataframe(recon, use_container_width=True, hide_index=True)
    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Approved KPI total", int(kpi_vs.get("approved_kpi") or 0))
    cc2.metric("Approved manifest total", int(kpi_vs.get("approved_manifest") or 0))
    cc3.metric(
        "Total meetings (KPI)",
        int(kpi_vs.get("total_kpi") or 0),
        help="Sum of dashboard 'total meetings' counter across all (lb × year × group) cells.",
    )
else:
    st.info("Reconciliation rows will appear after the first LB completes its full pipeline.")

# --- per-district progress ---
st.divider()
st.subheader("Per-district progress")
pd_df = per_district(run["id"])
if not pd_df.empty:
    pd_df["pct_done"] = (pd_df["done"] / pd_df["total"] * 100).round(1)
    st.dataframe(pd_df, use_container_width=True, hide_index=True)

# --- recent activity ---
st.divider()
st.subheader("Recent activity")
ra = recent_activity(run["id"], limit=25)
if not ra.empty:
    ra["ts"] = ra["ts"].apply(fmt_dt)
    st.dataframe(ra, use_container_width=True, hide_index=True)
else:
    st.caption("No activity recorded yet.")

# --- errored LBs ---
err = errored_lbs(run["id"])
if not err.empty:
    st.divider()
    st.subheader(f"🔴 Errored LBs ({len(err)})")
    err["started_at"] = err["started_at"].apply(fmt_dt)
    err["completed_at"] = err["completed_at"].apply(fmt_dt)
    st.dataframe(err, use_container_width=True, hide_index=True)

# --- footer + auto-refresh ---
st.divider()
st.caption(
    f"Refreshes every {REFRESH_SECONDS}s · DB: {DATABASE_URL.rsplit('@', 1)[-1]} · "
    f"Updated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
)

# Streamlit's experimental_rerun-by-timeout is the simplest auto-refresh
# mechanism. Use it via the streamlit-autorefresh component if installed,
# otherwise fall back to a manual JS-based reload.
try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(interval=REFRESH_SECONDS * 1000, key="sakarma-dashboard-refresh")
except ImportError:
    st.markdown(
        f"""<meta http-equiv="refresh" content="{REFRESH_SECONDS}" />""",
        unsafe_allow_html=True,
    )
