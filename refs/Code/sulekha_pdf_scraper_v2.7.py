import os
import re
import time
import random
import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ================= CONFIG =================

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
os.makedirs(OUT_DIR, exist_ok=True)

# Single PDF dump folder (NO subfolders)
PDF_DIR = os.path.join(OUT_DIR, "sulekha_pdfs")
os.makedirs(PDF_DIR, exist_ok=True)

# Bookmark stored in PDF folder
BOOKMARK_PATH = os.path.join(PDF_DIR, "_bookmark.json")

DB_PATH = os.path.join(OUT_DIR, "sulekha_progress.sqlite")

# timeouts
REQUEST_TIMEOUT = 60
# For PDF/API calls: longer read timeout + streaming
PDF_TIMEOUT = (30, 300)  # (connect, read)

DELAY_BETWEEN_REQUESTS = 0.8

MAX_RETRIES = 8
BACKOFF_BASE = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 180.0

# Sniff test (set None for all years)
YEAR_LABEL_PREFIXES = {"2022", "2023"}  # e.g. {"2024"} or None

# Debug dumps for failures
DEBUG_DUMP_FAILED_HTML = True
DEBUG_HTML_DIR = os.path.join(OUT_DIR, "_pdf_debug_html")
if DEBUG_DUMP_FAILED_HTML:
    os.makedirs(DEBUG_HTML_DIR, exist_ok=True)

print("🔹 Running sulekha_pdf_scraper_v2.7.py")
print("🔹 OUT_DIR:", os.path.abspath(OUT_DIR))
print("🔹 PDF_DIR:", os.path.abspath(PDF_DIR))
print("🔹 Bookmark:", os.path.abspath(BOOKMARK_PATH))
print("🔹 Progress DB:", os.path.abspath(DB_PATH))

POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")
PDF_URL_RE = re.compile(r"""(?P<url>(https?://|/)[^'"\s>]+?\.pdf[^'"\s>]*)""", re.IGNORECASE)
# capture any sgwapi URL (often not ending with .pdf)
SGWAPI_URL_RE = re.compile(r"""(?P<url>https?://sgwapi\.lsgkerala\.gov\.in[^'"\s<]+)""", re.IGNORECASE)

# ================= BOOKMARK =================

