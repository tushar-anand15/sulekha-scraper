import csv
import os
import re
import time
import random
import sqlite3
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ----------------- CONFIG -----------------

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
os.makedirs(OUT_DIR, exist_ok=True)

DB_PATH = os.path.join(OUT_DIR, "sulekha_progress.sqlite")

REQUEST_TIMEOUT = 60
DELAY_BETWEEN_REQUESTS = 1.2

MAX_RETRIES = 8
BACKOFF_BASE = 2.0
BACKOFF_START = 2.0
BACKOFF_MAX = 180.0

DB_COMMIT_EVERY = 50   # commit progress updates in batches (speed)

print("🔹 Running sulekha_table_scraper_v3.1.py")
print("🔹 OUT_DIR:", os.path.abspath(OUT_DIR))
print("🔹 Progress DB:", os.path.abspath(DB_PATH))

POSTBACK_RE = re.compile(
    r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)"
)

# ----------------- SQLITE PROGRESS -----------------

def db_connect():
    con = sqlite3.connect(DB_PATH)
    # speed + resilience
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA temp_store=MEMORY;")
    con.execute("PRAGMA cache_size=-20000;")  # ~20MB cache

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

def db_get(con, key):
    cur = con.execute(
        "SELECT expected, scraped, status, csv_path FROM progress WHERE key=?",
        (key,)
    )
    row = cur.fetchone()
    if not row:
        return None
    return {"expected": row[0], "scraped": row[1], "status": row[2], "csv_path": row[3]}

def db_upsert_no_commit(con, key, csv_path, expected=None, scraped=None, status=None):
    """
    Upsert WITHOUT committing. Caller commits in batches.
    """
    now = datetime.now(timezone.utc).isoformat()
    existing = db_get(con, key)

    if existing is None:
        con.execute(
            "INSERT OR REPLACE INTO progress(key, csv_path, expected, scraped, status, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (
                key,
                csv_path,
                expected if expected is not None else None,
                scraped if scraped is not None else 0,
                status if status is not None else "PARTIAL",
                now,
            ),
        )
    else:
        new_expected = expected if expected is not None else existing["expected"]
        new_scraped = scraped if scraped is not None else existing["scraped"]
        new_status = status if status is not None else existing["status"]
        new_path = csv_path or existing["csv_path"]
        con.execute(
            "UPDATE progress SET csv_path=?, expected=?, scraped=?, status=?, updated_at=? WHERE key=?",
            (new_path, new_expected, new_scraped, new_status, now, key),
        )

# ----------------- CLIENT -----------------

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
        years = []
        if not sel:
            return years
        for opt in sel.find_all("option"):
            val = opt.get("value")
            text = opt.get_text(strip=True)
            if val and val != "0":
                years.append((val, text))
        return years

    def get_lbtype_options(self):
        sel = self.soup.find("select", {"id": "drpType"})
        types_ = []
        if not sel:
            return types_
        for opt in sel.find_all("option"):
            val = opt.get("value")
            text = opt.get_text(strip=True)
            if val and val != "0":
                types_.append((val, text))
        return types_

# ----------------- HELPERS -----------------

def slugify(s, max_len=80):
    s = s.strip().replace(" ", "_")
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = re.sub(r"\s+", "_", s)
    if not s:
        s = "name"
    return s[:max_len]

def get_lb_csv_path(year_val, lb_val, district_index, district_name, lb_index, lb_name):
    dist_slug = slugify(f"{district_index}_{district_name}")
    lb_slug = slugify(f"{lb_index}_{lb_name}")
    filename = f"y{year_val}__lb{lb_val}__{dist_slug}__{lb_slug}.csv"
    return os.path.join(OUT_DIR, filename)

# ----------------- PARSERS -----------------

def parse_district_rows(soup):
    gv = soup.find("table", {"id": "gvState"})
    rows_out = []
    if not gv:
        return rows_out

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

        rows_out.append({
            "index": idx,
            "district_name": district_name,
            "no_of_lbs": no_of_lbs,
            "no_of_projects": no_of_projects,
            "postback": pb,
        })
    return rows_out

