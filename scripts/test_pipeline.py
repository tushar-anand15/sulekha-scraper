#!/usr/bin/env python3
"""Test pipeline script for random sampling.

This script tests the pipeline by randomly sampling data at each level:
1. Ensures discovery is complete (runs if needed)
2. Randomly selects n districts
3. Discovers local bodies for those districts
4. Randomly selects n local bodies from each district
5. Scrapes projects for those local bodies
6. Randomly selects n projects from each local body
7. Downloads PDFs for those projects

Usage:
    uv run python scripts/test_pipeline.py
    uv run python scripts/test_pipeline.py --n 5
    uv run python scripts/test_pipeline.py --n 3 --skip-discovery
"""

import argparse
import os
import sys
import time
from datetime import datetime
from typing import Optional

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Load .env file if present (for local development)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Set defaults - these will be overridden by environment variables if set
# For Docker: environment variables are set in docker-compose.yml
# For local: set in .env file or use these defaults (S3/Minio)
os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha"
)
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("LOG_LEVEL", "INFO")
# Storage defaults - will use GCS if STORAGE_BACKEND=gcs is set in environment
os.environ.setdefault("STORAGE_BACKEND", "s3")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("S3_BUCKET_NAME", "sulekha-pdfs")


class TestPipelineStats:
    """Statistics for the test pipeline run."""

    def __init__(self):
        self.start_time = datetime.now()
        self.districts_sampled = 0
        self.local_bodies_discovered = 0
        self.local_bodies_sampled = 0
        self.projects_scraped = 0
        self.projects_sampled = 0
        self.pdfs_downloaded = 0
        self.pdfs_missing = 0
        self.pdfs_error = 0
        self.errors = []

    @property
    def duration(self) -> float:
        return (datetime.now() - self.start_time).total_seconds()

    def to_dict(self) -> dict:
        return {
            "duration_seconds": round(self.duration, 1),
            "districts_sampled": self.districts_sampled,
            "local_bodies_discovered": self.local_bodies_discovered,
            "local_bodies_sampled": self.local_bodies_sampled,
            "projects_scraped": self.projects_scraped,
            "projects_sampled": self.projects_sampled,
            "pdfs_downloaded": self.pdfs_downloaded,
            "pdfs_missing": self.pdfs_missing,
            "pdfs_error": self.pdfs_error,
            "errors": self.errors,
        }


