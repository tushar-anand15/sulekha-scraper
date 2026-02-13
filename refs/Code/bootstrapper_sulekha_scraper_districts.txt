import os
import re
import csv
import sqlite3
from datetime import datetime, timezone

# ----------------- CONFIG -----------------

OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
DB_PATH = os.path.join(OUT_DIR, "sulekha_progress.sqlite")

# Matches your filename pattern:
# y{year_val}__lb{lb_val}__{district_index}_{district_name}__{lb_index}_{lb_name}.csv
FILENAME_RE = re.compile(
    r"^y(?P<year>\d+)__lb(?P<lbtype>\d+)__"
    r"(?P<district_slug>.+?)__"
    r"(?P<lb_slug>.+?)\.csv$",
    re.IGNORECASE
)

def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()

def safe_int(x):
    try:
        return int(str(x).strip())
    except Exception:
        return None

def parse_index_and_name_from_slug(slug: str):
    """
    slug example: "5_Kasargod" or "1_Thiruvananthapuram_District_Panchayat"
    returns (index:int|None, name:str)
    """
    if not slug:
        return None, ""
    parts = slug.split("_", 1)
    if len(parts) == 1:
        return None, slug.replace("_", " ").strip()
    idx = safe_int(parts[0])
    name = parts[1].replace("_", " ").strip()
    return idx, name

def make_lb_key(year_val, lb_val, district_index, lb_index):
    return f"{year_val}|{lb_val}|{district_index}|{lb_index}"

