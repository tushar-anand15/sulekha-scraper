import os
import re
import time
import random
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ================= CONFIG =================

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
os.makedirs(OUT_DIR, exist_ok=True)

# ONE single PDF dump folder (no subfolders)
PDF_DIR = os.path.join(OUT_DIR, "sulekha_pdfs")
os.makedirs(PDF_DIR, exist_ok=True)

DB_PATH = os.path.join(OUT_DIR, "sulekha_progress.sqlite")

REQUEST_TIMEOUT = 60
DELAY_BETWEEN_REQUESTS = 1.0

MAX_RETRIES = 8
BACKOFF_BASE = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 180.0

# Optional: if Select$N returns HTML but no pdf found, dump html for inspection
DEBUG_DUMP_FAILED_HTML = False
DEBUG_HTML_DIR = os.path.join(OUT_DIR, "_pdf_debug_html")
if DEBUG_DUMP_FAILED_HTML:
    os.makedirs(DEBUG_HTML_DIR, exist_ok=True)

print("🔹 Running sulekha_pdf_scraper_v2.5.1.py")
print("🔹 OUT_DIR:", os.path.abspath(OUT_DIR))
print("🔹 PDF_DIR:", os.path.abspath(PDF_DIR))
print("🔹 Progress DB:", os.path.abspath(DB_PATH))

POSTBACK_RE = re.compile(r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)")
PDF_URL_RE = re.compile(r"""(?P<url>(https?://|/)[^'"\s>]+?\.pdf[^'"\s>]*)""", re.IGNORECASE)

# ================= SQLITE =================