class TestPipeline:
    """Test pipeline that samples random data from each phase."""

    def __init__(self, n: int = 3, skip_discovery: bool = False, verbose: bool = True):
        """Initialize the test pipeline.

        Args:
            n: Number of samples per level
            skip_discovery: Skip Phase 1 even if no districts exist
            verbose: Print progress messages
        """
        self.n = n
        self.skip_discovery = skip_discovery
        self.verbose = verbose
        self.stats = TestPipelineStats()

    def log(self, message: str, level: str = "info"):
        """Print a log message if verbose."""
        if self.verbose:
            prefix = {
                "info": "  ",
                "success": "✅",
                "warning": "⚠️ ",
                "error": "❌",
            }.get(level, "  ")
            print(f"{prefix} {message}")

    def run(self) -> TestPipelineStats:
        """Run the test pipeline.

        Returns:
            Statistics from the pipeline run
        """
        print()
        print("╔════════════════════════════════════════════════════════════╗")
        print("║           SULEKHA TEST PIPELINE                            ║")
        print(f"║           {datetime.now().strftime('%Y-%m-%d %H:%M:%S'):<43}    ║")
        print(f"║           Samples per level: {self.n:<26}       ║")
        print("╚════════════════════════════════════════════════════════════╝")
        print()

        try:
            # Step 1: Ensure discovery is complete
            self.log("Step 1: Checking discovery status...")
            districts = self.ensure_discovery()
            if not districts:
                self.log("No districts available. Exiting.", "error")
                return self.stats

            # Step 2: Sample random districts
            self.log(f"Step 2: Sampling {self.n} random districts...")
            sampled_districts = self.sample_districts(self.n)
            self.stats.districts_sampled = len(sampled_districts)
            self.log(f"Sampled {len(sampled_districts)} districts", "success")

            # Step 3: Discover local bodies for sampled districts
            self.log("Step 3: Discovering local bodies for sampled districts...")
            for district in sampled_districts:
                self.discover_local_bodies_for_district(district)

            # Step 4: Sample random local bodies
            self.log(f"Step 4: Sampling {self.n} local bodies per district...")
            sampled_local_bodies = self.sample_local_bodies(sampled_districts, self.n)
            self.stats.local_bodies_sampled = len(sampled_local_bodies)
            self.log(f"Sampled {len(sampled_local_bodies)} local bodies", "success")

            # Step 5: Scrape projects for sampled local bodies
            self.log("Step 5: Scraping projects for sampled local bodies...")
            for lb in sampled_local_bodies:
                self.scrape_projects_for_local_body(lb)

            # Step 6: Sample random projects
            self.log(f"Step 6: Sampling {self.n} projects per local body...")
            sampled_projects = self.sample_projects(sampled_local_bodies, self.n)
            self.stats.projects_sampled = len(sampled_projects)
            self.log(f"Sampled {len(sampled_projects)} projects", "success")

            # Step 7: Download PDFs for sampled projects
            self.log("Step 7: Downloading PDFs for sampled projects...")
            for project in sampled_projects:
                self.download_pdf_for_project(project)

        except Exception as e:
            self.stats.errors.append(f"Pipeline error: {str(e)}")
            self.log(f"Pipeline failed: {e}", "error")
            raise

        # Report results
        self.report_results()
        return self.stats

    def ensure_discovery(self) -> bool:
        """Ensure Phase 1 is complete.

        Returns:
            True if districts exist, False otherwise
        """
        from sulekha.db.repositories import DistrictRepository
        from sulekha.db.session import get_session

        with get_session() as session:
            repo = DistrictRepository(session)
            total = repo.get_total_count()

            if total > 0:
                self.log(f"Found {total} existing districts", "success")
                return True

            if self.skip_discovery:
                self.log("No districts found and --skip-discovery is set", "warning")
                return False

            # Run discovery
            self.log("No districts found. Running Phase 1 discovery...")
            from sulekha.tasks.discovery import discover_all_districts

            result = discover_all_districts.apply()
            if result.successful():
                discovered = result.result.get("districts_discovered", 0)
                self.log(f"Discovered {discovered} districts", "success")
                return discovered > 0
            else:
                self.log(f"Discovery failed: {result.result}", "error")
                return False

    def sample_districts(self, n: int) -> list:
        """Sample n random districts.

        Args:
            n: Number of districts to sample

        Returns:
            List of sampled District objects
        """
        from sulekha.db.repositories import DistrictRepository
        from sulekha.db.session import get_session

        with get_session() as session:
            repo = DistrictRepository(session)
            districts = repo.get_random(limit=n)

            for d in districts:
                self.log(f"  - {d.district_name} ({d.year_label}, {d.lb_type_label})")

            # Detach from session
            session.expunge_all()
            return districts

    def discover_local_bodies_for_district(self, district) -> int:
        """Discover local bodies for a specific district.

        Args:
            district: District object

        Returns:
            Number of local bodies discovered
        """
        from sulekha.db.models import DistrictStatus
        from sulekha.db.repositories import DistrictRepository, LocalBodyRepository
        from sulekha.db.session import get_session

        # Check if already done
        with get_session() as session:
            repo = DistrictRepository(session)
            current = repo.get(district.id)

            if current and current.status == DistrictStatus.DONE:
                lb_repo = LocalBodyRepository(session)
                count = len(lb_repo.get_all_for_district(district.id))
                self.log(f"  - {district.district_name}: {count} local bodies (cached)")
                self.stats.local_bodies_discovered += count
                return count

        # Run discovery
        from sulekha.tasks.discovery import discover_local_bodies_for_district

        self.log(f"  - Discovering local bodies for {district.district_name}...")
        result = discover_local_bodies_for_district.apply(args=[district.id])

        if result.successful():
            count = result.result.get("local_bodies_discovered", 0)
            self.stats.local_bodies_discovered += count
            self.log(f"  - {district.district_name}: {count} local bodies", "success")
            return count
        else:
            self.stats.errors.append(
                f"Failed to discover local bodies for {district.district_name}"
            )
            self.log(
                f"  - {district.district_name}: Failed - {result.result}", "error"
            )
            return 0

    def sample_local_bodies(self, districts: list, n: int) -> list:
        """Sample n random local bodies from each district.

        Args:
            districts: List of District objects
            n: Number of local bodies to sample per district

        Returns:
            List of sampled LocalBody objects
        """
        from sulekha.db.repositories import LocalBodyRepository
        from sulekha.db.session import get_session

        sampled = []
        with get_session() as session:
            repo = LocalBodyRepository(session)

            for district in districts:
                lbs = repo.get_random_for_district(district.id, limit=n)
                for lb in lbs:
                    self.log(f"  - {lb.lb_name} ({district.district_name})")
                sampled.extend(lbs)

            # Detach from session
            session.expunge_all()
            return sampled

    def scrape_projects_for_local_body(self, local_body) -> int:
        """Scrape projects for a specific local body.

        Args:
            local_body: LocalBody object

        Returns:
            Number of projects scraped
        """
        from sulekha.db.models import LocalBodyStatus
        from sulekha.db.repositories import LocalBodyRepository, ProjectRepository
        from sulekha.db.session import get_session

        # Check if already done
        with get_session() as session:
            repo = LocalBodyRepository(session)
            current = repo.get(local_body.id)

            if current and current.status == LocalBodyStatus.DONE:
                project_repo = ProjectRepository(session)
                count = len(project_repo.get_all_for_local_body(local_body.id))
                self.log(f"  - {local_body.lb_name}: {count} projects (cached)")
                self.stats.projects_scraped += count
                return count

        # Run scraping
        from sulekha.tasks.table_scraper import scrape_projects_for_local_body

        self.log(f"  - Scraping projects for {local_body.lb_name}...")
        result = scrape_projects_for_local_body.apply(args=[local_body.id])

        if result.successful():
            count = result.result.get("projects_scraped", 0)
            self.stats.projects_scraped += count
            self.log(f"  - {local_body.lb_name}: {count} projects", "success")
            return count
        else:
            self.stats.errors.append(
                f"Failed to scrape projects for {local_body.lb_name}"
            )
            self.log(f"  - {local_body.lb_name}: Failed - {result.result}", "error")
            return 0

    def sample_projects(self, local_bodies: list, n: int) -> list:
        """Sample n random projects from each local body.

        Args:
            local_bodies: List of LocalBody objects
            n: Number of projects to sample per local body

        Returns:
            List of sampled Project objects
        """
        from sulekha.db.repositories import ProjectRepository
        from sulekha.db.session import get_session

        sampled = []
        with get_session() as session:
            repo = ProjectRepository(session)

            for lb in local_bodies:
                projects = repo.get_random_for_local_body(lb.id, limit=n)
                for p in projects:
                    name = p.project_name[:40] + "..." if len(p.project_name) > 40 else p.project_name
                    self.log(f"  - {p.project_no}: {name}")
                sampled.extend(projects)

            # Detach from session
            session.expunge_all()
            return sampled

    def download_pdf_for_project(self, project) -> bool:
        """Download PDF for a specific project.

        Args:
            project: Project object

        Returns:
            True if successful, False otherwise
        """
        from sulekha.db.models import PdfStatus
        from sulekha.db.repositories import ProjectRepository
        from sulekha.db.session import get_session

        # Check if already done
        with get_session() as session:
            repo = ProjectRepository(session)
            current = repo.get(project.id)

            if current and current.pdf_status == PdfStatus.DOWNLOADED:
                self.log(f"  - {project.project_no}: Already downloaded (cached)")
                self.stats.pdfs_downloaded += 1
                return True
            elif current and current.pdf_status == PdfStatus.MISSING:
                self.log(f"  - {project.project_no}: No PDF available (cached)")
                self.stats.pdfs_missing += 1
                return True

        # Run download
        from sulekha.tasks.pdf_scraper import download_pdf_for_project

        self.log(f"  - Downloading PDF for {project.project_no}...")
        result = download_pdf_for_project.apply(args=[project.id])

        if result.successful():
            task_result = result.result
            if task_result.get("pdf_downloaded"):
                self.stats.pdfs_downloaded += 1
                self.log(f"  - {project.project_no}: Downloaded", "success")
                return True
            else:
                self.stats.pdfs_missing += 1
                self.log(f"  - {project.project_no}: No PDF available", "warning")
                return True
        else:
            self.stats.pdfs_error += 1
            self.stats.errors.append(f"Failed to download PDF for {project.project_no}")
            self.log(f"  - {project.project_no}: Failed - {result.result}", "error")
            return False

    def report_results(self):
        """Print final results."""
        print()
        print("=" * 60)
        print("  TEST PIPELINE RESULTS")
        print("=" * 60)
        print()
        print(f"  Duration: {self.stats.duration:.1f} seconds")
        print()
        print("  Sampling:")
        print(f"    Districts sampled:     {self.stats.districts_sampled}")
        print(f"    Local bodies sampled:  {self.stats.local_bodies_sampled}")
        print(f"    Projects sampled:      {self.stats.projects_sampled}")
        print()
        print("  Results:")
        print(f"    Local bodies discovered: {self.stats.local_bodies_discovered}")
        print(f"    Projects scraped:        {self.stats.projects_scraped}")
        print(f"    PDFs downloaded:         {self.stats.pdfs_downloaded}")
        print(f"    PDFs missing:            {self.stats.pdfs_missing}")
        print(f"    PDF errors:              {self.stats.pdfs_error}")
        print()

        if self.stats.errors:
            print("  Errors:")
            for error in self.stats.errors[:10]:
                print(f"    - {error}")
            if len(self.stats.errors) > 10:
                print(f"    ... and {len(self.stats.errors) - 10} more")
            print()

        # Success/failure summary
        total_operations = (
            self.stats.local_bodies_discovered
            + self.stats.projects_scraped
            + self.stats.pdfs_downloaded
            + self.stats.pdfs_missing
        )
        if total_operations > 0 and len(self.stats.errors) == 0:
            print("  ✅ TEST PIPELINE COMPLETED SUCCESSFULLY")
        elif total_operations > 0:
            print("  ⚠️  TEST PIPELINE COMPLETED WITH ERRORS")
        else:
            print("  ❌ TEST PIPELINE FAILED")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Test the Sulekha pipeline by sampling random data"
    )
    parser.add_argument(
        "-n",
        "--n",
        type=int,
        default=3,
        help="Number of samples per level (default: 3)",
    )
    parser.add_argument(
        "--skip-discovery",
        action="store_true",
        help="Skip discovery phase even if no districts exist",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Quiet mode - less verbose output",
    )
    args = parser.parse_args()

    pipeline = TestPipeline(
        n=args.n,
        skip_discovery=args.skip_discovery,
        verbose=not args.quiet,
    )

    try:
        stats = pipeline.run()
        return 0 if len(stats.errors) == 0 else 1
    except Exception as e:
        print(f"\n❌ Pipeline failed with exception: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
