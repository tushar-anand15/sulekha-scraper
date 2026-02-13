import os
import re
import csv
import time
import json
import random
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ================= CONFIG =================

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

# Existing TABLE progress DB (from your CSV scraper)
TABLE_DB_PATH = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables\sulekha_progress.sqlite"

# NEW PDF root folder + logs + PDF progress DB
PDF_ROOT = r"C:\Users\csabi\Downloads\Sulekha_Data\PDFs"
os.makedirs(PDF_ROOT, exist_ok=True)

PDF_DB_PATH = os.path.join(PDF_ROOT, "pdf_progress.sqlite")

DOWNLOADED_LOG = os.path.join(PDF_ROOT, "pdf_downloaded_log.csv")
MISSING_LOG = os.path.join(PDF_ROOT, "pdf_missing_log.csv")
ERROR_LOG = os.path.join(PDF_ROOT, "pdf_error_log.csv")

REQUEST_TIMEOUT = 60
DELAY_BETWEEN_REQUESTS = 1.0

MAX_RETRIES = 8
BACKOFF_BASE = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 180.0

# For sniff testing: set to e.g. {"2024-2025","2023-2024"} or None for all.
YEAR_LABEL_FILTER = {"2024-2025", "2023-2024"}  # ✅ change or set to None

# Download only this many LBs (testing) - set None for all
LIMIT_LBS = None

# Download only this many projects per LB (testing) - set None for all
LIMIT_PROJECTS_PER_LB = None

POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")

print("🔹 Running sulekha_pdf_scraper_v3.0.py")
print("🔹 TABLE_DB_PATH:", TABLE_DB_PATH)
print("🔹 PDF_ROOT:", os.path.abspath(PDF_ROOT))
print("🔹 PDF_DB_PATH:", os.path.abspath(PDF_DB_PATH))
print("🔹 Logs:", os.path.abspath(DOWNLOADED_LOG))

# ================= UTILS =================

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def slugify(s, max_len=70):
    # Windows-safe-ish; keep unicode but remove forbidden filename chars
    s = (s or "").strip().replace(" ", "_")
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = re.sub(r"\s+", "_", s)
    if not s:
        s = "name"
    return s[:max_len]

def ensure_csv_header(path, header):
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)

