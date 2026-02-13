import csv
import os
import re
import time
from math import ceil
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ----------------------------------------------------------
# Configuration
# ----------------------------------------------------------

BASE_URL = "https://plan.lsgkerala.gov.in/formulation/Public.aspx"

# Per-LOCALBODY CSVs will be written here
OUT_DIR = "sulekha_tables"
os.makedirs(OUT_DIR, exist_ok=True)

# polite delay between requests (seconds)
REQUEST_DELAY = 1.5

# Optional filters (set to None or empty set to do all)
YEARS_TO_DO = None  # e.g. {"2025-2026"}
LBTYPES_TO_DO = None  # e.g. {"Grama Panchayat"}

# Expected projects per page (from the site layout)
PROJECTS_PER_PAGE_DEFAULT = 20

# Regex to parse __doPostBack calls
POSTBACK_RE = re.compile(
    r"__doPostBack\('(?P<target>[^']*)','(?P<argument>[^']*)'\)"
)


# ----------------------------------------------------------
# Helper functions
# ----------------------------------------------------------

def slugify(value: str) -> str:
    """
    Make a filesystem-safe slug from arbitrary text.
    Example: 'Ajanoor Grama Panchayat' -> 'ajanoor_grama_panchayat'
    """
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = re.sub(r"_+", "_", value)
    return value.strip("_")


def lb_csv_path(year_label, lbtype_label, district_name, lb_name) -> str:
    """
    Build a per-LOCALBODY CSV path.
    """
    fname = f"{year_label}_{lbtype_label}_{district_name}_{lb_name}"
    fname = slugify(fname) + ".csv"
    return os.path.join(OUT_DIR, fname)


def safe_int(text, default=None):
    text = (text or "").strip().replace(",", "")
    if not text:
        return default
    try:
        return int(text)
    except ValueError:
        return default


