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

CSV_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
PDF_ROOT = os.path.join(CSV_DIR, "sulekha_pdfs")
DB_PATH = os.path.join(CSV_DIR, "sulekha_progress.sqlite")

os.makedirs(CSV_DIR, exist_ok=True)
os.makedirs(PDF_ROOT, exist_ok=True)

REQUEST_TIMEOUT = 60
DELAY_BETWEEN_REQUESTS = 0.8  # increase if server rate-limits

MAX_RETRIES = 6
BACKOFF_BASE = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 90.0

COMMIT_EVERY = 50

ONLY_DONE_LBS = True
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

    # should already exist from table scraper
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

    # PDF tracking
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


def iter_lbs_from_progress(con):
    if ONLY_DONE_LBS:
        cur = con.execute("SELECT key, csv_path FROM progress WHERE status='DONE'")
    else:
        cur = con.execute("SELECT key, csv_path FROM progress")

    for key, csv_path in cur.fetchall():
        if csv_path and os.path.exists(csv_path):
            yield key, csv_path


def db_pdf_seen_done(con, pdf_url: str) -> bool:
    cur = con.execute("SELECT status FROM pdf_downloads WHERE pdf_url=?", (pdf_url,))
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
        # might be HTML or not; assume HTML for this method
        self._parse_form(r.text)
        return self.soup

    def postback_raw(self, event_target, event_argument="", updates=None):
        """
        Same as postback(), but returns raw Response.
        Caller decides whether it's PDF or HTML.
        """
        data = dict(self.form_data)
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument
        r = self._request_with_retries("POST", BASE_URL, data=data)
        return r

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


# ===================== CSV READER =====================

def read_csv_minimal(csv_path):
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
                    "district_index": int(row["district_index"]),
                    "district_name": row["district_name"],
                    "localbody_index": int(row["localbody_index"]),
                    "localbody_name": row["localbody_name"],
                }
            project_nos.append(str(row["project_no"]).strip())
    return meta, project_nos


def build_lb_folder(meta):
    district = slugify(f"{meta['district_index']}_{meta['district_name']}")
    lb = slugify(f"{meta['localbody_index']}_{meta['localbody_name']}")
    path = os.path.join(PDF_ROOT, district, lb)
    os.makedirs(path, exist_ok=True)
    return path


# ===================== NAV BUILD =====================

def parse_district_rows(soup, client: SulekhaClient):
    gv = soup.find("table", {"id": "gvState"})
    out = {}
    if not gv:
        return out
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
        pb = client.extract_postback_from_link(link)
        if pb:
            out[didx] = {"district_index": didx, "district_name": dname, "postback": pb, "lbs": {}}
    return out


def parse_localbody_rows(soup, client: SulekhaClient):
    gv = soup.find("table", {"id": "gvStat"})
    out = {}
    if not gv:
        return out
    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue
        try:
            lbidx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue
        lbname = tds[1].get_text(strip=True)
        link = tds[-1].find("a")
        pb = client.extract_postback_from_link(link)
        if pb:
            out[lbidx] = {"lb_index": lbidx, "lb_name": lbname, "postback": pb}
    return out


def build_navigation_index_for_year_lbtype(client, year_val, lb_val):
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": str(year_val)})
    client.postback("drpType", "", updates={"drpType": str(lb_val)})

    districts = parse_district_rows(client.soup, client)

    # for each district, load LB list and cache postbacks
    for didx, d in districts.items():
        dt, da = d["postback"]
        try:
            client.postback(dt, da)
        except Exception:
            client.load_base()
            client.postback("drpYear", "", updates={"drpYear": str(year_val)})
            client.postback("drpType", "", updates={"drpType": str(lb_val)})
            client.postback(dt, da)

        lbs = parse_localbody_rows(client.soup, client)
        d["lbs"] = lbs

        # back to district list
        client.load_base()
        client.postback("drpYear", "", updates={"drpYear": str(year_val)})
        client.postback("drpType", "", updates={"drpType": str(lb_val)})

    return districts


# ===================== PROJECTS PAGE PARSING =====================

def parse_pagination(soup, client: SulekhaClient):
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
            pb = client.extract_postback_from_link(a)
            if not pb:
                continue
            if txt == "...":
                ellipsis.append(pb)
            elif txt.isdigit():
                pages[int(txt)] = pb

    return current, pages, ellipsis


def find_select_postback_for_project_no_on_page(soup, client: SulekhaClient, project_no: str):
    """
    Finds the Select$N postback for the row whose first column equals project_no.
    (Your observation: javascript:__doPostBack('gvProjects','Select$19'))
    """
    gv = soup.find("table", {"id": "gvProjects"})
    if not gv:
        return None

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        pno = tds[0].get_text(strip=True)
        if pno != str(project_no).strip():
            continue

        # find any <a> in this row that contains Select$...
        for a in tr.find_all("a"):
            pb = client.extract_postback_from_link(a)
            if not pb:
                continue
            tgt, arg = pb
            if tgt == "gvProjects" and arg and "Select$" in arg:
                return pb

        # sometimes Select isn't on <a> but on onclick elsewhere; fallback: scan attributes
        html = str(tr)
        m = POSTBACK_RE.search(html)
        if m and m.group("target") == "gvProjects" and "Select$" in m.group("argument"):
            return (m.group("target"), m.group("argument"))

    return None