def parse_localbody_rows(soup):
    gv = soup.find("table", {"id": "gvStat"})
    rows_out = []
    if not gv:
        return rows_out

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
        try:
            no_of_projects = int(tds[2].get_text(strip=True))
        except ValueError:
            no_of_projects = None

        link = tds[-1].find("a")
        pb = SulekhaClient.extract_postback_from_link(link)
        if not pb:
            continue

        rows_out.append({
            "index": idx,
            "lb_name": lb_name,
            "no_of_projects": no_of_projects,
            "postback": pb,
        })
    return rows_out

def parse_projects_and_pager_full(soup):
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

    # data rows
    for tr in rows[1:]:
        if tr is pager_tr:
            continue
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 5:
            continue

        project_no = tds[0].get_text(strip=True)
        project_name = tds[1].get_text(strip=True)
        formulation = tds[2].get_text(strip=True)
        expense = tds[3].get_text(strip=True)

        if project_name and re.fullmatch(r"[0-9,\s]+", project_name):
            continue

        projects.append({
            "project_no": project_no,
            "project_name": project_name,
            "formulation": formulation,
            "expense": expense,
        })

    # pager
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

# ----------------- CSV IO -----------------

def write_csv_header_if_needed(csv_path):
    if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
        return
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "year_value","year_label","lbtype_value","lbtype_label",
            "district_index","district_name","localbody_index","localbody_name",
            "expected_projects","project_page","project_no","project_name",
            "formulation","expense"
        ])

def append_rows(csv_path, rows):
    if not rows:
        return
    with open(csv_path, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)

# ----------------- CONTEXT RESET -----------------

def safe_reset_to_context(client, year_val, lb_val, district_postback):
    client.load_base()
    client.postback("drpYear", "", updates={"drpYear": year_val})
    client.postback("drpType", "", updates={"drpType": lb_val})
    dt, da = district_postback
    client.postback(dt, da)

# ----------------- LOCALBODY SCRAPER -----------------

def scrape_localbody_projects_incremental(con, client,
                                         year_val, year_label,
                                         lb_val, lb_label,
                                         district, lb):
    d_idx = district["index"]
    d_name = district["district_name"]
    lb_idx = lb["index"]
    lb_name = lb["lb_name"]
    expected = lb["no_of_projects"]

    key = make_key(year_val, lb_val, d_idx, lb_idx)
    csv_path = get_lb_csv_path(year_val, lb_val, d_idx, d_name, lb_idx, lb_name)

    # record path/expected
    db_upsert_no_commit(con, key, csv_path, expected=expected)

    # ALWAYS restart this LB if not DONE (simple + robust)
    if os.path.exists(csv_path):
        try:
            os.remove(csv_path)
        except OSError:
            pass
    write_csv_header_if_needed(csv_path)

    rows_written = 0
    db_upsert_no_commit(con, key, csv_path, scraped=0, status="PARTIAL")

    print(f"        ▶ Scraping LB '{lb_name}' (expected {expected})")
    lb_target, lb_arg = lb["postback"]
    client.postback(lb_target, lb_arg)

    visited_pages = set()
    page_fallback = 0

    while True:
        page_fallback += 1
        projects, pager = parse_projects_and_pager_full(client.soup)
        current_page = pager["current_page"]
        page_id = current_page if current_page is not None else page_fallback

        if page_id in visited_pages:
            print(f"          ⚠ Page loop at {page_id}. Stopping LB.")
            break
        visited_pages.add(page_id)

        print(f"          Page {page_id}: {len(projects)} rows")

        out_rows = []
        for p in projects:
            out_rows.append([
                year_val, year_label, lb_val, lb_label,
                d_idx, d_name, lb_idx, lb_name,
                expected, page_id,
                p["project_no"], p["project_name"], p["formulation"], p["expense"]
            ])

        append_rows(csv_path, out_rows)
        rows_written += len(out_rows)
        db_upsert_no_commit(con, key, csv_path, scraped=rows_written, status="PARTIAL")

        if expected is not None and expected > 0 and rows_written >= expected:
            print(f"          ✅ Reached expected {rows_written}/{expected}.")
            break

        pages = pager["pages"]
        ellipsis = pager["ellipsis"]

        want = (current_page + 1) if current_page is not None else (page_id + 1)

        next_pb = None
        if want in pages:
            next_pb = pages[want]
        elif ellipsis:
            # advance the window of pages
            next_pb = ellipsis[-1]

        if not next_pb:
            break

        nt, na = next_pb
        print("          -> Next...")
        client.postback(nt, na)

    final_status = "PARTIAL"
    if expected is not None and expected > 0 and rows_written >= expected:
        final_status = "DONE"

    db_upsert_no_commit(con, key, csv_path, scraped=rows_written, status=final_status)
    print(f"        💾 CSV: {csv_path} ({rows_written} rows, status={final_status})")
    return rows_written, final_status