def extract_postback_from_link(tag):
    """
    Given an <a> or <span> with href/onclick containing __doPostBack,
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


def is_lb_complete(csv_path, expected_projects):
    """
    Check if the LOCALBODY CSV already contains the expected number of projects.
    """
    if not expected_projects:
        return False
    if not os.path.exists(csv_path):
        return False

    with open(csv_path, "r", encoding="utf-8-sig") as f:
        row_count = sum(1 for _ in f) - 1  # minus header
    return row_count >= expected_projects


# ----------------------------------------------------------
# Client to handle ASP.NET WebForms behaviour
# ----------------------------------------------------------

class SulekhaClient:
    def __init__(self, delay=REQUEST_DELAY):
        self.s = requests.Session()
        self.delay = delay
        self.form_data = {}
        self.soup = None

    def _sleep(self):
        time.sleep(self.delay)

    def _request(self, method, url, **kwargs):
        """
        Small wrapper with basic retry logic.
        """
        for attempt in range(3):
            try:
                self._sleep()
                r = self.s.request(method, url, timeout=60, **kwargs)
                r.raise_for_status()
                return r
            except requests.RequestException as e:
                print(f"[HTTP] Error (attempt {attempt + 1}/3): {e}")
                if attempt == 2:
                    raise
                time.sleep(self.delay * 2)

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
        print("Loading base page...")
        r = self._request("GET", BASE_URL)
        self._parse_form(r.text)

    def postback(self, event_target, event_argument="", updates=None):
        """
        Imitate __doPostBack:
        - updates: dict of form fields to override (e.g. {'drpYear': '29'})
        - returns BeautifulSoup of new page.
        """
        data = self.form_data.copy()
        if updates:
            data.update(updates)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = event_argument

        r = self._request("POST", BASE_URL, data=data)
        self._parse_form(r.text)
        return self.soup

    # ---------- Dropdown helpers ----------

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


# ----------------------------------------------------------
# Parsing functions for each level
# ----------------------------------------------------------

def find_district_rows(soup):
    """
    Parse the 'DPC Approved Details' district table (id='gvState').

    Returns list of dicts:
    {
        'district_name': ...,
        'num_lbs': int or None,
        'total_projects': int or None,
        'postback': (target, argument)
    }
    """
    table = soup.find("table", {"id": "gvState"})
    rows_out = []
    if not table:
        return rows_out

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 9:
            continue

        # Skip total row (DISTRICT cell = 'Total')
        district_text = cells[1].get_text(strip=True)
        if district_text.upper() == "TOTAL":
            continue

        # Details link in last cell
        details_cell = cells[-1]
        link = details_cell.find("a")
        pb = extract_postback_from_link(link)
        if not pb:
            continue

        num_lbs = safe_int(cells[2].get_text(strip=True))
        total_projects = safe_int(cells[3].get_text(strip=True))

        rows_out.append({
            "district_name": district_text,
            "num_lbs": num_lbs,
            "total_projects": total_projects,
            "postback": pb,
        })

    return rows_out


def find_localbody_rows(soup):
    """
    Parse the LOCALBODY table (id='gvStat') for a district.

    Returns list of dicts:
    {
        'lb_name': ...,
        'num_projects': int or None,
        'postback': (target, argument)
    }
    """
    table = soup.find("table", {"id": "gvStat"})
    rows_out = []
    if not table:
        return rows_out

    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        lb_name = cells[1].get_text(strip=True)
        if lb_name.upper() == "TOTAL":
            continue

        details_cell = cells[-1]
        link = details_cell.find("a")
        pb = extract_postback_from_link(link)
        if not pb:
            continue

        num_projects = safe_int(cells[2].get_text(strip=True))

        rows_out.append({
            "lb_name": lb_name,
            "num_projects": num_projects,
            "postback": pb,
        })

    return rows_out


def find_project_rows(soup):
    """
    Parse the project table (id='gvProjects') for a LOCALBODY.

    Returns list of dicts:
    {
        'project_name': ...,
        'formulation': ...,
        'expense': ...
    }

    Pagination row (containing page numbers 1 2 3 ... etc.)
    is explicitly skipped to avoid bogus rows like '1,2,3'.
    """
    table = soup.find("table", {"id": "gvProjects"})
    projects = []
    if not table:
        return projects

    for row in table.find_all("tr"):
        # Skip header row (has <th>)
        if row.find("th"):
            continue

        # Skip pagination row: it has nested table with page links
        if row.find("a", href=re.compile(r"Page\$\d+")) or row.find("span"):
            # This is the row with page numbers (1 2 3 ...).
            # We don't want to treat it as a project row.
            continue

        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        # Layout: [No., Project Name, Formulation, Expense, DETAILS]
        project_name = cells[1].get_text(strip=True)
        formulation = cells[2].get_text(strip=True)
        expense = cells[3].get_text(strip=True)

        projects.append({
            "project_name": project_name,
            "formulation": formulation,
            "expense": expense,
        })

    return projects


# ----------------------------------------------------------
# Master crawling loop (tables only)
# ----------------------------------------------------------

def crawl_tables():
    client = SulekhaClient()
    client.load_base()

    years = client.get_year_options()
    lbtypes = client.get_lbtype_options()

    print("Years:", years)
    print("LB Types:", lbtypes)

    for year_val, year_label in years:
        if YEARS_TO_DO and year_label not in YEARS_TO_DO:
            continue

        print(f"\n=== Year {year_label} ({year_val}) ===")

        # Set year
        client.postback("drpYear", "", updates={"drpYear": year_val})

        for lb_val, lb_label in lbtypes:
            if LBTYPES_TO_DO and lb_label not in LBTYPES_TO_DO:
                continue

            print(f"\n  -- LB Type {lb_label} ({lb_val}) --")

            # Set LB type
            client.postback("drpType", "", updates={"drpType": lb_val})

            # District-level table
            district_rows = find_district_rows(client.soup)
            print(f"    Found {len(district_rows)} districts")

            for d in district_rows:
                d_name = d["district_name"]
                d_num_lbs = d.get("num_lbs")
                d_total_projects = d.get("total_projects")
                d_target, d_arg = d["postback"]

                print(
                    f"    District: {d_name} – expected LBs: {d_num_lbs}, "
                    f"expected total projects: {d_total_projects}"
                )

                # Enter district (LOCALBODY list)
                client.postback(d_target, d_arg)

                lb_rows = find_localbody_rows(client.soup)
                print(
                    f"      Found {len(lb_rows)} local bodies "
                    f"(summary said: {d_num_lbs})"
                )

                # Iterate over all LOCALBODYs in this district
                for lb in lb_rows:
                    lb_name = lb["lb_name"]
                    lb_expected = lb.get("num_projects")
                    lb_target, lb_arg = lb["postback"]

                    csv_path = lb_csv_path(year_label, lb_label, d_name, lb_name)

                    # Resume logic: skip LB if already fully scraped
                    if lb_expected and is_lb_complete(csv_path, lb_expected):
                        print(
                            f"      LB: {lb_name} – already done "
                            f"({lb_expected}/{lb_expected} projects), skipping."
                        )
                        continue

                    # If there's an incomplete CSV, wipe and start fresh for this LB
                    if os.path.exists(csv_path):
                        print(
                            f"      LB: {lb_name} – existing CSV incomplete, "
                            f"re-scraping and overwriting."
                        )
                        os.remove(csv_path)

                    print(
                        f"      LB: {lb_name} – expected projects: {lb_expected}"
                    )

                    # Enter LOCALBODY (project list, page 1)
                    client.postback(lb_target, lb_arg)

                    # Prepare CSV for this LOCALBODY
                    with open(
                        csv_path, "w", newline="", encoding="utf-8-sig"
                    ) as f:
                        writer = csv.writer(f)
                        writer.writerow([
                            "year_value", "year_label",
                            "lbtype_value", "lbtype_label",
                            "district_name", "localbody_name",
                            "page_no", "project_no_on_page",
                            "project_name", "formulation", "expense",
                        ])

                        # Pagination over project pages
                        total_seen = 0
                        page_no = 0
                        page_size = None

                        while True:
                            page_no += 1
                            projects = find_project_rows(client.soup)

                            if not projects:
                                print(
                                    f"        Page {page_no}: 0 projects found – "
                                    f"stopping for this LB."
                                )
                                break

                            if page_size is None:
                                page_size = len(projects)

                            print(
                                f"        Page {page_no}: {len(projects)} projects found."
                            )

                            for k, proj in enumerate(projects, start=1):
                                writer.writerow([
                                    year_val, year_label,
                                    lb_val, lb_label,
                                    d_name, lb_name,
                                    page_no, k,
                                    proj["project_name"],
                                    proj["formulation"],
                                    proj["expense"],
                                ])
                            total_seen += len(projects)

                            # If we know how many projects we expect, stop when done
                            if lb_expected and total_seen >= lb_expected:
                                print(
                                    f"        Reached expected total "
                                    f"{total_seen}/{lb_expected} projects for this LB."
                                )
                                break

                            # Otherwise, try next page via Page$N postback
                            next_page_index = page_no + 1

                            # Basic safety: if page_size known, we can cap page count
                            if lb_expected and page_size:
                                max_pages = ceil(lb_expected / page_size)
                                if next_page_index > max_pages:
                                    print(
                                        f"        Next page index {next_page_index} "
                                        f"> max_pages {max_pages}, stopping."
                                    )
                                    break

                            print(f"        Moving to page {next_page_index}...")
                            try:
                                client.postback("gvProjects", f"Page${next_page_index}")
                            except requests.RequestException as e:
                                print(
                                    f"        Error while moving to page {next_page_index}: "
                                    f"{e}. Stopping this LB."
                                )
                                break

                    # After finishing this LOCALBODY, go back to district LB list
                    # by re-triggering the district's postback.
                    client.postback(d_target, d_arg)

                # after all LOCALBODYs of this district, we are still at LB list;
                # outer loop will just move to next district by new d_target/d_arg


if __name__ == "__main__":
    # You can safely stop this script with Ctrl+C.
    # When you re-run it, it will skip LOCALBODYs whose CSV already has
    # the expected number of projects (from NO.OF PROJECTS in the summary).
    crawl_tables()
