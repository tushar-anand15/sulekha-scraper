import csv
import os
import re
import time
import random
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# ===================== CONFIG =====================

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"
ORIGIN = "https://plan.lsgkerala.gov.in"

# Folder that already contains all the per-LB CSVs you scraped
CSV_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"

# Output folder for PDFs (one folder per LOCALBODY)
PDF_ROOT = os.path.join(CSV_DIR, "sulekha_pdfs")

# SQLite DB (progress)
DB_PATH = os.path.join(CSV_DIR, "sulekha_progress.sqlite")

os.makedirs(CSV_DIR, exist_ok=True)
os.makedirs(PDF_ROOT, exist_ok=True)

REQUEST_TIMEOUT = 60
DELAY_BETWEEN_REQUESTS = 0.8  # you can increase if server gets angry

MAX_RETRIES = 6
BACKOFF_BASE = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 90.0

COMMIT_EVERY = 50

# If True, only attempts PDFs for LBs marked DONE in progress DB
ONLY_DONE_LBS = True

# If True, will overwrite existing PDF files (normally False)
OVERWRITE_PDFS = False

# ==================================================

POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")
ILLEGAL_WIN_CHARS = re.compile(r'[<>:"/\\|?*]')


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def slugify(s: str, max_len: int = 120) -> str:
    s = (s or "").strip()
    s = ILLEGAL_WIN_CHARS.sub("_", s)
    s = re.sub(r"\s+", "_", s)
    if not s:
        s = "name"
    return s[:max_len]


# ===================== SQLITE =====================

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-20000;")  # ~20MB

    # progress table likely already exists from your table scraper; ensure at least present
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
    con.execute("CREATE INDEX IF NOT EXISTS idx_progress_status ON progress(status)")

    # PDF download tracking
    con.execute("""
        CREATE TABLE IF NOT EXISTS pdf_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lb_key TEXT NOT NULL,
            project_no TEXT,
            pdf_url TEXT NOT NULL,
            pdf_path TEXT NOT NULL,
            status TEXT DEFAULT 'PENDING',  -- PENDING/DONE/FAILED
            http_status INTEGER,
            err TEXT,
            updated_at TEXT
        )
    """)
    con.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_pdf_url ON pdf_downloads(pdf_url)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pdf_lb ON pdf_downloads(lb_key)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pdf_status ON pdf_downloads(status)")
    return con


def db_get_lb(con, key):
    cur = con.execute(
        "SELECT csv_path, expected, scraped, status FROM progress WHERE key=?",
        (key,),
    )
    r = cur.fetchone()
    if not r:
        return None
    return {"csv_path": r[0], "expected": r[1], "scraped": r[2], "status": r[3]}


def db_pdf_seen_done(con, pdf_url: str) -> bool:
    cur = con.execute(
        "SELECT status FROM pdf_downloads WHERE pdf_url=?",
        (pdf_url,),
    )
    r = cur.fetchone()
    return bool(r and r[0] == "DONE")


def db_pdf_upsert(con, lb_key, project_no, pdf_url, pdf_path, status, http_status=None, err=None):
    con.execute("""
        INSERT INTO pdf_downloads(lb_key, project_no, pdf_url, pdf_path, status, http_status, err, updated_at)
        VALUES (?,?,?,?,?,?,?,?)
        ON CONFLICT(pdf_url) DO UPDATE SET
            lb_key=excluded.lb_key,
            project_no=COALESCE(excluded.project_no, pdf_downloads.project_no),
            pdf_path=excluded.pdf_path,
            status=excluded.status,
            http_status=excluded.http_status,
            err=excluded.err,
            updated_at=excluded.updated_at
    """, (lb_key, project_no, pdf_url, pdf_path, status, http_status, err, now_utc()))


# ===================== HTTP CLIENT =====================

