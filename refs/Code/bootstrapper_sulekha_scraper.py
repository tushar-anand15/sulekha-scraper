import os
import csv
import sqlite3
from datetime import datetime

OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
DB_PATH = os.path.join(OUT_DIR, "sulekha_progress.sqlite")

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            key TEXT PRIMARY KEY,
            csv_path TEXT NOT NULL,
            expected INTEGER,
            scraped INTEGER DEFAULT 0,
            status TEXT DEFAULT 'PARTIAL',
            updated_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_status ON progress(status)")
    return con

def make_key(year_val, lb_val, district_index, lb_index):
    return f"{year_val}|{lb_val}|{district_index}|{lb_index}"

def upsert(con, key, csv_path, expected, scraped, status):
    now = datetime.utcnow().isoformat()
    con.execute("""
        INSERT INTO progress(key, csv_path, expected, scraped, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            csv_path=excluded.csv_path,
            expected=COALESCE(excluded.expected, progress.expected),
            scraped=excluded.scraped,
            status=excluded.status,
            updated_at=excluded.updated_at
    """, (key, csv_path, expected, scraped, status, now))

def scan_one_csv(csv_path):
    """
    Returns: (year_val, lb_val, district_index, lb_index, expected, scraped)
    """
    year_val = lb_val = district_index = lb_index = None
    expected = None
    scraped = 0

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        header = next(r, None)
        if not header:
            return None

        # Stream rows; keep first data row to grab expected + ids
        first_data = None
        for row in r:
            if not row:
                continue
            # skip empty/short lines
            if len(row) < 9:
                continue
            if first_data is None:
                first_data = row

            scraped += 1

        if first_data is None:
            # header only
            return None

        # Your column order:
        # 0 year_value
        # 2 lbtype_value
        # 4 district_index
        # 6 localbody_index
        # 8 expected_projects
        def to_int(x):
            try:
                x = (x or "").strip()
                return int(x) if x != "" else None
            except Exception:
                return None

        year_val = to_int(first_data[0])
        lb_val = to_int(first_data[2])
        district_index = to_int(first_data[4])
        lb_index = to_int(first_data[6])
        expected = to_int(first_data[8])

    if None in (year_val, lb_val, district_index, lb_index):
        # can't build key reliably
        return None

    return year_val, lb_val, district_index, lb_index, expected, scraped

def main():
    if not os.path.isdir(OUT_DIR):
        raise RuntimeError(f"Folder not found: {OUT_DIR}")

    con = db_connect()

    csv_files = [
        os.path.join(OUT_DIR, fn)
        for fn in os.listdir(OUT_DIR)
        if fn.lower().endswith(".csv")
    ]

    print(f"Found {len(csv_files)} CSVs in: {OUT_DIR}")
    updated = 0
    skipped = 0

    for i, path in enumerate(csv_files, start=1):
        info = scan_one_csv(path)
        if not info:
            skipped += 1
            continue

        year_val, lb_val, district_index, lb_index, expected, scraped = info
        key = make_key(year_val, lb_val, district_index, lb_index)

        status = "PARTIAL"
        if expected is not None and expected > 0 and scraped >= expected:
            status = "DONE"

        upsert(con, key, path, expected, scraped, status)
        updated += 1

        if i % 200 == 0:
            con.commit()
            print(f"  Processed {i}/{len(csv_files)}...")

    con.commit()
    con.close()

    print("✅ Bootstrap complete.")
    print(f"✅ Updated/inserted: {updated}")
    print(f"⚠ Skipped (empty/unparseable): {skipped}")
    print(f"📌 DB written to: {DB_PATH}")

if __name__ == "__main__":
    main()