def db_connect():
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-20000;")  # ~20MB

    # LB-level PDF counter
    con.execute("""
        CREATE TABLE IF NOT EXISTS lb_pdf_state (
            lb_key TEXT PRIMARY KEY,
            next_n INTEGER DEFAULT 1,
            updated_at TEXT
        )
    """)

    # Per-project PDF progress
    con.execute("""
        CREATE TABLE IF NOT EXISTS pdf_progress (
            key TEXT PRIMARY KEY,
            lb_key TEXT NOT NULL,
            pdf_path TEXT,
            status TEXT DEFAULT 'PENDING',
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

def pdf_set(con, pdf_key, lb_key, status, pdf_path=None):
    con.execute(
        "INSERT OR REPLACE INTO pdf_progress(key, lb_key, pdf_path, status, updated_at) "
        "VALUES (?,?,?,?,?)",
        (pdf_key, lb_key, pdf_path, status, now_utc()),
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
            raise RuntimeError("Could not find form id='form1' (not a normal WebForms page).")

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

                # retry 5xx
                if 500 <= resp.status_code < 600:
                    raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)

                resp.raise_for_status()
                return resp

            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                last_exc = e
                backoff = min(BACKOFF_START * (BACKOFF_BASE ** (attempt - 1)), BACKOFF_MAX)
                backoff = backoff * (0.7 + random.random() * 0.6)  # jitter

                code = ""
                if isinstance(e, requests.HTTPError) and getattr(e, "response", None) is not None:
                    code = f"(status={e.response.status_code})"

                print(f"    ⚠ Network/server error {code} attempt {attempt}/{MAX_RETRIES}: {e}")
                print(f"    ↻ Backing off for {backoff:.1f}s")
                time.sleep(backoff)

                if attempt in {3, 6}:
                    print("    🔄 Recreating HTTP session...")
                    self._new_session()

        raise last_exc

    def load_base(self):
        print("▶ Loading base page:", BASE_URL)
        r = self._request_with_retries("GET", BASE_URL)
        self._parse_form(r.text)
        print("✅ Base page loaded.")

    def postback(self, event_target, event_argument="", updates=None):
        """
        Standard postback that MUST return a normal WebForms page.
        """
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        r = self._request_with_retries("POST", BASE_URL, data=data)
        self._parse_form(r.text)
        return self.soup

    def postback_raw(self, event_target, event_argument="", updates=None):
        """
        Postback that may return a PDF/redirect/handler output. DOES NOT parse form.
        Returns requests.Response.
        """
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        r = self._request_with_retries("POST", BASE_URL, data=data, allow_redirects=True)
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

        out.append({
            "index": idx,
            "district_name": district_name,
            "postback": pb,
        })
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

        out.append({
            "index": idx,
            "lb_name": lb_name,
            "postback": pb,
        })
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

        projects.append({
            "project_no": project_no,
            "project_name": project_name,
            "select_postback": select_pb,
        })

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

def extract_pdf_url_from_html(html):
    m = PDF_URL_RE.search(html or "")
    if not m:
        return None
    return m.group("url")

def is_pdf_response(resp: requests.Response):
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ctype:
        return True
    # sometimes servers send octet-stream
    head = resp.content[:5] if resp.content else b""
    if head.startswith(b"%PDF"):
        return True
    return False

# ================= PDF DOWNLOAD =================

def save_pdf_bytes(out_path, content: bytes):
    with open(out_path, "wb") as f:
        f.write(content)

def download_pdf_by_url(client, pdf_url, out_path):
    full_url = urljoin(BASE_URL, pdf_url)
    r = client._request_with_retries("GET", full_url)
    save_pdf_bytes(out_path, r.content)

# ================= LOCALBODY PDF SCRAPER =================

def scrape_lb_pdfs(con, client,
                   year_val, year_label,
                   lb_val, lb_label,
                   district, lb):
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

    # enter LB project list
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

            pdf_key = make_pdf_key(year_val, lb_val, d_idx, lb_idx, project_no)
            st = pdf_get_status(con, pdf_key)
            if st and st["status"] == "DONE" and st["pdf_path"] and os.path.exists(st["pdf_path"]):
                continue

            if not select_pb:
                pdf_set(con, pdf_key, lb_key, "NO_SELECT", None)
                continue

            try:
                t, a = select_pb

                # IMPORTANT: Select postback may return PDF/redirect/handler output -> use raw
                resp = client.postback_raw(t, a)

                # Case A: response IS the PDF
                if is_pdf_response(resp):
                    n = lb_advance_n(con, lb_key)
                    filename = make_pdf_filename(meta, n, project_no)
                    out_path = os.path.join(PDF_DIR, filename)

                    if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
                        save_pdf_bytes(out_path, resp.content)

                    pdf_set(con, pdf_key, lb_key, "DONE", out_path)
                    print(f"            ✅ PDF proj {project_no} -> {os.path.basename(out_path)}")

                else:
                    # Case B: HTML/other -> try to extract a PDF url and download
                    html = resp.text if hasattr(resp, "text") else ""
                    pdf_url = extract_pdf_url_from_html(html)

                    if not pdf_url:
                        if DEBUG_DUMP_FAILED_HTML:
                            fn = f"FAILED__{slugify(lb_name,40)}__proj{slugify(str(project_no),12)}.html"
                            with open(os.path.join(DEBUG_HTML_DIR, fn), "w", encoding="utf-8") as f:
                                f.write(html)
                        pdf_set(con, pdf_key, lb_key, "NO_PDF_URL", None)
                        print(f"            ⚠ No PDF URL found for proj {project_no}")
                    else:
                        n = lb_advance_n(con, lb_key)
                        filename = make_pdf_filename(meta, n, project_no)
                        out_path = os.path.join(PDF_DIR, filename)

                        if not (os.path.exists(out_path) and os.path.getsize(out_path) > 0):
                            download_pdf_by_url(client, pdf_url, out_path)

                        pdf_set(con, pdf_key, lb_key, "DONE", out_path)
                        print(f"            ✅ PDF proj {project_no} -> {os.path.basename(out_path)}")

            except Exception as e:
                print(f"            ⚠ PDF error for proj {project_no}: {e}")
                pdf_set(con, pdf_key, lb_key, "ERROR", None)

            # After Select, the server state may have changed.
            # Re-enter LB project list cleanly to keep pagination stable.
            try:
                client.postback(lb_target, lb_arg)
            except Exception:
                # caller will reset context as needed
                pass

        # next page logic (handles "...")
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

# ================= MASTER CRAWLER =================

def crawl_pdfs(limit_years=None, limit_lbtypes=None, limit_districts=None, limit_lbs_per_district=None):
    con = db_connect()
    client = SulekhaClient(delay=DELAY_BETWEEN_REQUESTS)

    client.load_base()
    years = client.get_year_options()
    lbtypes = client.get_lbtype_options()

    print("Years:", years)
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

                    lb_name = lb["lb_name"]
                    print(f"      → LOCALBODY {lb['index']}: {lb_name}")

                    # Restore clean context before each LB
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
                            district, lb_again
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

# ================= RUN =================

if __name__ == "__main__":
    crawl_pdfs(
        limit_years=None,
        limit_lbtypes=None,
        limit_districts=None,
        limit_lbs_per_district=None,
    )
