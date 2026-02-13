import csv
import os
import re
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ----------------- CONFIG -----------------

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

# 🔴 Change this if you want a different location
OUT_DIR = r"C:\Users\csabi\Downloads\Sulekha_Data\sulekha_tables"
os.makedirs(OUT_DIR, exist_ok=True)

print("🔹 Running sulekha_table_scraper.py")
print("🔹 Current working directory:", os.getcwd())
print("🔹 OUT_DIR (CSV output folder):", os.path.abspath(OUT_DIR))

POSTBACK_RE = re.compile(
    r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)"
)


# ----------------- CLIENT -----------------


class SulekhaClient:
    def __init__(self, delay=1.5):
        self.s = requests.Session()
        self.delay = delay
        self.form_data = {}
        self.soup = None

        self.s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
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
                data[name] = first["value"] if first else ""
            else:
                data[name] = selected["value"]

        self.form_data = data
        self.soup = soup

    def load_base(self):
        print("▶ Loading base page:", BASE_URL)
        r = self.s.get(BASE_URL, timeout=60)
        r.raise_for_status()
        self._parse_form(r.text)
        print("✅ Base page loaded.")

    def postback(self, event_target, event_argument="", updates=None):
        """
        Imitate __doPostBack.
        """
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        self._sleep()
        r = self.s.post(BASE_URL, data=data, timeout=60)
        r.raise_for_status()
        self._parse_form(r.text)
        return self.soup

    @staticmethod
    def extract_postback_from_link(tag):
        """
        Given an <a> (or similar) with href/onclick containing __doPostBack,
        return (event_target, event_argument) or None.
        """
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


def slugify(s, max_len=60):
    """
    Make a filename-safe slug from a string.
    """
    s = s.strip().replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9_\-]+", "", s)
    if not s:
        s = "name"
    if len(s) > max_len:
        s = s[:max_len]
    return s


# ----------------- PARSERS -----------------


def parse_district_rows(soup):
    """
    Parse 'DPC Approved Details' district table (gvState).
    Returns list:
    {
        "index": int,
        "district_name": str,
        "no_of_lbs": int,
        "no_of_projects": int,
        "postback": (target, arg)
    }
    """
    gv = soup.find("table", {"id": "gvState"})
    rows_out = []
    if not gv:
        return rows_out

    rows = gv.find_all("tr", recursive=False)
    # skip header & total row
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


def parse_localbody_rows(soup):
    """
    Parse LOCALBODY table (gvStat).
    Returns list:
    {
        "index": int,
        "lb_name": str,
        "no_of_projects": int,
        "postback": (target, arg)
    }
    """
    gv = soup.find("table", {"id": "gvStat"})
    rows_out = []
    if not gv:
        return rows_out

    rows = gv.find_all("tr", recursive=False)
    # skip header & total row
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

        rows_out.append(
            {
                "index": idx,
                "lb_name": lb_name,
                "no_of_projects": no_of_projects,
                "postback": pb,
            }
        )
    return rows_out


def parse_projects_and_pager(soup):
    """
    Parse project table (gvProjects) and pager.

    Returns:
      projects: list of dicts {
          "project_no", "project_name", "formulation", "expense"
      }
      pager_info: {
          "current_page": int or None,
          "next_postback": (target, arg) or None
      }
    """
    gv = soup.find("table", {"id": "gvProjects"})
    projects = []
    pager_info = {"current_page": None, "next_postback": None}

    if not gv:
        return projects, pager_info

    rows = gv.find_all("tr", recursive=False)
    if not rows:
        return projects, pager_info

    # Identify pager row (row containing nested <table>)
    pager_tr = None
    for tr in rows:
        if tr.find("table"):
            pager_tr = tr
            break

    # Data rows: skip header row, skip pager row
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

        # Skip bogus rows like "1,2,3"
        if project_name and re.fullmatch(r"[0-9,\s]+", project_name):
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
        # current page: <span> with a digit
        for span in pager_tr.find_all("span"):
            txt = span.get_text(strip=True)
            if txt.isdigit():
                current_page = int(txt)
                break

        # candidate links: <a> with __doPostBack('gvProjects','Page$N')
        candidates = []
        for a in pager_tr.find_all("a"):
            pb = SulekhaClient.extract_postback_from_link(a)
            if not pb:
                continue
            _t, arg = pb
            m = re.search(r"Page\$(\d+)", arg or "")
            if not m:
                continue
            page_num = int(m.group(1))
            candidates.append((page_num, pb))

        if candidates:
            if current_page is not None:
                bigger = [(n, pb) for (n, pb) in candidates if n > current_page]
                if bigger:
                    n_next, pb_next = min(bigger, key=lambda x: x[0])
                    next_postback = pb_next
            else:
                # no current_page span? then pick smallest candidate
                n_next, pb_next = min(candidates, key=lambda x: x[0])
                next_postback = pb_next

    pager_info["current_page"] = current_page
    pager_info["next_postback"] = next_postback
    return projects, pager_info


