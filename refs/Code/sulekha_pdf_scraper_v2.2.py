import csv
import os
import re
import time
import random
import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple, Dict, List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ============================================================
# CONFIG
# ============================================================

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

# CHANGE THIS if you want output somewhere else
OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"

# Network + politeness
REQUEST_TIMEOUT = 45
BASE_DELAY_SECONDS = 0.8   # base politeness delay between requests
JITTER_SECONDS = 0.4       # random extra delay
MAX_RETRIES = None            # retries for 502/timeout/etc.
BACKOFF_START = 2.0        # seconds
MAX_BACKOFF = 300          # 5 minutes

# Pause control: if this file exists, scraper pauses
PAUSE_FILE_NAME = "PAUSE.txt"

# Progress DB filename (fast resume)
PROGRESS_DB_NAME = "sulekha_progress.sqlite"

# ============================================================
# REGEX
# ============================================================

POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")
PAGE_ARG_RE = re.compile(r"Page\$(\d+)", re.IGNORECASE)
SELECT_ARG_RE = re.compile(r"Select\$(\d+)", re.IGNORECASE)

# Pager-number bogus row example: "1,2,3"
BOGUS_NUM_LIST_RE = re.compile(r"^\s*\d+(\s*,\s*\d+)+\s*$")

# ============================================================
# SMALL HELPERS
# ============================================================

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def slugify(s: str, max_len: int = 80) -> str:
    s = (s or "").strip().replace(" ", "_")
    s = re.sub(r"[^\w\-]+", "", s, flags=re.UNICODE)
    if not s:
        s = "name"
    return s[:max_len]

def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def pause_if_requested():
    pause_path = os.path.join(OUT_DIR, PAUSE_FILE_NAME)
    if os.path.exists(pause_path):
        print(f"\n⏸ PAUSE requested ({pause_path}). Delete the file to resume.")
    while os.path.exists(pause_path):
        time.sleep(3)

# ============================================================
# PROGRESS DB (FAST RESUME)
# ============================================================

class ProgressDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        ensure_dir(os.path.dirname(db_path))
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self):
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS lb_progress (
                key TEXT PRIMARY KEY,
                year_val TEXT,
                lbtype_val TEXT,
                district_idx INTEGER,
                lb_idx INTEGER,
                csv_path TEXT,
                expected_projects INTEGER,
                scraped_rows INTEGER,
                done INTEGER,
                updated_at TEXT
            );
            """
        )
        self.conn.commit()

    @staticmethod
    def make_key(year_val: str, lbtype_val: str, district_idx: int, lb_idx: int) -> str:
        return f"{year_val}|{lbtype_val}|{district_idx}|{lb_idx}"

    def get_status(self, key: str) -> Optional[Dict]:
        cur = self.conn.execute(
            "SELECT key, expected_projects, scraped_rows, done, csv_path FROM lb_progress WHERE key=?",
            (key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "key": row[0],
            "expected": row[1],
            "scraped": row[2],
            "done": bool(row[3]),
            "csv_path": row[4],
        }

    def upsert(self, key: str, year_val: str, lbtype_val: str, district_idx: int, lb_idx: int,
               csv_path: str, expected: Optional[int], scraped: int, done: bool):
        self.conn.execute(
            """
            INSERT INTO lb_progress(key, year_val, lbtype_val, district_idx, lb_idx, csv_path,
                                   expected_projects, scraped_rows, done, updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET
                expected_projects=excluded.expected_projects,
                scraped_rows=excluded.scraped_rows,
                done=excluded.done,
                csv_path=excluded.csv_path,
                updated_at=excluded.updated_at
            """,
            (
                key,
                year_val,
                lbtype_val,
                district_idx,
                lb_idx,
                csv_path,
                expected if expected is not None else None,
                scraped,
                1 if done else 0,
                now_ts(),
            ),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

# ============================================================
# CLIENT (ASP.NET WebForms)
# ============================================================

class SulekhaClient:
    def __init__(self, delay: float = BASE_DELAY_SECONDS):
        self.s = requests.Session()
        self.delay = delay
        self.form_data: Dict[str, str] = {}
        self.soup: Optional[BeautifulSoup] = None

        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Connection": "keep-alive",
            }
        )

    def _sleep(self):
        time.sleep(self.delay + random.random() * JITTER_SECONDS)

    def _parse_form(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", {"id": "form1"})
        if form is None:
            raise RuntimeError("Could not find form id='form1' (site HTML changed or blocked)")

        data: Dict[str, str] = {}

        # inputs
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            data[name] = inp.get("value", "")

        # selects
        for sel in form.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            selected = sel.find("option", selected=True)
            if selected is None:
                first = sel.find("option")
                data[name] = first["value"] if first and first.get("value") else ""
            else:
                data[name] = selected.get("value", "")

        self.form_data = data
        self.soup = soup

def _request_with_retries(self, method, url, data=None, stream=False):
    attempt = 0

    while True:
        try:
            if method == "GET":
                r = self.session.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                    stream=stream
                )
            else:
                r = self.session.post(
                    url,
                    data=data,
                    timeout=REQUEST_TIMEOUT,
                    stream=stream
                )

            r.raise_for_status()
            return r

        except Exception as e:
            attempt += 1

            # If retries are finite, stop after MAX_RETRIES
            if MAX_RETRIES is not None and attempt >= MAX_RETRIES:
                print(f"❌ Giving up after {attempt} retries: {url}")
                raise

            # Exponential backoff with cap
            wait = min(BACKOFF_START * (2 ** (attempt - 1)), MAX_BACKOFF)

            print(
                f"⚠ Request failed (attempt {attempt}): {e}\n"
                f"   Waiting {wait:.1f}s before retrying..."
            )
            time.sleep(wait)

    def load_base(self):
        print("▶ Loading base page:", BASE_URL)
        r = self._request_with_retries("GET", BASE_URL)
        self._parse_form(r.text)
        print("✅ Base page loaded.")

    def postback(self, event_target: str, event_argument: str = "", updates: Optional[Dict[str, str]] = None):
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        r = self._request_with_retries("POST", BASE_URL, data=data)
        self._parse_form(r.text)
        return self.soup

    @staticmethod
    def extract_postback_from_link(tag) -> Optional[Tuple[str, str]]:
        if tag is None:
            return None
        attrs = [tag.get("href", ""), tag.get("onclick", "")]
        for attr in attrs:
            m = POSTBACK_RE.search(attr or "")
            if m:
                return m.group("target"), m.group("argument")
        return None

    def get_year_options(self) -> List[Tuple[str, str]]:
        sel = self.soup.find("select", {"id": "drpYear"}) if self.soup else None
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            text = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, text))
        return out

    def get_lbtype_options(self) -> List[Tuple[str, str]]:
        sel = self.soup.find("select", {"id": "drpType"}) if self.soup else None
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            text = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, text))
        return out

# ============================================================
# PARSERS
# ============================================================

def parse_district_rows(soup: BeautifulSoup) -> List[Dict]:
    """
    Table: gvState (DPC Approved Details)
    """
    gv = soup.find("table", {"id": "gvState"})
    rows_out: List[Dict] = []
    if not gv:
        return rows_out

    rows = gv.find_all("tr", recursive=False)
    # skip header & total row
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue

        try:
            idx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue

        district_name = tds[1].get_text(strip=True)

        try:
            no_of_lbs = int(tds[2].get_text(strip=True))
        except ValueError:
            no_of_lbs = None

        try:
            no_of_projects = int(tds[3].get_text(strip=True))
        except ValueError:
            no_of_projects = None

        link = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb:
            continue

        rows_out.append(
            {
                "index": idx,
                "district_name": district_name,
                "no_of_lbs": no_of_lbs,
                "no_of_projects": no_of_projects,
                "postback": pb,
            }
        )

    return rows_out


def parse_localbody_rows(soup: BeautifulSoup) -> List[Dict]:
    """
    Table: gvStat (Local bodies)
    """
    gv = soup.find("table", {"id": "gvStat"})
    rows_out: List[Dict] = []
    if not gv:
        return rows_out

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 4:
            continue

        try:
            idx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue

        lb_name = tds[1].get_text(strip=True)

        try:
            no_of_projects = int(tds[2].get_text(strip=True))
        except ValueError:
            no_of_projects = None

        link = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb:
            continue

        rows_out.append(
            {
                "index": idx,
                "lb_name": lb_name,
                "no_of_projects": no_of_projects,
                "postback": pb,
            }
        )

    return rows_out


def parse_projects_and_pager(soup: BeautifulSoup):
    """
    Table: gvProjects (projects list inside a LOCALBODY)
    Returns:
      projects: list of dicts
      pager_info: {current_page, next_postback}
    """
    gv = soup.find("table", {"id": "gvProjects"})
    projects = []
    pager_info = {"current_page": None, "next_postback": None}

    if not gv:
        return projects, pager_info

    rows = gv.find_all("tr", recursive=False)
    if not rows:
        return projects, pager_info

    # Identify pager row (contains nested table)
    pager_tr = None
    for tr in rows:
        if tr.find("table"):
            pager_tr = tr
            break

    # Project rows:
    for tr in rows[1:]:
        if tr is pager_tr:
            continue
        tds = tr.find_all("td", recursive=False)
        # Expected project rows have at least 5 tds (incl "Details" column)
        if len(tds) < 5:
            continue

        project_no = tds[0].get_text(strip=True)
        project_name = tds[1].get_text(strip=True)
        formulation = tds[2].get_text(strip=True)
        expense = tds[3].get_text(strip=True)

        # Strong filter: pager-number rows sneak in as "1,2,3"
        if BOGUS_NUM_LIST_RE.fullmatch(project_name or ""):
            continue
        if BOGUS_NUM_LIST_RE.fullmatch(formulation or ""):
            continue
        if BOGUS_NUM_LIST_RE.fullmatch(expense or ""):
            continue

        # Another filter: real rows usually have a Select$ link/button in details cell
        details_cell = tds[4]
        details_link = details_cell.find("a")
        pb = SulekhaClient.extract_postback_from_link(details_link)
        # If it has no select link at all, it's very likely not a real project row
        if not pb or not SELECT_ARG_RE.search(pb[1] or ""):
            # still allow, but only if project_name looks non-empty and not purely numeric
            if not project_name or project_name.isdigit():
                continue

        projects.append(
            {
                "project_no": project_no,
                "project_name": project_name,
                "formulation": formulation,
                "expense": expense,
            }
        )

    # Pager parsing
    current_page = None
    next_postback = None

    if pager_tr:
        # current page: span digit
        for span in pager_tr.find_all("span"):
            txt = span.get_text(strip=True)
            if txt.isdigit():
                current_page = int(txt)
                break

        candidates = []
        for a in pager_tr.find_all("a"):
            pb = SulekhaClient.extract_postback_from_link(a)
            if not pb:
                continue
            _t, arg = pb
            m = PAGE_ARG_RE.search(arg or "")
            if not m:
                continue
            page_num = int(m.group(1))
            label = a.get_text(strip=True)  # could be "..." or "11"
            candidates.append((page_num, pb, label))

        # Choose next page robustly:
        if candidates:
            # Prefer the smallest page number greater than current_page
            if current_page is not None:
                greater = [c for c in candidates if c[0] > current_page]
                if greater:
                    greater.sort(key=lambda x: x[0])
                    next_postback = greater[0][1]
                else:
                    next_postback = None
            else:
                # If current page unknown, pick the minimum page > 1
                candidates.sort(key=lambda x: x[0])
                next_postback = candidates[0][1]

    pager_info["current_page"] = current_page
    pager_info["next_postback"] = next_postback
    return projects, pager_info


# ============================================================
# CSV WRITER (APPEND AS YOU GO)
# ============================================================

CSV_HEADER = [
    "year_value",
    "year_label",
    "lbtype_value",
    "lbtype_label",
    "district_index",
    "district_name",
    "localbody_index",
    "localbody_name",
    "expected_projects",
    "project_page",
    "project_no",
    "project_name",
    "formulation",
    "expense",
]

def open_csv_for_append(csv_path: str):
    """
    Create file + header if missing; else append.
    UTF-8 with BOM so Excel opens Malayalam correctly.
    """
    ensure_dir(os.path.dirname(csv_path))
    file_exists = os.path.exists(csv_path)

    f = open(csv_path, "a", encoding="utf-8-sig", newline="")
    w = csv.writer(f)

    if not file_exists:
        w.writerow(CSV_HEADER)
        f.flush()

    return f, w

# ============================================================
# LOCALBODY SCRAPE (INCREMENTAL + LOOP-SAFE)
# ============================================================

def scrape_localbody_projects_incremental(
    client: SulekhaClient,
    db: ProgressDB,
    year_val: str,
    year_label: str,
    lb_val: str,
    lb_label: str,
    district: Dict,
    localbody: Dict,
):
    d_idx = int(district["index"])
    lb_idx = int(localbody["index"])
    lb_name = localbody["lb_name"]
    expected = localbody.get("no_of_projects")

    key = db.make_key(year_val, lb_val, d_idx, lb_idx)

    # Output CSV path (one per LOCALBODY per Year+LBType)
    # (district name included to avoid name collisions)
    fname = (
        f"Y{year_label}_T{lb_label}_D{d_idx:02d}_{slugify(district['district_name'])}"
        f"_LB{lb_idx:04d}_{slugify(lb_name)}.csv"
    )
    csv_path = os.path.join(OUT_DIR, fname)

    # If already done in DB, skip immediately (fast resume)
    status = db.get_status(key)
    if status and status["done"]:
        print(f"        ⏭ Skipping (already done): {lb_name}")
        return

    # Enter LOCALBODY projects page
    lb_target, lb_arg = localbody["postback"]
    client.postback(lb_target, lb_arg)

    # Open CSV append
    f, writer = open_csv_for_append(csv_path)

    scraped_rows = 0
    visited_signatures = set()  # stops pagination loops

    try:
        while True:
            projects, pager_info = parse_projects_and_pager(client.soup)

            current_page = pager_info["current_page"]
            page_id = current_page if current_page is not None else -1

            # Build a signature to detect looping pages
            first_sig = ""
            if projects:
                first_sig = f"{projects[0]['project_no']}|{projects[0]['project_name']}"
            signature = f"{page_id}|{len(projects)}|{first_sig}"

            if signature in visited_signatures:
                print(f"          ⚠ Pagination loop detected at page={page_id}. Stopping this LB.")
                break
            visited_signatures.add(signature)

            print(f"          Page {page_id}: {len(projects)} project rows")

            # Write rows immediately
            for p in projects:
                writer.writerow(
                    [
                        year_val,
                        year_label,
                        lb_val,
                        lb_label,
                        d_idx,
                        district["district_name"],
                        lb_idx,
                        lb_name,
                        expected,
                        page_id,
                        p["project_no"],
                        p["project_name"],
                        p["formulation"],
                        p["expense"],
                    ]
                )
                scraped_rows += 1

            f.flush()

            # Update progress every page (so restart is instant)
            db.upsert(
                key=key,
                year_val=year_val,
                lbtype_val=lb_val,
                district_idx=d_idx,
                lb_idx=lb_idx,
                csv_path=csv_path,
                expected=expected,
                scraped=scraped_rows,
                done=False,
            )

            next_pb = pager_info["next_postback"]
            if not next_pb:
                break

            print("          -> Moving to next project page (incl '...').")
            n_target, n_arg = next_pb
            client.postback(n_target, n_arg)

        # Done
        done_flag = True
        if expected is not None:
            print(f"        ✅ Finished LB '{lb_name}': scraped {scraped_rows} rows (expected {expected})")
        else:
            print(f"        ✅ Finished LB '{lb_name}': scraped {scraped_rows} rows")

        db.upsert(
            key=key,
            year_val=year_val,
            lbtype_val=lb_val,
            district_idx=d_idx,
            lb_idx=lb_idx,
            csv_path=csv_path,
            expected=expected,
            scraped=scraped_rows,
            done=done_flag,
        )

    finally:
        f.close()

# ============================================================
# MASTER CRAWLER
# ============================================================

def crawl_all(
    limit_years=None,
    limit_lbtypes=None,
    limit_districts=None,
    limit_lbs_per_district=None,
):
    ensure_dir(OUT_DIR)
    print("🔹 Running:", os.path.abspath(__file__) if "__file__" in globals() else "(interactive)")
    print("🔹 Current working directory:", os.getcwd())
    print("🔹 OUT_DIR (CSV output folder):", os.path.abspath(OUT_DIR))
    print("🔹 Progress DB:", os.path.join(OUT_DIR, PROGRESS_DB_NAME))
    print("🔹 Pause by creating:", os.path.join(OUT_DIR, PAUSE_FILE_NAME))
    print("")

    db = ProgressDB(os.path.join(OUT_DIR, PROGRESS_DB_NAME))
    client = SulekhaClient(delay=BASE_DELAY_SECONDS)
    client.load_base()

    years = client.get_year_options()
    lbtypes = client.get_lbtype_options()

    print("Years:", years)
    print("LB Types:", lbtypes)

    try:
        for i, (year_val, year_label) in enumerate(years, start=1):
            if limit_years and i > limit_years:
                break

            print("\n==============================")
            print(f"=== YEAR {year_label} ({year_val}) ===")
            print("==============================")

            # Year selection
            client.postback("drpYear", "", updates={"drpYear": year_val})

            for j, (lb_val, lb_label) in enumerate(lbtypes, start=1):
                if limit_lbtypes and j > limit_lbtypes:
                    break

                print(f"\n  -- LB Type {lb_label} ({lb_val}) --")
                client.postback("drpType", "", updates={"drpType": lb_val})

                districts = parse_district_rows(client.soup)
                print(f"    Found {len(districts)} districts for this Year+LBType")

                for d_i, district in enumerate(districts, start=1):
                    if limit_districts and d_i > limit_districts:
                        break

                    d_idx = district["index"]
                    d_name = district["district_name"]
                    print(
                        f"\n    ▶ District {d_idx}: {d_name} "
                        f"(LBs: {district.get('no_of_lbs')}, Projects: {district.get('no_of_projects')})"
                    )

                    # Enter district
                    d_target, d_arg = district["postback"]
                    client.postback(d_target, d_arg)

                    lb_rows = parse_localbody_rows(client.soup)
                    print(f"      Found {len(lb_rows)} LOCALBODY rows in this district")

                    for k, lb in enumerate(lb_rows, start=1):
                        if limit_lbs_per_district and k > limit_lbs_per_district:
                            break

                        lb_idx = lb["index"]
                        lb_name = lb["lb_name"]
                        expected = lb.get("no_of_projects")

                        print(f"      → LOCALBODY {lb_idx}: {lb_name} (Projects: {expected})")

                        # Re-open path fresh before each LB scrape (stable navigation)
                        client.postback("drpYear", "", updates={"drpYear": year_val})
                        client.postback("drpType", "", updates={"drpType": lb_val})
                        client.postback(d_target, d_arg)

                        lb_rows_again = parse_localbody_rows(client.soup)
                        lb_again = next((x for x in lb_rows_again if x["index"] == lb_idx), None)
                        if not lb_again:
                            print(f"        ⚠ Could not find LB index {lb_idx} again; skipping.")
                            continue

                        try:
                            scrape_localbody_projects_incremental(
                                client=client,
                                db=db,
                                year_val=year_val,
                                year_label=year_label,
                                lb_val=lb_val,
                                lb_label=lb_label,
                                district=district,
                                localbody=lb_again,
                            )
                        except Exception as e:
                            # Important: we do NOT die; we move on.
                            print(f"        ❌ Error while scraping LB '{lb_name}': {e}")
                            print("        ✅ You can re-run later; resume will be fast via SQLite.\n")
                            # small cooldown in case server is angry
                            time.sleep(5)

    finally:
        db.close()

# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    # For full scrape: set all limits to None
    crawl_all(
        limit_years=None,
        limit_lbtypes=None,
        limit_districts=None,
        limit_lbs_per_district=None,
    )