def response_is_pdf(resp: requests.Response) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    cdisp = (resp.headers.get("Content-Disposition") or "").lower()
    if "application/pdf" in ctype:
        return True
    if ".pdf" in cdisp:
        return True
    return False


def save_pdf_bytes(resp: requests.Response, out_path: str):
    with open(out_path, "wb") as f:
        f.write(resp.content)


def extract_pdf_like_urls_from_html(html: str):
    """
    Fallback if select returns HTML page.
    We look for any href/src containing .pdf
    """
    soup = BeautifulSoup(html, "lxml")
    urls = set()
    for tag in soup.find_all(["a", "iframe", "embed"]):
        for attr in ("href", "src"):
            u = (tag.get(attr) or "").strip()
            if u and ".pdf" in u.lower():
                urls.add(u)
    return soup, sorted(urls)


def try_find_pdf_download_postback(soup, client: SulekhaClient):
    """
    Fallback: sometimes details page has a linkbutton to download.
    We'll pick the first postback whose text hints at pdf/view/download/print.
    """
    hints = ("pdf", "download", "view", "print", "report")
    for a in soup.find_all("a"):
        txt = (a.get_text(strip=True) or "").lower()
        if any(h in txt for h in hints):
            pb = client.extract_postback_from_link(a)
            if pb:
                return pb
    return None


# ===================== CONTEXT NAV =====================

def go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback):
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": str(year_val)})
    client.postback("drpType", "", updates={"drpType": str(lb_val)})
    dt, da = district_postback
    client.postback(dt, da)
    lt, la = lb_postback
    client.postback(lt, la)


def go_to_lb_projects_page(client, year_val, lb_val, district_postback, lb_postback, target_page: int):
    """
    Re-enter LB projects, then navigate to a specific page number (if possible).
    """
    go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback)
    if target_page <= 1:
        return

    # try to reach that page by clicking ellipsis until it appears
    for _ in range(50):  # safety cap
        cur, pages, ell = parse_pagination(client.soup, client)
        if target_page in pages:
            nt, na = pages[target_page]
            client.postback(nt, na)
            return
        if ell:
            nt, na = ell[-1]
            client.postback(nt, na)
        else:
            return


# ===================== PDF DOWNLOADER FOR ONE LB =====================