# ----------------- CSV & RESUME -----------------


def get_lb_csv_path(
    year_val,
    year_label,
    lb_val,
    lb_label,
    district_index,
    district_name,
    lb_index,
    lb_name,
):
    y = f"y{year_val}"
    t = f"lb{lb_val}"
    dist_slug = slugify(f"{district_index}_{district_name}")
    lb_slug = slugify(f"{lb_index}_{lb_name}")
    filename = f"{y}__{t}__{dist_slug}__{lb_slug}.csv"
    return os.path.join(OUT_DIR, filename)


def existing_project_count(csv_path):
    if not os.path.exists(csv_path):
        return 0
    try:
        with open(csv_path, "r", encoding="utf-8-sig", newline="") as f:
            n = sum(1 for _ in f) - 1  # minus header
            return max(n, 0)
    except Exception:
        return 0


# ----------------- LOCALBODY SCRAPER -----------------


def scrape_localbody_projects(
    client,
    year_val,
    year_label,
    lb_val,
    lb_label,
    district,
    lb,
):
    """
    For a given LOCALBODY, scrape *all* project pages into one CSV.
    Returns number of project rows scraped.
    """
    d_idx = district["index"]
    d_name = district["district_name"]
    lb_idx = lb["index"]
    lb_name = lb["lb_name"]
    expected = lb["no_of_projects"]

    csv_path = get_lb_csv_path(
        year_val,
        year_label,
        lb_val,
        lb_label,
        d_idx,
        d_name,
        lb_idx,
        lb_name,
    )

    # Resume: skip LB if we already have >= expected rows
    if expected is not None:
        existing = existing_project_count(csv_path)
        if existing >= expected and expected > 0:
            print(
                f"        🟡 Skipping LB '{lb_name}' – already has "
                f"{existing}/{expected} projects in CSV."
            )
            return existing

    print(f"        ▶ Scraping LB '{lb_name}' (expected {expected} projects)")

    # Enter LB's project list
    lb_target, lb_arg = lb["postback"]
    client.postback(lb_target, lb_arg)

    all_rows = []
    visited_pages = set()
    page_counter = 0

    while True:
        page_counter += 1
        projects, pager_info = parse_projects_and_pager(client.soup)
        current_page = pager_info["current_page"]

        # Fallback page number
        page_id = current_page if current_page is not None else page_counter

        if page_id in visited_pages:
            print(
                f"          ⚠ Page {page_id} already visited – "
                f"stopping to avoid pagination loop."
            )
            break
        visited_pages.add(page_id)

        print(
            f"          Page {page_id}: {len(projects)} project rows"
        )

        for p in projects:
            all_rows.append(
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
                    current_page,
                    p["project_no"],
                    p["project_name"],
                    p["formulation"],
                    p["expense"],
                ]
            )

        next_pb = pager_info["next_postback"]
        if not next_pb:
            break

        n_target, n_arg = next_pb
        print("          -> Moving to next project page...")
        client.postback(n_target, n_arg)

    # Write CSV (UTF-8 with BOM)
    if all_rows:
        os.makedirs(OUT_DIR, exist_ok=True)
        print(f"        💾 Writing CSV: {csv_path}")
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
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
            )
            writer.writerows(all_rows)

    scraped_count = len(all_rows)
    if expected is not None:
        print(
            f"        ✅ Finished LB '{lb_name}': scraped {scraped_count} rows "
            f"(expected {expected})"
        )
    else:
        print(f"        ✅ Finished LB '{lb_name}': scraped {scraped_count} rows")

    return scraped_count


