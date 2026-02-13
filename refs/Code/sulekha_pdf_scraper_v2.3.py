# -*- coding: utf-8 -*-
"""
Sulekha Plan Monitoring Scraper (tables + optional PDFs) — resumable + pauseable

Target: https://plan.lsgkerala.gov.in/formulation/Public.aspx (ASP.NET WebForms)

What it does
- Selects Year (drpYear) and LB Type (drpType)
- Walks:
    District table (gvState)  -> click DETAILS >>
    LocalBody table (gvStat) -> click DETAILS >>
    Projects table (gvProjects) with pagination INCLUDING the "..." pager
- Writes ONE CSV per LOCALBODY (incremental append as it goes; UTF-8 with BOM for Excel)
- Optionally downloads PDFs into ONE folder per LOCALBODY
- Uses a small SQLite progress DB so you can stop/restart without rescanning thousands of CSVs
- Pause: create PAUSE.txt in OUT_DIR; resume by deleting it

You can run for all years/LB types/districts/local bodies by leaving all limits as None.
"""

from __future__ import annotations

import csv
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


# ------------------------
# USER SETTINGS
# ------------------------

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

# Output root (CSV folders + PDFs + progress DB)
OUT_DIR = Path(__file__).resolve().parent / "sulekha_tables"

# Pause file (create it to pause; delete it to continue)
PAUSE_FILE = OUT_DIR / "PAUSE.txt"

# If True, download PDFs while scraping (slower). If False, only scrape tables.
DOWNLOAD_PDFS = False

# Requests / politeness
REQUEST_TIMEOUT = 60
BASE_DELAY_SECONDS = 1.2   # base politeness delay between requests
JITTER_SECONDS = 0.6       # random extra delay
MAX_RETRIES = None         # None => infinite retries
BACKOFF_START = 2.0        # seconds (exponential-ish backoff)

# Validation: if True, compare scraped projects vs NO.OF PROJECTS from gvStat (when available)
ENABLE_VALIDATION = True

# Windows-safe filenames
MAX_NAME_CHARS = 120


# ------------------------
# INTERNAL CONSTANTS
# ------------------------

POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")
PROGRESS_DB = OUT_DIR / "sulekha_progress.sqlite"

CSV_DIR = OUT_DIR / "csvs"
PDF_DIR = OUT_DIR / "pdfs"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ------------------------
# UTILITIES
# ------------------------

def ensure_dirs():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CSV_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)

def polite_sleep():
    time.sleep(BASE_DELAY_SECONDS + random.random() * JITTER_SECONDS)

def is_paused():
    return PAUSE_FILE.exists()

def wait_if_paused():
    while is_paused():
        print(f"⏸ Paused. Delete {PAUSE_FILE} to resume...")
        time.sleep(3)

def safe_filename(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"[\\/:*?\"<>|]+", "_", s)
    s = re.sub(r"\s+", " ", s)
    if len(s) > MAX_NAME_CHARS:
        s = s[:MAX_NAME_CHARS].rstrip()
    return s or "unnamed"

def bom_utf8() -> str:
    # Excel-friendly UTF-8 with BOM
    return "utf-8-sig"

def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------
# PROGRESS DB
# ------------------------