class SulekhaClient:
    def __init__(self, delay=DELAY_BETWEEN_REQUESTS):
        self.delay = delay
        self._new_session()
        self.form_data = {}
        self.soup = None

    def _new_session(self):
        self.s = requests.Session()
        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": BASE_URL,
                "Origin": ORIGIN,
            }
        )

    def _parse_form(self, html):
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", {"id": "form1"})
        if form is None:
            raise RuntimeError("Could not find form id='form1'")
        data = {}

        for inp in form.find_all("input"):
            name = inp.get("name")
            if name:
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
                resp = self.s.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)

                if 500 <= resp.status_code < 600:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)

                resp.raise_for_status()

                # throttle after success
                time.sleep(self.delay)
                return resp

            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                last_exc = e
                backoff = min(BACKOFF_START * (BACKOFF_BASE ** (attempt - 1)), BACKOFF_MAX)
                backoff *= (0.7 + random.random() * 0.6)

                code = ""
                if isinstance(e, requests.HTTPError) and getattr(e, "response", None) is not None:
                    code = f"(status={e.response.status_code})"

                print(f"    ⚠ Network/server error {code} attempt {attempt}/{MAX_RETRIES}: {e}")
                print(f"    ↻ Backing off for {backoff:.1f}s")
                time.sleep(backoff)

                if attempt in {3, 5}:
                    print("    🔄 Recreating HTTP session...")
                    self._new_session()

        raise last_exc

    def load_base(self):
        r = self._request_with_retries("GET", BASE_URL)
        self._parse_form(r.text)

    def postback(self, event_target, event_argument="", updates=None):
        data = dict(self.form_data)
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

    def download_file(self, url, out_path):
        # stream download to file
        r = self._request_with_retries("GET", url, stream=True)
        with open(out_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 128):
                if chunk:
                    f.write(chunk)
        return r.status_code


# ===================== PARSERS =====================

def parse_csv_filename_for_metadata(csv_path: str):
    """
    Your CSV names look like:
      y{year}__lb{lbtype}__{district_index}_{district_name}__{lb_index}_{lb_name}.csv
    We’ll recover a stable lb_key from the progress DB, not from filename.
    But folder naming uses district/lb names for readability.
    """
    base = os.path.basename(csv_path)
    # best-effort split:
    parts = base.split("__")
    # fallback if unexpected
    return {"filename": base, "parts": parts}


def find_project_details_postback_in_projects_page(soup, project_no: str):
    """
    Given the gvProjects page, try to find the details link for a specific project row.
    Commonly the last column has an <a> with __doPostBack.
    We'll match by first column = project_no.
    """
    gv = soup.find("table", {"id": "gvProjects"})
    if not gv:
        return None

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue
        no = tds[0].get_text(strip=True)
        if no != str(project_no).strip():
            continue

        # details is typically last <a>
        a = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(a)
        return pb

    return None


def extract_pdf_links_from_details_page(soup):
    """
    ✅ This is the one place you may need to tweak.
    Strategy:
    - find all <a href="...pdf"> links on the details page
    - also consider links that contain 'Download' text and end with .pdf
    """
    pdf_urls = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        if ".pdf" in href.lower():
            pdf_urls.add(href)
    return sorted(pdf_urls)


def parse_pagination(soup):
    """
    Returns:
      current_page (int or None)
      pages: dict page_num->postback
      ellipsis: list of postbacks (text == "...")
    """
    gv = soup.find("table", {"id": "gvProjects"})
    if not gv:
        return None, {}, []

    rows = gv.find_all("tr", recursive=False)
    pager_tr = None
    for tr in rows:
        if tr.find("table"):
            pager_tr = tr
            break

    current = None
    pages = {}
    ellipsis = []

    if pager_tr:
        for span in pager_tr.find_all("span"):
            t = span.get_text(strip=True)
            if t.isdigit():
                current = int(t)
                break
        for a in pager_tr.find_all("a"):
            txt = a.get_text(strip=True)
            pb = SulekhaClient.extract_postback_from_link(a)
            if not pb:
                continue
            if txt == "...":
                ellipsis.append(pb)
            elif txt.isdigit():
                pages[int(txt)] = pb

    return current, pages, ellipsis