# ----------------- MASTER CRAWLER -----------------


def crawl_tables(
    limit_years=None,
    limit_lbtypes=None,
    limit_districts=None,
    limit_lbs_per_district=None,
):
    client = SulekhaClient(delay=1.5)
    client.load_base()

    years = client.get_year_options()
    lbtypes = client.get_lbtype_options()

    print("Years:", years)
    print("LB Types:", lbtypes)
    print("📂 CSVs will be written to:", os.path.abspath(OUT_DIR))

    for i, (year_val, year_label) in enumerate(years, start=1):
        if limit_years and i > limit_years:
            break

        print("\n==============================")
        print(f"=== YEAR {year_label} ({year_val}) ===")
        print("==============================")

        client.postback("drpYear", "", updates={"drpYear": year_val})

        for j, (lb_val, lb_label) in enumerate(lbtypes, start=1):
            if limit_lbtypes and j > limit_lbtypes:
                break

            print(f"\n  -- LB Type {lb_label} ({lb_val}) --")
            client.postback("drpType", "", updates={"drpType": lb_val})

            districts = parse_district_rows(client.soup)
            print(f"    Found {len(districts)} districts for this Year+LBType")

            for d_idx0, district in enumerate(districts, start=1):
                if limit_districts and d_idx0 > limit_districts:
                    break

                d_name = district["district_name"]
                print(
                    f"\n    ▶ District {district['index']}: {d_name} "
                    f"(LBs: {district['no_of_lbs']}, Projects: {district['no_of_projects']})"
                )

                # Enter district-level LB list
                d_target, d_arg = district["postback"]
                client.postback(d_target, d_arg)

                lb_rows = parse_localbody_rows(client.soup)
                print(f"      Found {len(lb_rows)} LOCALBODY rows in this district")

                for lb_k, lb in enumerate(lb_rows, start=1):
                    if limit_lbs_per_district and lb_k > limit_lbs_per_district:
                        break

                    lb_name = lb["lb_name"]
                    print(
                        f"      → LOCALBODY {lb['index']}: {lb_name} "
                        f"(Projects: {lb['no_of_projects']})"
                    )

                    # Re-open Year + LBType + District + LB list before each LB scrape
                    client.postback("drpYear", "", updates={"drpYear": year_val})
                    client.postback("drpType", "", updates={"drpType": lb_val})
                    client.postback(d_target, d_arg)

                    lb_rows_again = parse_localbody_rows(client.soup)
                    lb_again = next(
                        (x for x in lb_rows_again if x["index"] == lb["index"]), None
                    )
                    if not lb_again:
                        print(
                            f"        ⚠ Could not find LB index {lb['index']} again, skipping."
                        )
                        continue

                    try:
                        scrape_localbody_projects(
                            client,
                            year_val,
                            year_label,
                            lb_val,
                            lb_label,
                            district,
                            lb_again,
                        )
                    except requests.HTTPError as e:
                        print(f"        ❌ HTTP error while scraping LB '{lb_name}': {e}")
                        print("        You can re-run the script later; it will resume.")
                    except Exception as e:
                        print(f"        ❌ Unexpected error for LB '{lb_name}': {e}")
                        print("        Continuing with next LB.")


if __name__ == "__main__":
    # 🔧 While testing, keep limits small to avoid hammering the site.
    crawl_tables(
        limit_years=None,           # set to None for all years
        limit_lbtypes=None,         # set to None for all LB types
        limit_districts=None,       # set to None for all districts
        limit_lbs_per_district=None # set to None for all LOCALBODYs
    )