def init_progress_db():
    conn = sqlite3.connect(PROGRESS_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS localbody_progress (
            key TEXT PRIMARY KEY,
            year_val TEXT,
            year_label TEXT,
            lb_val TEXT,
            lb_label TEXT,
            district TEXT,
            localbody TEXT,
            expected_projects INTEGER,
            scraped_projects INTEGER DEFAULT 0,
            done INTEGER DEFAULT 0,
            updated_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS downloaded_pdfs (
            key TEXT PRIMARY KEY,
            updated_at TEXT
        )
    """)
    conn.commit()
    return conn

def pb_key(year_val: str, lb_val: str, district: str, localbody: str) -> str:
    return f"{year_val}||{lb_val}||{district}||{localbody}"

def mark_progress(
    conn: sqlite3.Connection,
    key: str,
    year_val: str,
    year_label: str,
    lb_val: str,
    lb_label: str,
    district: str,
    localbody: str,
    expected_projects: Optional[int],
    scraped_projects: int,
    done: bool
):
    conn.execute("""
        INSERT INTO localbody_progress
            (key, year_val, year_label, lb_val, lb_label, district, localbody, expected_projects,
             scraped_projects, done, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            expected_projects=excluded.expected_projects,
            scraped_projects=excluded.scraped_projects,
            done=excluded.done,
            updated_at=excluded.updated_at
    """, (
        key, year_val, year_label, lb_val, lb_label, district, localbody,
        expected_projects, scraped_projects, 1 if done else 0, now_ts()
    ))
    conn.commit()

def is_done(conn: sqlite3.Connection, key: str) -> bool:
    row = conn.execute("SELECT done FROM localbody_progress WHERE key=?", (key,)).fetchone()
    return bool(row and row[0] == 1)

def get_scraped_count(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT scraped_projects FROM localbody_progress WHERE key=?", (key,)).fetchone()
    return int(row[0]) if row else 0

def pdf_done(conn: sqlite3.Connection, pdf_key: str) -> bool:
    row = conn.execute("SELECT 1 FROM downloaded_pdfs WHERE key=?", (pdf_key,)).fetchone()
    return row is not None

def mark_pdf_done(conn: sqlite3.Connection, pdf_key: str):
    conn.execute("""
        INSERT OR REPLACE INTO downloaded_pdfs (key, updated_at) VALUES (?, ?)
    """, (pdf_key, now_ts()))
    conn.commit()


# ------------------------
# CLIENT (ASP.NET WebForms)
# ------------------------

@dataclass
class Postback:
    target: str
    argument: str

class SulekhaClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": USER_AGENT})
        self.form_data: Dict[str, str] = {}
        self.soup: Optional[BeautifulSoup] = None

    def load_base(self):
        print(f"\n▶ Loading base page: {BASE_URL}")
        resp = self._request_with_retries("GET", BASE_URL)
        self._parse_form(resp.text)

    def _request_with_retries(self, method: str, url: str, *, data: Optional[dict]=None) -> requests.Response:
        attempt = 0
        backoff = BACKOFF_START
        while True:
            wait_if_paused()
            attempt += 1
            try:
                polite_sleep()
                resp = self.s.request(
                    method, url, data=data,
                    timeout=REQUEST_TIMEOUT,
                    allow_redirects=True
                )
                if resp.status_code in (502, 503, 504):
                    raise requests.HTTPError(f"{resp.status_code} Server Error", response=resp)
                resp.raise_for_status()
                return resp
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                # MAX_RETRIES=None => infinite
                if MAX_RETRIES is not None and attempt >= int(MAX_RETRIES):
                    raise
                r = getattr(e, "response", None)
                code = f" (HTTP {r.status_code})" if r is not None else ""
                print(f"⚠️  Request failed{code} [attempt {attempt}]. Retrying in {backoff:.1f}s ...")
                time.sleep(backoff)
                backoff = min(backoff * 1.8, 120)

    def _parse_form(self, html: str):
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", {"id": "form1"})
        if not form:
            raise RuntimeError("Could not find form id='form1'")

        data: Dict[str, str] = {}

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
                data[name] = first["value"] if first and first.has_attr("value") else ""
            else:
                data[name] = selected.get("value", "")

        self.form_data = data
        self.soup = soup

    def postback(self, event_target: str, event_argument: str = "", updates: Optional[Dict[str, str]] = None) -> requests.Response:
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        resp = self._request_with_retries("POST", BASE_URL, data=data)

        # If server responds with a PDF directly, keep it; caller can detect content-type
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/pdf" in ctype:
            return resp

        self._parse_form(resp.text)
        return resp

    # Dropdowns
    def get_year_options(self) -> List[Tuple[str, str]]:
        sel = self.soup.find("select", {"id": "drpYear"}) if self.soup else None
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            txt = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, txt))
        return out

    def get_lbtype_options(self) -> List[Tuple[str, str]]:
        sel = self.soup.find("select", {"id": "drpType"}) if self.soup else None
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            txt = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, txt))
        return out

    @staticmethod
    def extract_postback_from_link(tag) -> Optional[Postback]:
        if tag is None:
            return None
        for attr in (tag.get("href", ""), tag.get("onclick", "")):
            m = POSTBACK_RE.search(attr or "")
            if m:
                return Postback(m.group("target"), m.group("argument"))
        return None


# ------------------------
# PARSERS
# ------------------------

def table_by_id(soup: BeautifulSoup, table_id: str):
    return soup.find("table", {"id": table_id})

def parse_gvState_districts(soup: BeautifulSoup) -> List[dict]:
    tbl = table_by_id(soup, "gvState")
    out = []
    if not tbl:
        return out
    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        link = tr.find("a", href=True)
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb or pb.target != "gvState":
            continue
        district = tds[1].get_text(strip=True)
        lb_count = tds[2].get_text(strip=True) if len(tds) > 2 else ""
        proj_count = tds[3].get_text(strip=True) if len(tds) > 3 else ""
        out.append({
            "district": district,
            "lb_count": int(lb_count) if lb_count.isdigit() else None,
            "project_count": int(proj_count) if proj_count.isdigit() else None,
            "pb": pb
        })
    return out

def parse_gvStat_localbodies(soup: BeautifulSoup) -> List[dict]:
    tbl = table_by_id(soup, "gvStat")
    out = []
    if not tbl:
        return out
    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        link = tr.find("a", href=True)
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb or pb.target != "gvStat":
            continue
        localbody = tds[1].get_text(strip=True)
        expected = tds[2].get_text(strip=True) if len(tds) > 2 else ""
        expected_n = int(expected) if expected.isdigit() else None
        out.append({
            "localbody": localbody,
            "expected_projects": expected_n,
            "pb": pb
        })
    return out

def parse_projects_and_pager(soup: BeautifulSoup) -> Tuple[List[dict], dict]:
    tbl = table_by_id(soup, "gvProjects")
    projects: List[dict] = []
    pager_info = {"current_page": None, "page_links": {}, "ellipsis": None}

    if not tbl:
        return projects, pager_info

    # Project rows (skip pager rows by requiring first cell to be digit)
    for tr in tbl.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        first = tds[0].get_text(strip=True)
        if not first.isdigit():
            continue
        if len(tds) < 5:
            continue
        row_no = int(first)
        project_name = tds[1].get_text(strip=True)
        formulation = tds[2].get_text(strip=True)
        expense = tds[3].get_text(strip=True)
        link = tds[4].find("a", href=True)
        details_pb = SulekhaClient.extract_postback_from_link(link)
        projects.append({
            "row_no": row_no,
            "project_name": project_name,
            "formulation": formulation,
            "expense": expense,
            "details_pb": details_pb,
        })

    # Pager links: __doPostBack('gvProjects','Page$N') including "..."
    for a in tbl.find_all("a", href=True):
        pb = SulekhaClient.extract_postback_from_link(a)
        if not pb or pb.target != "gvProjects":
            continue
        arg = pb.argument or ""
        if arg.startswith("Page$"):
            try:
                n = int(arg.split("$", 1)[1])
                pager_info["page_links"][n] = pb
            except ValueError:
                pass
        if a.get_text(strip=True) == "...":
            pager_info["ellipsis"] = pb

    # Current page: often <span>12</span> in pager row
    for span in tbl.find_all("span"):
        txt = span.get_text(strip=True)
        if txt.isdigit():
            pager_info["current_page"] = int(txt)
            break

    return projects, pager_info


# ------------------------
# CSV + PDF OUTPUT
# ------------------------

CSV_FIELDS = [
    "year_val", "year_label",
    "lb_val", "lb_label",
    "district", "localbody",
    "page_no", "row_no",
    "project_name",
    "formulation",
    "expense",
    "pdf_path",
]

def localbody_paths(year_val: str, lb_val: str, district: str, localbody: str) -> Tuple[Path, Path]:
    d = safe_filename(district)
    lb = safe_filename(localbody)
    y = safe_filename(year_val)
    t = safe_filename(lb_val)
    csv_folder = CSV_DIR / f"year_{y}" / f"lbtype_{t}" / d
    pdf_folder = PDF_DIR / f"year_{y}" / f"lbtype_{t}" / d / lb
    csv_folder.mkdir(parents=True, exist_ok=True)
    pdf_folder.mkdir(parents=True, exist_ok=True)
    csv_path = csv_folder / f"{lb}.csv"
    return csv_path, pdf_folder

def append_rows_to_localbody_csv(csv_path: Path, rows: List[dict]):
    new_file = not csv_path.exists()
    with csv_path.open("a", newline="", encoding=bom_utf8()) as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
        for r in rows:
            w.writerow(r)

def download_pdf_for_project(
    client: SulekhaClient,
    conn: sqlite3.Connection,
    year_val: str, lb_val: str, district: str, localbody: str,
    details_pb: Optional[Postback],
    pdf_folder: Path
) -> Optional[str]:
    if not DOWNLOAD_PDFS or not details_pb:
        return ""

    pdf_key = f"{pb_key(year_val, lb_val, district, localbody)}||{details_pb.target}||{details_pb.argument}"
    if pdf_done(conn, pdf_key):
        return ""

    resp = client.postback(details_pb.target, details_pb.argument)

    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ctype:
        fname = safe_filename(details_pb.argument.replace("$", "_")) + ".pdf"
        outpath = pdf_folder / fname
        outpath.write_bytes(resp.content)
        mark_pdf_done(conn, pdf_key)
        return str(outpath)

    soup = client.soup
    if soup:
        a = soup.find("a", href=re.compile(r"\.pdf", re.I))
        if a and a.get("href"):
            full = urljoin(BASE_URL, a["href"])
            pdf_resp = client._request_with_retries("GET", full)
            fname = safe_filename(Path(a["href"]).name.split("?")[0]) or (safe_filename(details_pb.argument) + ".pdf")
            outpath = pdf_folder / fname
            outpath.write_bytes(pdf_resp.content)
            mark_pdf_done(conn, pdf_key)
            return str(outpath)

    mark_pdf_done(conn, pdf_key)
    return ""


# ------------------------
# MAIN CRAWL
# ------------------------

def crawl_all(
    *,
    limit_years: Optional[int] = None,
    limit_lbtypes: Optional[int] = None,
    limit_districts: Optional[int] = None,
    limit_lbs_per_district: Optional[int] = None,
):
    ensure_dirs()
    conn = init_progress_db()

    print(f"🔹 Running: {Path(__file__).resolve()}")
    print(f"🔹 Current working directory: {Path.cwd()}")
    print(f"🔹 OUT_DIR (CSV output folder): {OUT_DIR}")
    print(f"🔹 Progress DB: {PROGRESS_DB}")
    print(f"🔹 Pause by creating: {PAUSE_FILE}")

    client = SulekhaClient()
    client.load_base()

    years = client.get_year_options()
    lbtypes = client.get_lbtype_options()

    print(f"\nYears ({len(years)}): {years[:3]} ...")
    print(f"LB Types ({len(lbtypes)}): {lbtypes}")

    for yi, (year_val, year_label) in enumerate(years, start=1):
        if limit_years is not None and yi > limit_years:
            break

        print(f"\n===============================")
        print(f"=== Year {year_label} ({year_val}) ===")
        print(f"===============================")

        client.postback("drpYear", "", updates={"drpYear": year_val})

        for ti, (lb_val, lb_label) in enumerate(lbtypes, start=1):
            if limit_lbtypes is not None and ti > limit_lbtypes:
                break

            print(f"\n  -- LB Type {lb_label} ({lb_val}) --")
            client.postback("drpType", "", updates={"drpType": lb_val})

            districts = parse_gvState_districts(client.soup)
            print(f"    Found {len(districts)} districts")

            for di, d in enumerate(districts, start=1):
                if limit_districts is not None and di > limit_districts:
                    break

                district = d["district"]
                print(f"    District: {district}")

                client.postback(d["pb"].target, d["pb"].argument)

                localbodies = parse_gvStat_localbodies(client.soup)
                print(f"      Found {len(localbodies)} LOCALBODY rows")

                for li, lb in enumerate(localbodies, start=1):
                    if limit_lbs_per_district is not None and li > limit_lbs_per_district:
                        break

                    localbody = lb["localbody"]
                    expected = lb["expected_projects"]
                    key = pb_key(year_val, lb_val, district, localbody)

                    if is_done(conn, key):
                        continue

                    print(f"      LOCALBODY: {localbody} (expected projects: {expected})")

                    client.postback(lb["pb"].target, lb["pb"].argument)

                    csv_path, pdf_folder = localbody_paths(year_val, lb_val, district, localbody)

                    scraped_total = get_scraped_count(conn, key)
                    written_this_run = 0

                    visited_pages = set()
                    page_counter = 0

                    while True:
                        projects, pager_info = parse_projects_and_pager(client.soup)
                        current_page = pager_info.get("current_page")
                        page_counter += 1
                        page_id = current_page if current_page is not None else page_counter

                        if page_id in visited_pages:
                            print(f"          ⚠️  Page {page_id} already visited — stopping to avoid pagination loop.")
                            break
                        visited_pages.add(page_id)

                        print(f"          Page {page_id}: {len(projects)} project rows")

                        rows_to_write = []
                        for p in projects:
                            pdf_path = ""
                            if DOWNLOAD_PDFS:
                                pdf_path = download_pdf_for_project(
                                    client, conn,
                                    year_val, lb_val, district, localbody,
                                    p.get("details_pb"),
                                    pdf_folder
                                ) or ""

                            rows_to_write.append({
                                "year_val": year_val,
                                "year_label": year_label,
                                "lb_val": lb_val,
                                "lb_label": lb_label,
                                "district": district,
                                "localbody": localbody,
                                "page_no": page_id,
                                "row_no": p["row_no"],
                                "project_name": p["project_name"],
                                "formulation": p["formulation"],
                                "expense": p["expense"],
                                "pdf_path": pdf_path,
                            })

                        if rows_to_write:
                            append_rows_to_localbody_csv(csv_path, rows_to_write)
                            written_this_run += len(rows_to_write)
                            scraped_total += len(rows_to_write)
                            mark_progress(
                                conn, key,
                                year_val, year_label,
                                lb_val, lb_label,
                                district, localbody,
                                expected, scraped_total, done=False
                            )

                        next_pb: Optional[Postback] = None
                        if current_page is None:
                            next_pb = pager_info["page_links"].get(2)
                        else:
                            next_pb = pager_info["page_links"].get(current_page + 1)
                            if not next_pb and pager_info.get("ellipsis"):
                                next_pb = pager_info["ellipsis"]

                        if not next_pb:
                            break

                        print("          -> Moving to next project page...")
                        client.postback(next_pb.target, next_pb.argument)

                    done = True
                    if ENABLE_VALIDATION and expected is not None:
                        if scraped_total != expected:
                            done = False
                            print(f"          ⚠️  Validation mismatch: scraped {scraped_total} vs expected {expected} (will revisit later).")

                    mark_progress(
                        conn, key,
                        year_val, year_label,
                        lb_val, lb_label,
                        district, localbody,
                        expected, scraped_total, done=done
                    )
                    print(f"      ✅ Wrote {written_this_run} rows to {csv_path.name} (total scraped: {scraped_total}).")

                    # Back to district view (re-select year + type + district)
                    client.postback("drpYear", "", updates={"drpYear": year_val})
                    client.postback("drpType", "", updates={"drpType": lb_val})
                    districts_again = parse_gvState_districts(client.soup)
                    drow = next((x for x in districts_again if x["district"] == district), None)
                    if drow:
                        client.postback(drow["pb"].target, drow["pb"].argument)

    print("\n🎉 Done (or hit limits). You can rerun anytime; completed LOCALBODYs will be skipped.")


if __name__ == "__main__":
    # Set all to None for full scrape:
    crawl_all(
        limit_years=None,
        limit_lbtypes=None,
        limit_districts=None,
        limit_lbs_per_district=None,
    )