def load_bookmark():
    if not os.path.exists(BOOKMARK_PATH):
        return None
    try:
        with open(BOOKMARK_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def save_bookmark(bm: dict):
    bm = dict(bm)
    bm["updated_at"] = datetime.now(timezone.utc).isoformat()
    tmp = BOOKMARK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(bm, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BOOKMARK_PATH)

def bookmark_repr(bm):
    if not bm:
        return "None"
    return (
        f"Year={bm.get('year_label')}({bm.get('year_val')}), "
        f"LBType={bm.get('lb_label')}({bm.get('lb_val')}), "
        f"DistrictIdx={bm.get('district_idx')}, LBIdx={bm.get('lb_idx')}, "
        f"ProjectNo={bm.get('project_no')}"
    )

# ================= SQLITE =================

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-20000;")

    con.execute("""
        CREATE TABLE IF NOT EXISTS lb_pdf_state (
            lb_key TEXT PRIMARY KEY,
            next_n INTEGER DEFAULT 1,
            updated_at TEXT
        )
    """)

    con.execute("""
        CREATE TABLE IF NOT EXISTS pdf_progress (
            key TEXT PRIMARY KEY,
            lb_key TEXT NOT NULL,
            pdf_path TEXT,
            status TEXT DEFAULT 'PENDING',
            note TEXT,
            updated_at TEXT
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_pdf_status ON pdf_progress(status)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_pdf_lbkey ON pdf_progress(lb_key)")
    con.commit()
    return con

def now_utc():
    return datetime.now(timezone.utc).isoformat()

def make_lb_key(year_val, lb_val, district_idx, lb_idx):
    return f"{year_val}|{lb_val}|{district_idx}|{lb_idx}"

def make_pdf_key(year_val, lb_val, district_idx, lb_idx, project_no):
    return f"{year_val}|{lb_val}|{district_idx}|{lb_idx}|{project_no}"

def lb_get_next_n(con, lb_key):
    row = con.execute("SELECT next_n FROM lb_pdf_state WHERE lb_key=?", (lb_key,)).fetchone()
    if not row:
        con.execute(
            "INSERT INTO lb_pdf_state(lb_key, next_n, updated_at) VALUES (?,?,?)",
            (lb_key, 1, now_utc()),
        )
        con.commit()
        return 1
    return int(row[0])

def lb_advance_n(con, lb_key):
    n = lb_get_next_n(con, lb_key)
    con.execute(
        "UPDATE lb_pdf_state SET next_n=?, updated_at=? WHERE lb_key=?",
        (n + 1, now_utc(), lb_key),
    )
    con.commit()
    return n

def pdf_get_status(con, pdf_key):
    row = con.execute(
        "SELECT status, pdf_path FROM pdf_progress WHERE key=?",
        (pdf_key,),
    ).fetchone()
    if not row:
        return None
    return {"status": row[0], "pdf_path": row[1]}

def pdf_set(con, pdf_key, lb_key, status, pdf_path=None, note=None):
    con.execute(
        "INSERT OR REPLACE INTO pdf_progress(key, lb_key, pdf_path, status, note, updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (pdf_key, lb_key, pdf_path, status, note, now_utc()),
    )
    con.commit()

# ================= CLIENT =================

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
                "Origin": "https://plan.lsgkerala.gov.in",
            }
        )

    def _sleep(self):
        time.sleep(self.delay)

    def _parse_form(self, html):
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form", {"id": "form1"})
        if form is None:
            raise RuntimeError("Could not find form id='form1'")
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

    def _request_with_retries(self, method, url, timeout=REQUEST_TIMEOUT, stream=False, **kwargs):
        """
        Generic retry wrapper. For PDF/API endpoints, pass timeout=PDF_TIMEOUT and stream=True.
        """
        last_exc = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self._sleep()
                resp = self.s.request(method, url, timeout=timeout, stream=stream, **kwargs)

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

                # IMPORTANT: do NOT recreate session too aggressively (loses cookies)
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

    def postback_raw(self, event_target, event_argument="", updates=None, allow_redirects=True):
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument
        r = self._request_with_retries("POST", BASE_URL, data=data, allow_redirects=allow_redirects)
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

    def get_year_options(self):
        sel = self.soup.find("select", {"id": "drpYear"})
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            text = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, text))
        return out

    def get_lbtype_options(self):
        sel = self.soup.find("select", {"id": "drpType"})
        out = []
        if not sel:
            return out
        for opt in sel.find_all("option"):
            val = opt.get("value")
            text = opt.get_text(strip=True)
            if val and val != "0":
                out.append((val, text))
        return out

# ================= HELPERS =================

def slugify(s, max_len=80):
    s = (s or "").strip().replace(" ", "_")
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = re.sub(r"\s+", "_", s)
    if not s:
        s = "name"
    return s[:max_len]

def make_pdf_filename(meta, n_int, project_no):
    y = f"y{meta['year_val']}"
    lb = f"lb{meta['lb_val']}_{slugify(meta['lb_label'], 25)}"
    d = f"d{meta['district_idx']}_{slugify(meta['district_name'], 35)}"
    p = f"p{meta['lb_idx']}_{slugify(meta['lb_name'], 45)}"
    n = f"n{int(n_int):04d}"
    proj = f"proj{slugify(str(project_no), 12)}"
    return f"{y}__{lb}__{d}__{p}__{n}__{proj}.pdf"

def safe_reset_to_context(client, year_val, lb_val, district_postback):
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": year_val})
    client.postback("drpType", "", updates={"drpType": lb_val})
    dt, da = district_postback
    client.postback(dt, da)

def is_pdf_response(resp: requests.Response):
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ctype:
        return True
    # sometimes octet-stream
    if "application/octet-stream" in ctype:
        head = resp.content[:5] if resp.content else b""
        return head.startswith(b"%PDF")
    head = resp.content[:5] if resp.content else b""
    return head.startswith(b"%PDF")

def extract_pdf_url_from_text(text):
    if not text:
        return None
    m = PDF_URL_RE.search(text)
    return m.group("url") if m else None

def extract_sgwapi_url_from_text(text):
    if not text:
        return None
    m = SGWAPI_URL_RE.search(text)
    return m.group("url") if m else None

def dump_debug_html(tag, html):
    if not DEBUG_DUMP_FAILED_HTML:
        return
    fn = f"{tag}__{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.html"
    path = os.path.join(DEBUG_HTML_DIR, fn)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html or "")
        print(f"            🧾 Dumped debug HTML: {path}")
    except Exception:
        pass

def stream_download_to_file(client, url, out_path):
    # allow relative urls too
    full_url = urljoin(BASE_URL, url)
    r = client._request_with_retries("GET", full_url, timeout=PDF_TIMEOUT, stream=True)
    # ensure folder exists
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 128):
            if chunk:
                f.write(chunk)

# ================= PARSERS =================

def parse_district_rows(soup):
    gv = soup.find("table", {"id": "gvState"})
    out = []
    if not gv:
        return out

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 9:
            continue
        try:
            idx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue

        district_name = tds[1].get_text(strip=True)
        link = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb:
            continue

        out.append({"index": idx, "district_name": district_name, "postback": pb})
    return out

def parse_localbody_rows(soup):
    gv = soup.find("table", {"id": "gvStat"})
    out = []
    if not gv:
        return out

    rows = gv.find_all("tr", recursive=False)
    for tr in rows[1:-1]:
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 7:
            continue
        try:
            idx = int(tds[0].get_text(strip=True))
        except ValueError:
            continue

        lb_name = tds[1].get_text(strip=True)
        link = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb:
            continue

        out.append({"index": idx, "lb_name": lb_name, "postback": pb})
    return out

def parse_projects_with_select_and_pager(soup):
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

    for tr in rows[1:]:
        if tr is pager_tr:
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue

        project_no = tds[0].get_text(strip=True)
        project_name = tds[1].get_text(strip=True)

        if project_name and re.fullmatch(r"[0-9,\s]+", project_name):
            continue

        select_link = tds[-1].find("a")
        select_pb = SulekhaClient.extract_postback_from_link(select_link)

        projects.append({"project_no": project_no, "project_name": project_name, "select_postback": select_pb})

    if pager_tr:
        for span in pager_tr.find_all("span"):
            txt = span.get_text(strip=True)
            if txt.isdigit():
                pager["current_page"] = int(txt)
                break

        for a in pager_tr.find_all("a"):
            text = a.get_text(strip=True)
            pb = SulekhaClient.extract_postback_from_link(a)
            if not pb:
                continue
            if text == "...":
                pager["ellipsis"].append(pb)
            elif text.isdigit():
                pager["pages"][int(text)] = pb

    return projects, pager

# ================= RESUME / SKIP LOGIC =================

def should_skip_until_bookmark(bm, year_val, lb_val, district_idx, lb_idx, project_no):
    if not bm:
        return False, False

    if (str(year_val) != str(bm.get("year_val")) or
        str(lb_val) != str(bm.get("lb_val")) or
        int(district_idx) != int(bm.get("district_idx")) or
        int(lb_idx) != int(bm.get("lb_idx"))):
        return True, False

    bm_proj = str(bm.get("project_no"))
    if bm_proj and str(project_no) != bm_proj:
        return True, False

    return True, True  # reached bookmark

# ================= PDF EXTRACTION LOGIC =================

def resolve_pdf_from_select_response(client, resp):
    """
    Try multiple strategies to get a PDF:
      1) direct PDF bytes
      2) Location header redirect (if any)
      3) find .pdf url in text (html/json)
      4) find sgwapi url in text and fetch it (may produce pdf)
    Returns: ("BYTES", bytes) or ("URL", url) or (None, None)
    """
    # 1) direct PDF
    try:
        if is_pdf_response(resp):
            return ("BYTES", resp.content)
    except Exception:
        pass

    # 2) redirect chain / final URL
    try:
        final_url = getattr(resp, "url", None)
        if final_url and final_url.lower().endswith(".pdf"):
            return ("URL", final_url)
    except Exception:
        pass

    # 3) search response text for pdf url
    text = ""
    try:
        text = resp.text
    except Exception:
        text = ""
    pdf_url = extract_pdf_url_from_text(text)
    if pdf_url:
        return ("URL", pdf_url)

    # 4) search for sgwapi URL (often not .pdf but returns PDF)
    api_url = extract_sgwapi_url_from_text(text)
    if api_url:
        # call the API url (streaming + bigger timeout)
        r2 = client._request_with_retries("GET", api_url, timeout=PDF_TIMEOUT, stream=True)
        # some servers return pdf as octet-stream; detect from headers
        ctype = (r2.headers.get("Content-Type") or "").lower()
        if "application/pdf" in ctype or "application/octet-stream" in ctype:
            # stream to bytes (we can also stream directly to file, but keep uniform)
            data = b""
            chunks = []
            for chunk in r2.iter_content(chunk_size=1024 * 128):
                if chunk:
                    chunks.append(chunk)
            data = b"".join(chunks)
            if data.startswith(b"%PDF"):
                return ("BYTES", data)

        # fallback: maybe redirected to pdf
        if r2.url and r2.url.lower().endswith(".pdf"):
            return ("URL", r2.url)

    return (None, None)

# ================= YEAR FILTER =================

def year_allowed(year_label: str):
    if YEAR_LABEL_PREFIXES is None:
        return True
    year_label = (year_label or "").strip()
    return any(year_label.startswith(pref) for pref in YEAR_LABEL_PREFIXES)

# ================= LB PDF SCRAPER =================

def scrape_lb_pdfs(con, client,
                   year_val, year_label,
                   lb_val, lb_label,
                   district, lb,
                   bm_state):
    d_idx = district["index"]
    d_name = district["district_name"]
    lb_idx = lb["index"]
    lb_name = lb["lb_name"]

    lb_key = make_lb_key(year_val, lb_val, d_idx, lb_idx)

    meta = {
        "year_val": year_val,
        "year_label": year_label,
        "lb_val": lb_val,
        "lb_label": lb_label,
        "district_idx": d_idx,
        "district_name": d_name,
        "lb_idx": lb_idx,
        "lb_name": lb_name,
    }

    print(f"        ▶ PDF scrape LB '{lb_name}'")

    lb_target, lb_arg = lb["postback"]
    client.postback(lb_target, lb_arg)

    visited_pages = set()
    page_fallback = 0

    while True:
        page_fallback += 1
        projects, pager = parse_projects_with_select_and_pager(client.soup)
        current_page = pager["current_page"]
        page_id = current_page if current_page is not None else page_fallback

        if page_id in visited_pages:
            print(f"          ⚠ Page loop at {page_id}. Stopping LB.")
            break
        visited_pages.add(page_id)

        print(f"          Page {page_id}: {len(projects)} projects")

        for proj in projects:
            project_no = proj["project_no"]
            select_pb = proj["select_postback"]

            # bookmark skip
            if bm_state["active"]:
                skip_this, reached_now = should_skip_until_bookmark(
                    bm_state["bookmark"], year_val, lb_val, d_idx, lb_idx, project_no
                )
                if reached_now:
                    bm_state["active"] = False
                    print("          ✅ Bookmark reached; resuming downloads from next project.")
                    continue
                if skip_this:
                    continue

            pdf_key = make_pdf_key(year_val, lb_val, d_idx, lb_idx, project_no)
            st = pdf_get_status(con, pdf_key)
            if st and st["status"] == "DONE" and st["pdf_path"] and os.path.exists(st["pdf_path"]):
                continue

            if not select_pb:
                pdf_set(con, pdf_key, lb_key, "NO_SELECT", None, note="No select postback")
                continue

            try:
                t, a = select_pb

                # IMPORTANT: Select often redirects to sgwapi; allow redirects
                resp = client.postback_raw(t, a, allow_redirects=True)

                kind, payload = resolve_pdf_from_select_response(client, resp)

                if kind is None:
                    # dump html for debugging
                    try:
                        text = resp.text
                    except Exception:
                        text = ""
                    dump_debug_html(
                        f"NO_PDF__y{year_val}__lb{lb_val}__d{d_idx}__p{lb_idx}__proj{slugify(str(project_no),12)}",
                        text
                    )
                    pdf_set(con, pdf_key, lb_key, "NO_PDF", None, note="No PDF resolved from select response")
                    print(f"            ⚠ No PDF resolved for proj {project_no}")
                else:
                    n = lb_advance_n(con, lb_key)
                    filename = make_pdf_filename(meta, n, project_no)
                    out_path = os.path.join(PDF_DIR, filename)

                    if kind == "BYTES":
                        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
                            with open(out_path, "wb") as f:
                                f.write(payload)
                    elif kind == "URL":
                        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
                            # streaming download for large PDFs
                            stream_download_to_file(client, payload, out_path)

                    pdf_set(con, pdf_key, lb_key, "DONE", out_path, note="OK")
                    print(f"            ✅ PDF proj {project_no} -> {os.path.basename(out_path)}")

                    # bookmark after each success
                    save_bookmark({
                        "year_val": year_val, "year_label": year_label,
                        "lb_val": lb_val, "lb_label": lb_label,
                        "district_idx": d_idx, "district_name": d_name,
                        "lb_idx": lb_idx, "lb_name": lb_name,
                        "project_no": project_no
                    })

            except Exception as e:
                pdf_set(con, pdf_key, lb_key, "ERROR", None, note=str(e))
                print(f"            ⚠ PDF error for proj {project_no}: {e}")

            # Go back to LB project list reliably
            try:
                client.postback(lb_target, lb_arg)
            except Exception:
                # if LB list fails, just keep going; next context restore will fix
                pass

        # pager next (handles "...")
        pages = pager["pages"]
        ellipsis = pager["ellipsis"]
        want = (current_page + 1) if current_page is not None else (page_id + 1)

        next_pb = None
        if want in pages:
            next_pb = pages[want]
        elif ellipsis:
            next_pb = ellipsis[-1]

        if not next_pb:
            break

        nt, na = next_pb
        print("          -> Next page...")
        client.postback(nt, na)

# ================= MASTER =================

def crawl_pdfs(limit_years=None, limit_lbtypes=None, limit_districts=None, limit_lbs_per_district=None):
    con = db_connect()
    client = SulekhaClient(delay=DELAY_BETWEEN_REQUESTS)

    bm = load_bookmark()
    bm_state = {"bookmark": bm, "active": bool(bm)}
    print("🔖 Loaded bookmark:", bookmark_repr(bm))

    client.load_base()
    years = [(v, t) for (v, t) in client.get_year_options() if year_allowed(t)]
    lbtypes = client.get_lbtype_options()

    print("Years (filtered):", years)
    print("LB Types:", lbtypes)

    for i, (year_val, year_label) in enumerate(years, start=1):
        if limit_years and i > limit_years:
            break

        print("\n==============================")
        print(f"=== YEAR {year_label} ({year_val}) ===")
        print("==============================")

        try:
            client.postback("drpYear", "", updates={"drpYear": year_val})
        except Exception as e:
            print(f"⚠ Failed to set year {year_label}: {e} -> reload base and retry once")
            client.load_base()
            client.postback("drpYear", "", updates={"drpYear": year_val})

        for j, (lb_val, lb_label) in enumerate(lbtypes, start=1):
            if limit_lbtypes and j > limit_lbtypes:
                break

            # bookmark skip LB types
            if bm_state["active"] and str(year_val) == str(bm.get("year_val")):
                if str(lb_val) != str(bm.get("lb_val")):
                    continue

            print(f"\n  -- LB Type {lb_label} ({lb_val}) --")

            try:
                client.postback("drpType", "", updates={"drpType": lb_val})
            except Exception as e:
                print(f"⚠ Failed to set LB type {lb_label}: {e} -> reload base/year and retry once")
                client.load_base()
                client.postback("drpYear", "", updates={"drpYear": year_val})
                client.postback("drpType", "", updates={"drpType": lb_val})

            districts = parse_district_rows(client.soup)
            print(f"    Found {len(districts)} districts")

            for dpos, district in enumerate(districts, start=1):
                if limit_districts and dpos > limit_districts:
                    break

                # bookmark skip districts
                if bm_state["active"] and str(year_val) == str(bm.get("year_val")) and str(lb_val) == str(bm.get("lb_val")):
                    if int(district["index"]) != int(bm.get("district_idx")):
                        continue

                d_name = district["district_name"]
                print(f"\n    ▶ District {district['index']}: {d_name}")

                d_target, d_arg = district["postback"]
                try:
                    client.postback(d_target, d_arg)
                except Exception as e:
                    print(f"      ⚠ Failed to enter district {d_name}: {e} -> reset and continue")
                    try:
                        safe_reset_to_context(client, year_val, lb_val, district["postback"])
                    except Exception:
                        pass
                    continue

                lb_rows = parse_localbody_rows(client.soup)
                print(f"      Found {len(lb_rows)} LOCALBODY rows")

                for lbk, lb in enumerate(lb_rows, start=1):
                    if limit_lbs_per_district and lbk > limit_lbs_per_district:
                        break

                    # bookmark skip LBs
                    if bm_state["active"] and str(year_val) == str(bm.get("year_val")) and str(lb_val) == str(bm.get("lb_val")) and int(district["index"]) == int(bm.get("district_idx")):
                        if int(lb["index"]) != int(bm.get("lb_idx")):
                            continue

                    lb_name = lb["lb_name"]
                    print(f"      → LOCALBODY {lb['index']}: {lb_name}")

                    # Restore context before each LB
                    try:
                        client.postback("drpYear", "", updates={"drpYear": year_val})
                        client.postback("drpType", "", updates={"drpType": lb_val})
                        client.postback(d_target, d_arg)
                    except Exception as e:
                        print(f"        ⚠ Failed to restore context: {e} -> hard reset and continue")
                        try:
                            safe_reset_to_context(client, year_val, lb_val, district["postback"])
                        except Exception:
                            pass
                        continue

                    lb_rows_again = parse_localbody_rows(client.soup)
                    lb_again = next((x for x in lb_rows_again if x["index"] == lb["index"]), None)
                    if not lb_again:
                        print(f"        ⚠ Could not re-find LB index {lb['index']}. Skipping.")
                        continue

                    try:
                        scrape_lb_pdfs(
                            con, client,
                            year_val, year_label,
                            lb_val, lb_label,
                            district, lb_again,
                            bm_state
                        )
                    except Exception as e:
                        print(f"        ❌ LB PDF scrape failed '{lb_name}': {e}")
                        print("        ↻ Resetting context and continuing...")
                        try:
                            safe_reset_to_context(client, year_val, lb_val, district["postback"])
                        except Exception:
                            pass
                        continue

    con.close()
    print("✅ PDF crawl complete.")

if __name__ == "__main__":
    crawl_pdfs(
        limit_years=None,
        limit_lbtypes=None,
        limit_districts=None,
        limit_lbs_per_district=None,
    )