# ===================== NAV / CONTEXT =====================

def safe_reset_to_context(client, year_val, lb_val, district_postback):
    """
    base -> year -> lbtype -> district
    """
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": year_val})
    client.postback("drpType", "", updates={"drpType": lb_val})
    dt, da = district_postback
    client.postback(dt, da)


def go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback):
    """
    base -> year -> lbtype -> district -> lb projects
    """
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": year_val})
    client.postback("drpType", "", updates={"drpType": lb_val})
    dt, da = district_postback
    client.postback(dt, da)
    lt, la = lb_postback
    client.postback(lt, la)


# ===================== MAIN PDF LOGIC =====================

def iter_lbs_from_progress(con):
    """
    Yields LB items from progress DB:
      key, csv_path
    Optionally filter to DONE only.
    """
    if ONLY_DONE_LBS:
        cur = con.execute(
            "SELECT key, csv_path, expected, scraped, status FROM progress WHERE status='DONE'"
        )
    else:
        cur = con.execute(
            "SELECT key, csv_path, expected, scraped, status FROM progress"
        )

    for key, csv_path, expected, scraped, status in cur.fetchall():
        yield key, csv_path, expected, scraped, status


def read_csv_minimal(csv_path):
    """
    Reads the LB CSV and returns:
      - metadata row0 (year_val, year_label, lb_val, lb_label, district_index, district_name, lb_index, lb_name)
      - set of project_no strings
    """
    project_nos = []
    meta = None

    with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        for row in r:
            if meta is None:
                meta = {
                    "year_value": row["year_value"],
                    "year_label": row["year_label"],
                    "lbtype_value": row["lbtype_value"],
                    "lbtype_label": row["lbtype_label"],
                    "district_index": row["district_index"],
                    "district_name": row["district_name"],
                    "localbody_index": row["localbody_index"],
                    "localbody_name": row["localbody_name"],
                }
            project_nos.append(row["project_no"])

    return meta, project_nos


def build_lb_folder(meta):
    district = slugify(f"{meta['district_index']}_{meta['district_name']}")
    lb = slugify(f"{meta['localbody_index']}_{meta['localbody_name']}")
    path = os.path.join(PDF_ROOT, district, lb)
    os.makedirs(path, exist_ok=True)
    return path