def append_log(path, row):
    with open(path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(row)

def is_pdf_response(resp: requests.Response) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    cdisp = (resp.headers.get("Content-Disposition") or "").lower()
    if "application/pdf" in ctype:
        return True
    if ".pdf" in cdisp:
        return True
    return False

# ================= SQLITE: PDF PROGRESS =================

def pdf_db_connect():
    con = sqlite3.connect(PDF_DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-20000;")

    con.execute("""
        CREATE TABLE IF NOT EXISTS pdf_progress (
            key TEXT PRIMARY KEY,
            year_val TEXT,
            year_label TEXT,
            lb_val TEXT,
            lb_label TEXT,
            district_index INTEGER,
            district_name TEXT,
            localbody_index INTEGER,
            localbody_name TEXT,
            project_no TEXT,
            select_arg TEXT,
            status TEXT,            -- DOWNLOADED / MISSING / ERROR
            pdf_filename TEXT,
            pdf_url TEXT,
            notes TEXT,
            updated_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_pdf_status ON pdf_progress(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pdf_lb ON pdf_progress(year_val, lb_val, district_index, localbody_index)")
    con.commit()
    return con

def make_pdf_key(year_val, lb_val, district_index, localbody_index, project_no):
    return f"{year_val}|{lb_val}|{district_index}|{localbody_index}|{project_no}"

def pdf_already_done(con, key):
    row = con.execute("SELECT status FROM pdf_progress WHERE key=?", (key,)).fetchone()
    if not row:
        return False
    return row[0] in ("DOWNLOADED", "MISSING")

def pdf_mark(con, *, key, meta, status, filename=None, url=None, notes=None):
    con.execute("""
        INSERT OR REPLACE INTO pdf_progress
        (key, year_val, year_label, lb_val, lb_label, district_index, district_name,
         localbody_index, localbody_name, project_no, select_arg, status, pdf_filename, pdf_url, notes, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        key,
        str(meta["year_val"]), str(meta["year_label"]),
        str(meta["lb_val"]), str(meta["lb_label"]),
        int(meta["district_index"]), str(meta["district_name"]),
        int(meta["localbody_index"]), str(meta["localbody_name"]),
        str(meta["project_no"]), str(meta["select_arg"]),
        status,
        filename,
        url,
        notes,
        now_utc()
    ))
    con.commit()

def next_seq_for_lb(con, year_val, lb_val, district_index, localbody_index):
    # count already DOWNLOADED for this LB; use as seq base (fast)
    row = con.execute("""
        SELECT COUNT(1)
        FROM pdf_progress
        WHERE year_val=? AND lb_val=? AND district_index=? AND localbody_index=? AND status='DOWNLOADED'
    """, (str(year_val), str(lb_val), int(district_index), int(localbody_index))).fetchone()
    n = int(row[0]) if row and row[0] is not None else 0
    return n + 1

# ================= READ LBs FROM TABLE DB =================

def read_done_lbs_from_table_db():
    """
    Reads your existing sulekha_progress.sqlite (table scraper) and returns
    LB records that are DONE. This avoids opening district tables just to enumerate LBs.
    """
    if not os.path.exists(TABLE_DB_PATH):
        raise FileNotFoundError(f"TABLE_DB_PATH not found: {TABLE_DB_PATH}")

    con = sqlite3.connect(TABLE_DB_PATH)
    # progress table schema: key, csv_path, expected, scraped, status, updated_at
    rows = con.execute("""
        SELECT key, csv_path, expected, scraped, status
        FROM progress
        WHERE status='DONE'
    """).fetchall()
    con.close()

    out = []
    for key, csv_path, expected, scraped, status in rows:
        # key is: year|lbtype|district|lbindex in your v3+ scripts
        parts = str(key).split("|")
        if len(parts) != 4:
            continue
        year_val, lb_val, district_index, localbody_index = parts

        # parse labels/names from csv_path filename if possible
        # csv name: y{year_val}__lb{lb_val}__{district_index}_{district_name}__{lb_index}_{lb_name}.csv
        district_name = ""
        localbody_name = ""
        year_label = ""
        lb_label = ""

        base = os.path.basename(csv_path or "")
        # Try extracting district/localbody names from filename:
        try:
            # y29__lb5__1_Kasargod__1_Ajanoor_Grama_Panchayat.csv
            chunks = base.split("__")
            if len(chunks) >= 4:
                # chunks[2] = district chunk, chunks[3] = lb chunk
                dchunk = chunks[2]
                lbchunk = chunks[3].rsplit(".", 1)[0]
                # dchunk begins with "{district_index}_..."
                if "_" in dchunk:
                    district_name = dchunk.split("_", 1)[1].replace("_", " ")
                if "_" in lbchunk:
                    localbody_name = lbchunk.split("_", 1)[1].replace("_", " ")
        except Exception:
            pass

        out.append({
            "year_val": year_val,
            "lb_val": lb_val,
            "district_index": int(district_index),
            "district_name": district_name,
            "localbody_index": int(localbody_index),
            "localbody_name": localbody_name,
            "expected": expected,
            "csv_path": csv_path,
            "scraped": scraped
        })

    return out

# ================= WEBFORMS CLIENT =================

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

                # Retry 5xx
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

                # Recreate session sometimes (but not too often—cookies matter)
                if attempt == 6:
                    print("    🔄 Recreating HTTP session...")
                    self._new_session()
                    # after recreating session, reload base later by caller if needed

        raise last_exc

    def load_base(self):
        print("▶ Loading base page:", BASE_URL)
        r = self._request_with_retries("GET", BASE_URL)
        self._parse_form(r.text)
        print("✅ Base page loaded.")

    def postback(self, event_target, event_argument="", updates=None, stream=False):
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        r = self._request_with_retries("POST", BASE_URL, data=data, stream=stream)
        if stream and is_pdf_response(r):
            # Caller will handle saving; don't parse HTML
            return r

        # HTML response
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
        years = []
        if not sel:
            return years
        for opt in sel.find_all("option"):
            val = opt.get("value")
            txt = opt.get_text(strip=True)
            if val and val != "0":
                years.append((val, txt))
        return years

    def get_lbtype_options(self):
        sel = self.soup.find("select", {"id": "drpType"})
        types_ = []
        if not sel:
            return types_
        for opt in sel.find_all("option"):
            val = opt.get("value")
            txt = opt.get_text(strip=True)
            if val and val != "0":
                types_.append((val, txt))
        return types_

# ================= PARSERS =================

def parse_district_rows(soup):
    gv = soup.find("table", {"id": "gvState"})
    out = []
    if not gv:
        return out

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        try:
            idx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue
        dname = tds[1].get_text(strip=True)
        link = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb:
            continue
        out.append({"district_index": idx, "district_name": dname, "postback": pb})
    return out

def parse_localbody_rows(soup):
    gv = soup.find("table", {"id": "gvStat"})
    out = []
    if not gv:
        return out

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 3:
            continue
        try:
            lb_idx = int(tds[0].get_text(strip=True))
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
        out.append({
            "localbody_index": lb_idx,
            "localbody_name": lb_name,
            "no_of_projects": no_of_projects,
            "postback": pb
        })
    return out

def parse_projects_page(soup):
    """
    From gvProjects, returns:
      projects: list of dicts:
        - project_no (text)
        - select_arg (e.g. "Select$19") for js:__doPostBack('gvProjects','Select$19')
      pager: dict {current_page, pages, ellipsis_postbacks}
    """
    gv = soup.find("table", {"id": "gvProjects"})
    projects = []
    pager = {"current_page": None, "pages": {}, "ellipsis": []}

    if not gv:
        return projects, pager

    rows = gv.find_all("tr", recursive=False)
    if not rows:
        return projects, pager

    pager_tr = None
    for tr in rows:
        if tr.find("table"):
            pager_tr = tr
            break

    # data rows: skip header, skip pager row
    for tr in rows[1:]:
        if tr is pager_tr:
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue

        project_no = tds[0].get_text(strip=True)

        # Find a link in the row that contains Select$N
        select_arg = None
        for a in tr.find_all("a"):
            pb = SulekhaClient.extract_postback_from_link(a)
            if not pb:
                continue
            target, arg = pb
            if target == "gvProjects" and (arg or "").startswith("Select$"):
                select_arg = arg
                break

        if not project_no or not select_arg:
            continue

        projects.append({
            "project_no": project_no,
            "select_arg": select_arg,
        })

    # pager parse
    if pager_tr:
        for span in pager_tr.find_all("span"):
            txt = span.get_text(strip=True)
            if txt.isdigit():
                pager["current_page"] = int(txt)
                break

        for a in pager_tr.find_all("a"):
            txt = a.get_text(strip=True)
            pb = SulekhaClient.extract_postback_from_link(a)
            if not pb:
                continue
            if txt == "...":
                pager["ellipsis"].append(pb)
            elif txt.isdigit():
                pager["pages"][int(txt)] = pb

    return projects, pager

def find_pdf_url_in_html(soup):
    """
    Heuristics: look for links or embeds pointing to PDF.
    Returns url (may be relative) or None.
    """
    # direct links
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if href.lower().endswith(".pdf") or ".pdf?" in href.lower():
            return href

    # iframe/object/embed src
    for tag in soup.find_all(["iframe", "embed", "object"]):
        src = (tag.get("src") or tag.get("data") or "").strip()
        if src and (src.lower().endswith(".pdf") or ".pdf?" in src.lower() or "pdf" in src.lower()):
            return src

    # any script text containing .pdf
    txt = soup.get_text(" ", strip=True)
    if ".pdf" in txt.lower():
        # Not reliable to parse from plain text; skip
        pass

    return None

def try_find_back_postback(soup):
    """
    After selecting a project, page may show a "Back" link/button.
    Try to find a postback that returns to gvProjects list.
    """
    # common candidates
    candidates = []

    # links/buttons with "Back"
    for a in soup.find_all("a"):
        t = a.get_text(strip=True).lower()
        if t in {"back", "<<" , "< back", "go back"} or "back" in t:
            pb = SulekhaClient.extract_postback_from_link(a)
            if pb:
                candidates.append(pb)

    # input buttons
    for inp in soup.find_all("input"):
        t = (inp.get("value") or "").strip().lower()
        if "back" in t:
            pb = SulekhaClient.extract_postback_from_link(inp)
            if pb:
                candidates.append(pb)

    return candidates[0] if candidates else None

# ================= NAV HELPERS =================

def reset_to_year_lb_district_lb(client, year_val, lb_val, district_pb, lb_index):
    """
    Rebuilds the navigation state:
      base -> year -> lbtype -> district -> choose LB row by index (re-find)
    """
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": year_val})
    client.postback("drpType", "", updates={"drpType": lb_val})
    dt, da = district_pb
    client.postback(dt, da)
    lb_rows = parse_localbody_rows(client.soup)
    lb = next((x for x in lb_rows if x["localbody_index"] == lb_index), None)
    if not lb:
        return None
    lt, la = lb["postback"]
    client.postback(lt, la)
    return lb

# ================= PDF DOWNLOAD CORE =================

def build_pdf_filename(meta, seq):
    # taxonomy-like name, all in one folder
    # Example:
    # y28__lb1__d1_Thiruvananthapuram__p1_Thiruvananthapuram_District_Panchayat__00001__proj_1.pdf
    dslug = slugify(f"{meta['district_index']}_{meta['district_name']}", 55)
    lbslug = slugify(f"{meta['localbody_index']}_{meta['localbody_name']}", 55)
    proj = slugify(f"proj_{meta['project_no']}", 20)
    return f"y{meta['year_val']}__lb{meta['lb_val']}__d{dslug}__p{lbslug}__{seq:05d}__{proj}.pdf"

def save_pdf_bytes(path, resp: requests.Response):
    with open(path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 128):
            if chunk:
                f.write(chunk)

def download_pdf_by_url(client: SulekhaClient, pdf_url: str):
    full = urljoin(BASE_URL, pdf_url)
    r = client._request_with_retries("GET", full, stream=True)
    if not is_pdf_response(r):
        # sometimes endpoints return HTML error pages
        return None, full, f"Non-PDF response content-type={r.headers.get('Content-Type')}"
    return r, full, None

def scrape_pdfs_for_one_lb(con_pdf, client, year, lbtype, district, lb):
    """
    Assumes current state is already at LB's gvProjects page.
    Iterates pages and projects; downloads PDFs.
    """
    meta_base = {
        "year_val": year["year_val"],
        "year_label": year["year_label"],
        "lb_val": lbtype["lb_val"],
        "lb_label": lbtype["lb_label"],
        "district_index": district["district_index"],
        "district_name": district["district_name"],
        "localbody_index": lb["localbody_index"],
        "localbody_name": lb["localbody_name"],
    }

    print(f"        ▶ PDF scrape LB '{lb['localbody_name']}'")

    visited_pages = set()
    page_fallback = 0
    total_projects_seen = 0
    dl_count = 0

    while True:
        page_fallback += 1
        projects, pager = parse_projects_page(client.soup)
        current_page = pager["current_page"]
        page_id = current_page if current_page is not None else page_fallback

        if page_id in visited_pages:
            print(f"          ⚠ Pagination loop at page {page_id}. Stopping LB.")
            break
        visited_pages.add(page_id)

        print(f"          Page {page_id}: {len(projects)} projects")

        for proj in projects:
            total_projects_seen += 1
            if LIMIT_PROJECTS_PER_LB and total_projects_seen > LIMIT_PROJECTS_PER_LB:
                print("          (limit reached for projects per LB)")
                return dl_count

            meta = dict(meta_base)
            meta["project_no"] = proj["project_no"]
            meta["select_arg"] = proj["select_arg"]

            key = make_pdf_key(meta["year_val"], meta["lb_val"], meta["district_index"], meta["localbody_index"], meta["project_no"])

            if pdf_already_done(con_pdf, key):
                continue

            # determine next seq for this LB (so filenames are 00001..n)
            seq = next_seq_for_lb(con_pdf, meta["year_val"], meta["lb_val"], meta["district_index"], meta["localbody_index"])
            filename = build_pdf_filename(meta, seq)
            out_path = os.path.join(PDF_ROOT, filename)

            # Try 1: postback Select$N and see if it returns PDF directly (stream)
            try:
                # Use stream=True so if server responds with PDF, we catch it
                resp = client.postback("gvProjects", proj["select_arg"], stream=True)
                if isinstance(resp, requests.Response) and is_pdf_response(resp):
                    save_pdf_bytes(out_path, resp)
                    pdf_mark(con_pdf, key=key, meta=meta, status="DOWNLOADED", filename=filename, url="(direct postback pdf)", notes="pdf via Select$ stream")
                    append_log(DOWNLOADED_LOG, [now_utc(), filename, meta["year_label"], meta["lb_label"], meta["district_name"], meta["localbody_name"], meta["project_no"], "direct_postback_pdf", ""])
                    dl_count += 1
                    print(f"            ✅ Downloaded (direct): {filename}")
                    # after direct download, we likely remain on gvProjects page; if not, attempt to parse/return
                else:
                    # resp was soup (HTML parsed)
                    soup = client.soup

                    # Try to find a PDF URL in the resulting HTML
                    pdf_url = find_pdf_url_in_html(soup)
                    if not pdf_url:
                        pdf_mark(con_pdf, key=key, meta=meta, status="MISSING", filename=None, url=None, notes="No PDF URL found after Select$")
                        append_log(MISSING_LOG, [now_utc(), meta["year_label"], meta["lb_label"], meta["district_name"], meta["localbody_name"], meta["project_no"], meta["select_arg"], "No PDF URL found"])
                        print(f"            ⚠ No PDF URL found for proj {meta['project_no']}")
                    else:
                        r_pdf, full_url, err = download_pdf_by_url(client, pdf_url)
                        if r_pdf is None:
                            pdf_mark(con_pdf, key=key, meta=meta, status="ERROR", filename=None, url=full_url, notes=err)
                            append_log(ERROR_LOG, [now_utc(), meta["year_label"], meta["lb_label"], meta["district_name"], meta["localbody_name"], meta["project_no"], full_url, err])
                            print(f"            ❌ PDF download error for proj {meta['project_no']}: {err}")
                        else:
                            save_pdf_bytes(out_path, r_pdf)
                            pdf_mark(con_pdf, key=key, meta=meta, status="DOWNLOADED", filename=filename, url=full_url, notes="pdf via url in html")
                            append_log(DOWNLOADED_LOG, [now_utc(), filename, meta["year_label"], meta["lb_label"], meta["district_name"], meta["localbody_name"], meta["project_no"], full_url, ""])
                            dl_count += 1
                            print(f"            ✅ Downloaded: {filename}")

                # Try to return back to projects list if we got navigated into a details view
                # If we are not on gvProjects anymore, attempt back. Otherwise continue.
                if not client.soup.find("table", {"id": "gvProjects"}):
                    back_pb = try_find_back_postback(client.soup)
                    if back_pb:
                        bt, ba = back_pb
                        client.postback(bt, ba)
                    # If still not back, hard reset is handled by caller per LB/district
            except Exception as e:
                pdf_mark(con_pdf, key=key, meta=meta, status="ERROR", filename=None, url=None, notes=str(e))
                append_log(ERROR_LOG, [now_utc(), meta["year_label"], meta["lb_label"], meta["district_name"], meta["localbody_name"], meta["project_no"], meta["select_arg"], str(e)])
                print(f"            ❌ Exception for proj {meta['project_no']}: {e}")

                # On any exception, try to restore LB projects page by re-entering LB from district context
                raise

        # pager navigation
        pages = pager["pages"]
        ellipsis = pager["ellipsis"]

        want = (current_page + 1) if current_page is not None else (page_id + 1)
        next_pb = None
        if want in pages:
            next_pb = pages[want]
        elif ellipsis:
            next_pb = ellipsis[-1]  # advance the window

        if not next_pb:
            break

        nt, na = next_pb
        client.postback(nt, na)

    return dl_count

# ================= MAIN =================

def main():
    ensure_csv_header(DOWNLOADED_LOG, ["timestamp_utc","pdf_filename","year_label","lbtype_label","district_name","localbody_name","project_no","pdf_url","notes"])
    ensure_csv_header(MISSING_LOG, ["timestamp_utc","year_label","lbtype_label","district_name","localbody_name","project_no","select_arg","reason"])
    ensure_csv_header(ERROR_LOG, ["timestamp_utc","year_label","lbtype_label","district_name","localbody_name","project_no","url_or_select","error"])

    lbs = read_done_lbs_from_table_db()

    # Build quick filter set from TABLE db outputs
    # We still need labels (year_label, lb_label, district_name, lb_name) from website,
    # so we will navigate on site and overwrite blanks when we discover them.

    con_pdf = pdf_db_connect()
    client = SulekhaClient(delay=DELAY_BETWEEN_REQUESTS)

    # Start session and read dropdowns so we have labels
    client.load_base()
    year_options = dict(client.get_year_options())     # year_val -> year_label
    lbtype_options = dict(client.get_lbtype_options()) # lb_val -> lb_label

    # Filter LB list by YEAR_LABEL_FILTER if provided
    filtered = []
    for rec in lbs:
        ylab = year_options.get(str(rec["year_val"]), "")
        if YEAR_LABEL_FILTER is not None and ylab not in YEAR_LABEL_FILTER:
            continue
        filtered.append(rec)

    print(f"✅ Loaded DONE LBs from table DB: {len(lbs)}")
    print(f"✅ After year filter: {len(filtered)}")

    if LIMIT_LBS is not None:
        filtered = filtered[:LIMIT_LBS]

    # Group by (year_val, lb_val) so we can navigate efficiently
    filtered.sort(key=lambda r: (int(r["year_val"]), int(r["lb_val"]), int(r["district_index"]), int(r["localbody_index"])))

    current_year_val = None
    current_lb_val = None

    for rec in filtered:
        year_val = str(rec["year_val"])
        lb_val = str(rec["lb_val"])
        d_idx = int(rec["district_index"])
        lb_idx = int(rec["localbody_index"])

        year_label = year_options.get(year_val, f"year_{year_val}")
        lb_label = lbtype_options.get(lb_val, f"lbtype_{lb_val}")

        print("\n==============================")
        print(f"YEAR {year_label} ({year_val}) | LB TYPE {lb_label} ({lb_val})")
        print(f"DISTRICT IDX {d_idx} | LB IDX {lb_idx}")
        print("==============================")

        # Ensure we are at correct year/lbtype
        try:
            if current_year_val != year_val:
                client.postback("drpYear", "", updates={"drpYear": year_val})
                current_year_val = year_val
                current_lb_val = None  # force reset lbtype
            if current_lb_val != lb_val:
                client.postback("drpType", "", updates={"drpType": lb_val})
                current_lb_val = lb_val
        except Exception as e:
            print(f"⚠ Failed to set year/lbtype: {e} -> reload base and retry")
            client.load_base()
            client.postback("drpYear", "", updates={"drpYear": year_val})
            client.postback("drpType", "", updates={"drpType": lb_val})
            current_year_val = year_val
            current_lb_val = lb_val

        # From gvState, get districts to find postback for this district index
        try:
            districts = parse_district_rows(client.soup)
            drow = next((d for d in districts if d["district_index"] == d_idx), None)
            if not drow:
                print(f"⚠ Could not find district index {d_idx} on gvState. Skipping this LB.")
                continue

            # enter district
            dt, da = drow["postback"]
            client.postback(dt, da)

            # From gvStat, find LB postback for this lb_idx
            lb_rows = parse_localbody_rows(client.soup)
            lbrow = next((x for x in lb_rows if x["localbody_index"] == lb_idx), None)
            if not lbrow:
                print(f"⚠ Could not find LOCALBODY index {lb_idx} in district {drow['district_name']}. Skipping.")
                # go back to year/lbtype start
                client.postback("drpYear", "", updates={"drpYear": year_val})
                client.postback("drpType", "", updates={"drpType": lb_val})
                continue

            # enter LB project list
            lt, la = lbrow["postback"]
            client.postback(lt, la)

            # run PDF scrape for this LB
            year_meta = {"year_val": year_val, "year_label": year_label}
            lbtype_meta = {"lb_val": lb_val, "lb_label": lb_label}
            district_meta = {
                "district_index": drow["district_index"],
                "district_name": drow["district_name"],
                "postback": drow["postback"]
            }
            lb_meta = {
                "localbody_index": lbrow["localbody_index"],
                "localbody_name": lbrow["localbody_name"],
                "no_of_projects": lbrow.get("no_of_projects"),
                "postback": lbrow["postback"]
            }

            try:
                downloaded = scrape_pdfs_for_one_lb(con_pdf, client, year_meta, lbtype_meta, district_meta, lb_meta)
                print(f"✅ LB finished: downloaded {downloaded} PDFs")
            except Exception as e:
                print(f"❌ LB crashed while scraping PDFs: {e}")
                print("↻ Attempting to reset session and continue with next LB...")
                # Hard reset to stable state
                try:
                    client.load_base()
                except Exception:
                    pass
                current_year_val = None
                current_lb_val = None
                continue

            # Return to year/lbtype for next LB
            try:
                client.postback("drpYear", "", updates={"drpYear": year_val})
                client.postback("drpType", "", updates={"drpType": lb_val})
            except Exception:
                current_year_val = None
                current_lb_val = None

        except Exception as e:
            print(f"❌ Unexpected navigation error for LB {lb_idx}: {e}")
            print("↻ Resetting base and continuing...")
            try:
                client.load_base()
            except Exception:
                pass
            current_year_val = None
            current_lb_val = None
            continue

    con_pdf.close()
    print("\n✅ DONE. PDFs in:", os.path.abspath(PDF_ROOT))
    print("✅ Logs:", os.path.abspath(DOWNLOADED_LOG), os.path.abspath(MISSING_LOG), os.path.abspath(ERROR_LOG))


if __name__ == "__main__":
    main()