def download_pdfs_for_lb(con, client, lb_key, meta, project_nos, district_postback, lb_postback):
    lb_folder = build_lb_folder(meta)
    year_val = meta["year_value"]
    lb_val = meta["lbtype_value"]

    print(f"\n▶ PDFs for LB: {meta['localbody_name']}  ({meta['district_name']})")
    print(f"  Folder: {lb_folder}")
    print(f"  Projects in CSV: {len(project_nos)}")

    # Start at page 1
    go_to_lb_projects(client, year_val, lb_val, district_postback, lb_postback)

    remaining = set(project_nos)
    visited_pages = set()
    page_fallback = 0

    while remaining:
        page_fallback += 1
        current_page, pages, ellipsis = parse_pagination(client.soup, client)
        page_id = current_page if current_page is not None else page_fallback

        if page_id in visited_pages:
            print(f"  ⚠ Pagination loop at page {page_id}. Stopping LB.")
            break
        visited_pages.add(page_id)

        # Identify which remaining projects are visible on this page
        gv = client.soup.find("table", {"id": "gvProjects"})
        present = []
        if gv:
            rows = gv.find_all("tr", recursive=False)
            for tr in rows[1:]:
                tds = tr.find_all("td", recursive=False)
                if len(tds) < 2:
                    continue
                pno = tds[0].get_text(strip=True)
                if pno in remaining:
                    present.append(pno)

        print(f"  Page {page_id}: found {len(present)} target projects")

        for pno in present:
            # Decide output filename (one pdf per project; if it varies, Content-Disposition will decide)
            fname = slugify(f"project_{pno}.pdf", 180)
            out_path = os.path.join(lb_folder, fname)

            # We'll use a synthetic pdf_url key (since the pdf may not have a stable URL).
            # Use a stable pseudo-URL so sqlite can mark it DONE:
            synthetic_pdf_url = f"{BASE_URL}#LB={lb_key}#P={pno}"

            if not OVERWRITE_PDFS:
                if db_pdf_seen_done(con, synthetic_pdf_url) and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    remaining.discard(pno)
                    continue

            pb = find_select_postback_for_project_no_on_page(client.soup, client, pno)
            if not pb:
                print(f"    ⚠ Could not find Select$ row for project {pno} on page {page_id}")
                continue

            print(f"    ▶ Project {pno}: clicking Select...")
            tgt, arg = pb

            try:
                resp = client.postback_raw(tgt, arg)
            except Exception as e:
                db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", err=str(e))
                print(f"      ❌ Select failed: {e}")
                # restore current projects page
                go_to_lb_projects_page(client, year_val, lb_val, district_postback, lb_postback, page_id)
                continue

            # Case A: server returns PDF bytes directly
            if response_is_pdf(resp):
                # If server supplies a filename, prefer it
                cd = resp.headers.get("Content-Disposition") or ""
                m = re.search(r'filename="?([^"]+)"?', cd, flags=re.IGNORECASE)
                if m:
                    fname2 = slugify(m.group(1), 180)
                    out_path = os.path.join(lb_folder, fname2)

                try:
                    save_pdf_bytes(resp, out_path)
                    db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "DONE", http_status=resp.status_code, err=None)
                    remaining.discard(pno)
                    print(f"      ✅ Saved PDF: {os.path.basename(out_path)}")
                except Exception as e:
                    db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", http_status=resp.status_code, err=str(e))
                    print(f"      ❌ Save failed: {e}")

                # restore projects page
                go_to_lb_projects_page(client, year_val, lb_val, district_postback, lb_postback, page_id)
                continue

            # Case B: HTML details page; parse for pdf link or pdf download postback
            html = resp.text
            try:
                client._parse_form(html)
            except Exception:
                # if parsing fails, just restore and mark failed
                db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", http_status=resp.status_code, err="Non-HTML/non-PDF response")
                go_to_lb_projects_page(client, year_val, lb_val, district_postback, lb_postback, page_id)
                continue

            details_soup = client.soup
            _, pdf_urls = extract_pdf_like_urls_from_html(html)

            # If details page contains a pdf URL:
            if pdf_urls:
                full = urljoin(BASE_URL, pdf_urls[0])
                # download via direct GET
                try:
                    r2 = client._request_with_retries("GET", full)
                    if response_is_pdf(r2):
                        save_pdf_bytes(r2, out_path)
                        db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "DONE", http_status=r2.status_code, err=None)
                        remaining.discard(pno)
                        print(f"      ✅ Saved PDF (from HTML link): {os.path.basename(out_path)}")
                    else:
                        db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", http_status=r2.status_code, err="PDF link did not return PDF")
                        print("      ❌ Link did not return PDF")
                except Exception as e:
                    db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", err=str(e))
                    print(f"      ❌ Download failed: {e}")

                go_to_lb_projects_page(client, year_val, lb_val, district_postback, lb_postback, page_id)
                continue

            # Else: try to find a "download/view pdf" postback on details page
            pb2 = try_find_pdf_download_postback(details_soup, client)
            if pb2:
                print("      ▶ Details page: trying download/view postback...")
                try:
                    resp3 = client.postback_raw(pb2[0], pb2[1])
                    if response_is_pdf(resp3):
                        save_pdf_bytes(resp3, out_path)
                        db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "DONE", http_status=resp3.status_code, err=None)
                        remaining.discard(pno)
                        print(f"      ✅ Saved PDF (download postback): {os.path.basename(out_path)}")
                    else:
                        db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", http_status=resp3.status_code, err="Download postback did not return PDF")
                        print("      ❌ Download postback did not return PDF")
                except Exception as e:
                    db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", err=str(e))
                    print(f"      ❌ Download postback failed: {e}")
            else:
                db_pdf_upsert(con, lb_key, pno, synthetic_pdf_url, out_path, "FAILED", http_status=resp.status_code, err="No PDF found on details page")
                print("      ❌ No PDF link/postback found on details page.")

            # restore projects page
            go_to_lb_projects_page(client, year_val, lb_val, district_postback, lb_postback, page_id)
            remaining.discard(pno)

            con.commit()

        # move to next page
        want = (current_page + 1) if current_page is not None else (page_id + 1)
        next_pb = pages.get(want)
        if not next_pb and ellipsis:
            next_pb = ellipsis[-1]

        if not next_pb:
            break

        try:
            client.postback(next_pb[0], next_pb[1])
        except Exception:
            # hard restore to next page if possible
            go_to_lb_projects_page(client, year_val, lb_val, district_postback, lb_postback, want)

    print(f"✅ Finished LB PDFs: {meta['localbody_name']}")
    return True


# ===================== MASTER =====================

def run_pdf_scraper():
    con = db_connect()
    client = SulekhaClient()

    lb_items = list(iter_lbs_from_progress(con))
    if not lb_items:
        print("❌ No LBs found in progress DB (or CSVs missing). Check DB_PATH/CSV_DIR.")
        return

    print(f"✅ Found {len(lb_items)} LBs to consider for PDF downloads.")
    print(f"📂 CSV folder: {CSV_DIR}")
    print(f"📂 PDF root:   {PDF_ROOT}")
    print(f"🗄 DB:         {DB_PATH}")

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
        didx = meta["district_index"]
        lbidx = meta["localbody_index"]

        group = (str(year_val), str(lb_val))
        if group not in nav_cache:
            print(f"\n=== Building navigation index for Year={year_val}, LBType={lb_val} ===")
            try:
                nav_cache[group] = build_navigation_index_for_year_lbtype(client, year_val, lb_val)
            except Exception as e:
                print(f"❌ Failed building navigation index for {group}: {e}")
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
            continue

    con.commit()
    con.close()
    print("\n✅ PDF scraping run completed.")


if __name__ == "__main__":
    run_pdf_scraper()