def download_pdfs_for_lb(con, client, lb_key, meta, project_nos,
                         district_postback, lb_postback):
    """
    For one LB:
      - navigate to LB projects
      - for each project_no:
          open details
          extract pdf links
          download
    """
    lb_folder = build_lb_folder(meta)

    year_val = meta["year_value"]
    lb_val = meta["lbtype_value"]

    print(f"\n▶ PDFs for LB: {meta['localbody_name']}  ({meta['district_name']})")
    print(f"  Folder: {lb_folder}")
    print(f"  Projects in CSV: {len(project_nos)}")

    # Enter LB projects page
    go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback)

    visited_pages = set()
    page_fallback = 0

    # Build mapping project_no -> (page navigation PB chain)
    # We’ll do a simple approach:
    # - crawl pages sequentially (with ellipsis support)
    # - on each page, detect which project_nos are present
    # - process only those, then move next
    remaining = set(str(x).strip() for x in project_nos)

    while remaining:
        page_fallback += 1
        current_page, pages, ellipsis = parse_pagination(client.soup)
        page_id = current_page if current_page is not None else page_fallback

        if page_id in visited_pages:
            print(f"  ⚠ Pagination loop at page {page_id}. Stopping LB to avoid infinite loop.")
            break
        visited_pages.add(page_id)

        # gather project_nos present on this page
        gv = client.soup.find("table", {"id": "gvProjects"})
        present = []
        if gv:
            rows = gv.find_all("tr", recursive=False)
            for tr in rows[1:]:
                tds = tr.find_all("td", recursive=False)
                if len(tds) < 5:
                    continue
                pno = tds[0].get_text(strip=True)
                if pno in remaining:
                    present.append(pno)

        print(f"  Page {page_id}: found {len(present)} target projects")

        # process each present project
        for pno in present:
            # find details postback
            pb = find_project_details_postback_in_projects_page(client.soup, pno)
            if not pb:
                # couldn’t find; skip but keep remaining
                continue

            # open details
            dt, da = pb
            try:
                client.postback(dt, da)
            except Exception as e:
                print(f"    ⚠ Failed opening details for project {pno}: {e}")
                # try to restore LB projects context and continue
                try:
                    go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback)
                except Exception:
                    pass
                continue

            # extract pdf links on details page
            pdf_links = extract_pdf_links_from_details_page(client.soup)

            if not pdf_links:
                # no pdf links found, go back to projects list
                # easiest is to re-enter LB projects
                go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback)
                remaining.discard(pno)
                continue

            for href in pdf_links:
                full_url = urljoin(BASE_URL, href)

                if not OVERWRITE_PDFS and db_pdf_seen_done(con, full_url):
                    continue

                # filename
                parsed = urlparse(full_url)
                fname = os.path.basename(parsed.path) or f"project_{pno}.pdf"
                fname = slugify(fname, 180)
                out_path = os.path.join(lb_folder, fname)

                if not OVERWRITE_PDFS and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    db_pdf_upsert(con, lb_key, pno, full_url, out_path, "DONE", http_status=200, err=None)
                    continue

                print(f"    ↓ PDF for project {pno}: {fname}")
                try:
                    status_code = client.download_file(full_url, out_path)
                    db_pdf_upsert(con, lb_key, pno, full_url, out_path, "DONE", http_status=status_code, err=None)
                except Exception as e:
                    db_pdf_upsert(con, lb_key, pno, full_url, out_path, "FAILED", http_status=None, err=str(e))
                    print(f"      ❌ Failed: {e}")

            # return to projects list
            go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback)

            remaining.discard(pno)

        con.commit()

        # Move to next project page (with ellipsis support)
        # If current page known, prefer current+1 if visible; else click ellipsis
        want = (current_page + 1) if current_page is not None else (page_id + 1)
        next_pb = pages.get(want)

        if not next_pb and ellipsis:
            next_pb = ellipsis[-1]

        if not next_pb:
            # no more pages visible; stop
            break

        nt, na = next_pb
        try:
            client.postback(nt, na)
        except Exception:
            # reset to LB projects and try again once
            go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback)
            try:
                client.postback(nt, na)
            except Exception:
                break

    # done
    print(f"✅ Finished LB PDFs: {meta['localbody_name']}")
    return True


# ===================== IMPORTANT: HOW WE GET POSTBACKS =====================
# Your CSVs alone do NOT contain the ASP.NET postback targets/arguments for
# district/LB navigation. So the PDF scraper MUST navigate the site fresh
# and re-discover those postbacks.

def build_navigation_index_for_year_lbtype(client, year_val, lb_val):
    """
    Returns a dict:
      districts[district_index] = {
          name, postback, lbs: {lb_index: {name, postback}}
      }
    """
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": str(year_val)})
    client.postback("drpType", "", updates={"drpType": str(lb_val)})

    gv = client.soup.find("table", {"id": "gvState"})
    if not gv:
        return {}

    districts = {}

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 9:
            continue
        try:
            didx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue
        dname = tds[1].get_text(strip=True)
        link = tds[-1].find("a")
        dpb = SulekhaClient.extract_postback_from_link(link)
        if not dpb:
            continue

        districts[didx] = {
            "district_index": didx,
            "district_name": dname,
            "postback": dpb,
            "lbs": {}
        }

    # for each district, load LB list and cache postbacks
    for didx, d in districts.items():
        dt, da = d["postback"]
        try:
            client.postback(dt, da)
        except Exception:
            # if fail, rebuild context and retry once
            client.load_base()
            client.postback("drpYear", "", updates={"drpYear": str(year_val)})
            client.postback("drpType", "", updates={"drpType": str(lb_val)})
            client.postback(dt, da)

        gv_lb = client.soup.find("table", {"id": "gvStat"})
        if not gv_lb:
            continue

        rows_lb = gv_lb.find_all("tr", recursive=False)
        for tr2 in rows_lb[1:-1]:
            tds2 = tr2.find_all("td", recursive=False)
            if len(tds2) < 7:
                continue
            try:
                lbidx = int(tds2[0].get_text(strip=True))
            except ValueError:
                continue
            lbname = tds2[1].get_text(strip=True)
            alink = tds2[-1].find("a")
            lpb = SulekhaClient.extract_postback_from_link(alink)
            if not lpb:
                continue
            d["lbs"][lbidx] = {"lb_index": lbidx, "lb_name": lbname, "postback": lpb}

        # go back to district list by restoring year+type (fast enough)
        client.load_base()
        client.postback("drpYear", "", updates={"drpYear": str(year_val)})
        client.postback("drpType", "", updates={"drpType": str(lb_val)})

    return districts