# ----------------- MASTER CRAWLER -----------------

def crawl_tables(limit_years=None, limit_lbtypes=None, limit_districts=None, limit_lbs_per_district=None):
    con = db_connect()
    client = SulekhaClient(delay=DELAY_BETWEEN_REQUESTS)
    client.load_base()

    years = client.get_year_options()
    lbtypes = client.get_lbtype_options()

    print("Years:", years)
    print("LB Types:", lbtypes)
    print("📂 CSVs written to:", os.path.abspath(OUT_DIR))

    db_ops = 0

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
                print(f"\n    ▶ District {district['index']}: {d_name} (LBs: {district['no_of_lbs']}, Projects: {district['no_of_projects']})")

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

                # ✅ BIG SPEED WIN:
                # Decide DONE/PARTIAL purely from DB *before* doing any restore-context work.
                for lbk, lb in enumerate(lb_rows, start=1):
                    if limit_lbs_per_district and lbk > limit_lbs_per_district:
                        break

                    lb_idx = lb["index"]
                    lb_name = lb["lb_name"]
                    expected = lb["no_of_projects"]
                    key = make_key(year_val, lb_val, district["index"], lb_idx)

                    state = db_get(con, key)
                    if state and state["status"] == "DONE" and state["expected"] and state["scraped"] >= state["expected"]:
                        print(f"      → LOCALBODY {lb_idx}: {lb_name} (Projects: {expected})")
                        print(f"        🟡 Skipping DONE LB '{lb_name}' ({state['scraped']}/{state['expected']})")
                        continue

                    # Only now do expensive restore-context + re-find LB
                    print(f"      → LOCALBODY {lb_idx}: {lb_name} (Projects: {expected})")

                    try:
                        client.postback("drpYear", "", updates={"drpYear": year_val})
                        client.postback("drpType", "", updates={"drpType": lb_val})
                        client.postback(d_target, d_arg)
                    except Exception as e:
                        print(f"        ⚠ Failed to restore context: {e} -> reset and continue")
                        try:
                            safe_reset_to_context(client, year_val, lb_val, district["postback"])
                        except Exception:
                            pass
                        continue

                    lb_rows_again = parse_localbody_rows(client.soup)
                    lb_again = next((x for x in lb_rows_again if x["index"] == lb_idx), None)
                    if not lb_again:
                        print(f"        ⚠ Could not re-find LB index {lb_idx}. Skipping.")
                        continue

                    try:
                        _, _ = scrape_localbody_projects_incremental(
                            con, client,
                            year_val, year_label,
                            lb_val, lb_label,
                            district, lb_again
                        )
                        db_ops += 1
                        if db_ops % DB_COMMIT_EVERY == 0:
                            con.commit()
                            print(f"        ✅ Committed progress batch ({db_ops})")

                    except Exception as e:
                        print(f"        ❌ Error scraping LB '{lb_name}': {e}")
                        print("        ↻ Resetting context and continuing...")
                        try:
                            safe_reset_to_context(client, year_val, lb_val, district["postback"])
                        except Exception:
                            pass
                        continue

    con.commit()
    con.close()
    print("✅ Done.")

if __name__ == "__main__":
    crawl_tables(
        limit_years=None,
        limit_lbtypes=None,
        limit_districts=None,
        limit_lbs_per_district=None,
    )
