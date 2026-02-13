import csv
import os
import re
import time
import random
import sqlite3
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ================= CONFIG =================

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
os.makedirs(OUT_DIR, exist_ok=True)

OUT_CSV = os.path.join(OUT_DIR, "district_localbody_summary.csv")
DB_PATH = os.path.join(OUT_DIR, "district_localbody_summary_progress.sqlite")

REQUEST_TIMEOUT = 60
DELAY_BETWEEN_REQUESTS = 0.8

MAX_RETRIES = 8
BACKOFF_BASE = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 180.0

POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")

print("🔹 Running sulekha_district_lb_summary_scraper_v1.py")
print("🔹 OUT_DIR:", os.path.abspath(OUT_DIR))
print("🔹 OUT_CSV:", os.path.abspath(OUT_CSV))
print("🔹 DB_PATH:", os.path.abspath(DB_PATH))


# ================= SQLITE PROGRESS =================

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-20000;")

    # One row per (year, lbtype, district)
    con.execute("""
        CREATE TABLE IF NOT EXISTS district_done (
            key TEXT PRIMARY KEY,
            year_val TEXT,
            year_label TEXT,
            lb_val TEXT,
            lb_label TEXT,
            district_index INTEGER,
            district_name TEXT,
            status TEXT DEFAULT 'DONE',
            rows_written INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_district_done_status ON district_done(status)")
    con.commit()
    return con

def make_district_key(year_val, lb_val, district_index):
    return f"{year_val}|{lb_val}|{district_index}"

def district_is_done(con, key):
    row = con.execute("SELECT status FROM district_done WHERE key=?", (key,)).fetchone()
    return (row is not None) and (row[0] == "DONE")

def district_mark_done(con, key, year_val, year_label, lb_val, lb_label, district_index, district_name, rows_written):
    con.execute("""
        INSERT OR REPLACE INTO district_done
        (key, year_val, year_label, lb_val, lb_label, district_index, district_name, status, rows_written, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        key, str(year_val), str(year_label), str(lb_val), str(lb_label),
        int(district_index), str(district_name), "DONE", int(rows_written), now_utc()
    ))
    con.commit()


# ================= CSV HELPERS =================

CSV_HEADER = [
    "year_value",
    "year_label",
    "lbtype_value",
    "lbtype_label",
    "district_index",
    "district_name",
    "localbody_index",
    "localbody_name",
    "no_of_projects",
    "productive",
    "service",
    "infrastructure",
    "total",
]

def ensure_csv_header():
    if os.path.exists(OUT_CSV) and os.path.getsize(OUT_CSV) > 0:
        return
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)

def append_csv_rows(rows):
    if not rows:
        return
    with open(OUT_CSV, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)


# ================= CLIENT =================

class SulekhaClient:
    def __init__(self, delay=DELAY_BETWEEN_REQUESTS):
        self.delay = delay
        self._new_session()
        self.form_data = {}
        self.soup = None

    def _new_session(self):
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": BASE_URL,
            "Origin": "https://plan.lsgkerala.gov.in",
        })

    def _sleep(self):
        time.sleep(self.delay)

    def _parse_form(self, html):
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", {"id": "form1"})
        if form is None:
            raise RuntimeError("Could not find form id='form1' (session may be blocked or changed)")
        data = {}

        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            data[name] = inp.get("value", "")

        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            selected = sel.find("option", selected=True)
            if selected is None:
                first = sel.find("option")
                data[name] = first["value"] if first else ""
            else:
                data[name] = selected["value"]

        self.form_data = data
        self.soup = soup

    def _request_with_retries(self, method, url, **kwargs):
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._sleep()
                resp = self.s.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)

                if 500 <= resp.status_code < 600:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)

                resp.raise_for_status()
                return resp

            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                last_exc = e
                backoff = min(BACKOFF_START * (BACKOFF_BASE ** (attempt - 1)), BACKOFF_MAX)
                backoff = backoff * (0.7 + random.random() * 0.6)

                code = ""
                if isinstance(e, requests.HTTPError) and getattr(e, "response", None) is not None:
                    code = f"(status={e.response.status_code})"

                print(f"    ⚠ Network/server error {code} attempt {attempt}/{MAX_RETRIES}: {e}")
                print(f"    ↻ Backing off for {backoff:.1f}s")
                time.sleep(backoff)

                # Don’t recreate too aggressively (cookies matter), but do it sometimes
                if attempt == 6:
                    print("    🔄 Recreating HTTP session...")
                    self._new_session()

        raise last_exc

    def load_base(self):
        print("▶ Loading base page:", BASE_URL)
        r = self._request_with_retries("GET", BASE_URL)
        self._parse_form(r.text)
        print("✅ Base page loaded.")

    def postback(self, event_target, event_argument="", updates=None):
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        r = self._request_with_retries("POST", BASE_URL, data=data)
        self._parse_form(r.text)
        return self.soup

    @staticmethod
    def extract_postback_from_link(tag):
        if tag is None:
            return None
        attrs = [tag.get("href", ""), tag.get("onclick", "")]
        for attr in attrs:
            m = POSTBACK_RE.search(attr or "")
            if m:
                return m.group("target"), m.group("argument")
        return None

    def get_year_options(self):
        sel = self.soup.find("select", {"id": "drpYear"})
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            txt = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, txt))
        return out

    def get_lbtype_options(self):
        sel = self.soup.find("select", {"id": "drpType"})
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            txt = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, txt))
        return out