def db_connect():
    os.makedirs(OUT_DIR, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")

    con.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            key TEXT PRIMARY KEY,
            year_val INTEGER,
            lb_val INTEGER,
            district_index INTEGER,
            district_name TEXT,
            lb_index INTEGER,
            lb_name TEXT,
            csv_path TEXT NOT NULL,
            expected INTEGER,
            scraped INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PARTIAL',
            updated_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_progress_status ON progress(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_progress_district ON progress(year_val, lb_val, district_index)")

    con.execute("""
        CREATE TABLE IF NOT EXISTS district_summary (
            district_key TEXT PRIMARY KEY,
            year_val INTEGER,
            lb_val INTEGER,
            district_index INTEGER,
            district_name TEXT,
            observed_lbs INTEGER DEFAULT 0,
            done_lbs INTEGER DEFAULT 0,
            observed_projects INTEGER DEFAULT 0,
            done_projects INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_district_summary ON district_summary(year_val, lb_val, district_index)")

    return con

def count_csv_rows_and_expected(csv_path):
    """
    Returns (scraped_rows, expected_projects).
    - scraped_rows = number of data rows (excluding header)
    - expected_projects is read from column 'expected_projects' if present
      (we take the first non-empty value we see).
    """
    scraped = 0
    expected = None

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        # If file is empty or header missing
        if reader.fieldnames is None:
            return 0, None

        for row in reader:
            scraped += 1
            if expected is None:
                val = row.get("expected_projects")
                e = safe_int(val)
                if e is not None:
                    expected = e

    return scraped, expected

def upsert_progress(con, rec):
    con.execute("""
        INSERT INTO progress (
            key, year_val, lb_val, district_index, district_name,
            lb_index, lb_name, csv_path, expected, scraped, status, updated_at
        )
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(key) DO UPDATE SET
            year_val=excluded.year_val,
            lb_val=excluded.lb_val,
            district_index=excluded.district_index,
            district_name=excluded.district_name,
            lb_index=excluded.lb_index,
            lb_name=excluded.lb_name,
            csv_path=excluded.csv_path,
            expected=excluded.expected,
            scraped=excluded.scraped,
            status=excluded.status,
            updated_at=excluded.updated_at
    """, (
        rec["key"],
        rec["year_val"],
        rec["lb_val"],
        rec["district_index"],
        rec["district_name"],
        rec["lb_index"],
        rec["lb_name"],
        rec["csv_path"],
        rec["expected"],
        rec["scraped"],
        rec["status"],
        rec["updated_at"],
    ))

def rebuild_district_summary(con):
    """
    district_summary is derived from progress (LB-level).
    This DOES NOT use gvState expected_lbs/projects (because those are online),
    but it’s still useful to skip districts where ALL observed LBs are DONE.
    """
    now = utc_now_iso()

    con.execute("DELETE FROM district_summary")

    rows = con.execute("""
        SELECT
            year_val, lb_val, district_index,
            COALESCE(MAX(district_name), '') as district_name,
            COUNT(*) as observed_lbs,
            SUM(CASE WHEN status='DONE' THEN 1 ELSE 0 END) as done_lbs,
            SUM(scraped) as observed_projects,
            SUM(CASE WHEN status='DONE' THEN scraped ELSE 0 END) as done_projects
        FROM progress
        GROUP BY year_val, lb_val, district_index
    """).fetchall()

    for (year_val, lb_val, district_index, district_name,
         observed_lbs, done_lbs, observed_projects, done_projects) in rows:
        district_key = f"{year_val}|{lb_val}|{district_index}"
        con.execute("""
            INSERT INTO district_summary(
                district_key, year_val, lb_val, district_index, district_name,
                observed_lbs, done_lbs, observed_projects, done_projects, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            district_key, year_val, lb_val, district_index, district_name,
            observed_lbs, done_lbs, observed_projects, done_projects, now
        ))

def bootstrap():
    print("🔹 Bootstrapper starting")
    print("🔹 OUT_DIR:", os.path.abspath(OUT_DIR))
    print("🔹 DB_PATH:", os.path.abspath(DB_PATH))

    con = db_connect()

    # Optional: wipe LB progress and rebuild from scratch
    con.execute("DELETE FROM progress")
    con.commit()

    files = [f for f in os.listdir(OUT_DIR) if f.lower().endswith(".csv")]
    print(f"🔎 Found {len(files)} CSVs in folder")

    inserted = 0
    skipped = 0
    for fname in files:
        m = FILENAME_RE.match(fname)
        if not m:
            skipped += 1
            continue

        year_val = safe_int(m.group("year"))
        lb_val = safe_int(m.group("lbtype"))

        district_slug = m.group("district_slug")
        lb_slug = m.group("lb_slug")

        district_index, district_name = parse_index_and_name_from_slug(district_slug)
        lb_index, lb_name = parse_index_and_name_from_slug(lb_slug)

        # If indices are missing, we can’t create stable keys
        if year_val is None or lb_val is None or district_index is None or lb_index is None:
            skipped += 1
            continue

        csv_path = os.path.join(OUT_DIR, fname)

        try:
            scraped, expected = count_csv_rows_and_expected(csv_path)
        except Exception as e:
            print(f"⚠ Failed reading {fname}: {e}")
            skipped += 1
            continue

        status = "PARTIAL"
        if expected is not None and expected > 0 and scraped >= expected:
            status = "DONE"

        rec = {
            "key": make_lb_key(year_val, lb_val, district_index, lb_index),
            "year_val": year_val,
            "lb_val": lb_val,
            "district_index": district_index,
            "district_name": district_name,
            "lb_index": lb_index,
            "lb_name": lb_name,
            "csv_path": csv_path,
            "expected": expected,
            "scraped": scraped,
            "status": status,
            "updated_at": utc_now_iso(),
        }

        upsert_progress(con, rec)
        inserted += 1

        if inserted % 250 == 0:
            con.commit()
            print(f"… processed {inserted} CSVs")

    con.commit()

    # Build district summary
    rebuild_district_summary(con)
    con.commit()

    # Quick stats
    done = con.execute("SELECT COUNT(*) FROM progress WHERE status='DONE'").fetchone()[0]
    total = con.execute("SELECT COUNT(*) FROM progress").fetchone()[0]

    print("✅ Bootstrap complete")
    print(f"   Inserted into DB: {inserted}")
    print(f"   Skipped (non-matching/invalid): {skipped}")
    print(f"   DONE LBs: {done}/{total}")
    print(f"📌 DB saved at: {os.path.abspath(DB_PATH)}")

    con.close()

if __name__ == "__main__":
    bootstrap()