# ===================== MASTER RUNNER =====================

def run_pdf_scraper():
    con = db_connect()
    client = SulekhaClient(delay=DELAY_BETWEEN_REQUESTS)

    # We'll group LBs by (year_val, lb_val) so we can build nav index once per group
    lb_items = []
    for key, csv_path, expected, scraped, status in iter_lbs_from_progress(con):
        if not csv_path or not os.path.exists(csv_path):
            continue
        lb_items.append((key, csv_path))

    if not lb_items:
        print("❌ No LBs found in progress DB. Did you point DB_PATH/CSV_DIR correctly?")
        return

    print(f"✅ Found {len(lb_items)} LBs to consider for PDF downloads.")
    print(f"📂 CSV folder: {CSV_DIR}")
    print(f"📂 PDF root:   {PDF_ROOT}")
    print(f"🗄 DB:         {DB_PATH}")

    # Cache: (year_val, lb_val) -> nav index
    nav_cache = {}

    ops = 0

    for lb_key, csv_path in lb_items:
        try:
            meta, project_nos = read_csv_minimal(csv_path)
        except Exception as e:
            print(f"⚠ Could not read CSV {csv_path}: {e}")
            continue

        year_val = meta["year_value"]
        lb_val = meta["lbtype_value"]
        didx = int(meta["district_index"])
        lbidx = int(meta["localbody_index"])

        group = (str(year_val), str(lb_val))
        if group not in nav_cache:
            print(f"\n=== Building navigation index for Year={year_val}, LBType={lb_val} ===")
            try:
                nav_cache[group] = build_navigation_index_for_year_lbtype(client, year_val, lb_val)
            except Exception as e:
                print(f"❌ Failed building navigation index for {group}: {e}")
                # try again with fresh session once
                try:
                    client._new_session()
                    nav_cache[group] = build_navigation_index_for_year_lbtype(client, year_val, lb_val)
                except Exception as e2:
                    print(f"❌ Still failed: {e2}")
                    continue

        nav = nav_cache[group]
        if didx not in nav:
            print(f"⚠ District index {didx} not found in nav for (year={year_val}, lb={lb_val}). Skipping LB.")
            continue
        if lbidx not in nav[didx]["lbs"]:
            print(f"⚠ LB index {lbidx} not found under district {didx}. Skipping LB.")
            continue

        district_postback = nav[didx]["postback"]
        lb_postback = nav[didx]["lbs"][lbidx]["postback"]

        try:
            download_pdfs_for_lb(con, client, lb_key, meta, project_nos, district_postback, lb_postback)
            ops += 1
            if ops % COMMIT_EVERY == 0:
                con.commit()
                print(f"✅ Committed batch {ops}")
        except Exception as e:
            print(f"❌ LB failed (will resume next run): {meta['localbody_name']} :: {e}")
            # continue to next LB

    con.commit()
    con.close()
    print("\n✅ PDF scraping run completed.")


if __name__ == "__main__":
    run_pdf_scraper()