# ================= PARSERS =================

def parse_district_rows(soup):
    """
    Reads the first table (district summary): gvState
    Returns list of dicts with district info + postback for >>
    """
    gv = soup.find("table", {"id": "gvState"})
    out = []
    if not gv:
        return out

    rows = gv.find_all("tr", recursive=False)
    # skip header and total row (usually last)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue

        # Expect first cell is serial number, second is district name, last contains >>
        try:
            idx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue

        district_name = tds[1].get_text(strip=True)

        link = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb:
            continue

        out.append({
            "district_index": idx,
            "district_name": district_name,
            "postback": pb
        })

    return out


def parse_localbody_rows_with_amounts(soup):
    """
    Reads the second table (district → localbodies): gvStat
    Columns:
      Serial No | LOCALBODY | NO.OF PROJECTS | PRODUCTIVE | SERVICE | INFRASTRUCTURE | TOTAL | DETAILS
    Returns list of dicts for LB rows (skipping total row)
    """
    gv = soup.find("table", {"id": "gvStat"})
    out = []
    if not gv:
        return out

    rows = gv.find_all("tr", recursive=False)
    # skip header and total row
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        # expected at least 7 columns before DETAILS
        if len(tds) < 7:
            continue

        # serial
        try:
            lb_idx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue

        lb_name = tds[1].get_text(strip=True)

        no_of_projects = tds[2].get_text(strip=True)
        productive = tds[3].get_text(strip=True)
        service = tds[4].get_text(strip=True)
        infrastructure = tds[5].get_text(strip=True)
        total = tds[6].get_text(strip=True)

        out.append({
            "localbody_index": lb_idx,
            "localbody_name": lb_name,
            "no_of_projects": no_of_projects,
            "productive": productive,
            "service": service,
            "infrastructure": infrastructure,
            "total": total
        })

    return out


# ================= CONTEXT RESET =================

def safe_reset_to_year_lbtype(client, year_val, lb_val):
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": year_val})
    client.postback("drpType", "", updates={"drpType": lb_val})


# ================= MAIN SCRAPER =================

def scrape_all_second_tables():
    ensure_csv_header()
    con = db_connect()
    client = SulekhaClient(delay=DELAY_BETWEEN_REQUESTS)

    client.load_base()
    years = client.get_year_options()
    lbtypes = client.get_lbtype_options()

    print("Years:", years)
    print("LB Types:", lbtypes)
    print("📂 Writing to CSV:", os.path.abspath(OUT_CSV))

    for (year_val, year_label) in years:
        print("\n==============================")
        print(f"=== YEAR {year_label} ({year_val}) ===")
        print("==============================")

        try:
            client.postback("drpYear", "", updates={"drpYear": year_val})
        except Exception as e:
            print(f"⚠ Failed to set year {year_label}: {e} -> reset and retry once")
            client.load_base()
            client.postback("drpYear", "", updates={"drpYear": year_val})

        for (lb_val, lb_label) in lbtypes:
            print(f"\n  -- LB Type {lb_label} ({lb_val}) --")

            try:
                client.postback("drpType", "", updates={"drpType": lb_val})
            except Exception as e:
                print(f"⚠ Failed to set LB type {lb_label}: {e} -> reset year+lbtype and retry once")
                safe_reset_to_year_lbtype(client, year_val, lb_val)

            districts = parse_district_rows(client.soup)
            print(f"    Found {len(districts)} districts")

            for d in districts:
                d_idx = d["district_index"]
                d_name = d["district_name"]
                d_key = make_district_key(year_val, lb_val, d_idx)

                if district_is_done(con, d_key):
                    print(f"    🟡 Skipping DONE district {d_idx}: {d_name}")
                    continue

                print(f"    ▶ District {d_idx}: {d_name}")

                # enter district (to see gvStat)
                dt, da = d["postback"]
                try:
                    client.postback(dt, da)
                except Exception as e:
                    print(f"      ⚠ Failed to enter district {d_name}: {e}")
                    print("      ↻ Resetting to year+lbtype and continuing...")
                    try:
                        safe_reset_to_year_lbtype(client, year_val, lb_val)
                    except Exception:
                        pass
                    continue

                lbs = parse_localbody_rows_with_amounts(client.soup)
                print(f"      Found {len(lbs)} localbodies in gvStat")

                # write rows immediately
                out_rows = []
                for lb in lbs:
                    out_rows.append([
                        year_val,
                        year_label,
                        lb_val,
                        lb_label,
                        d_idx,
                        d_name,
                        lb["localbody_index"],
                        lb["localbody_name"],
                        lb["no_of_projects"],
                        lb["productive"],
                        lb["service"],
                        lb["infrastructure"],
                        lb["total"],
                    ])

                append_csv_rows(out_rows)
                district_mark_done(
                    con,
                    d_key,
                    year_val, year_label,
                    lb_val, lb_label,
                    d_idx, d_name,
                    rows_written=len(out_rows)
                )

                print(f"      ✅ Saved district {d_idx}: wrote {len(out_rows)} rows")

                # go back safely to year+lbtype screen for next district
                try:
                    safe_reset_to_year_lbtype(client, year_val, lb_val)
                except Exception:
                    # if reset fails, next loop iteration will re-try anyway
                    pass

    con.close()
    print("\n✅ DONE. Output CSV:", os.path.abspath(OUT_CSV))


if __name__ == "__main__":
    scrape_all_second_tables()
