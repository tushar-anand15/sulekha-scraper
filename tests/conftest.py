"""Pytest configuration and shared fixtures for Sulekha tests.

This module provides:
- Database fixtures (PostgreSQL test database)
- Storage fixtures (Minio mock for GCS)
- HTTP mocking fixtures (responses library)
- Sample data fixtures

Usage:
    # Start services first (if not already running):
    docker-compose -f docker-compose.dev.yml up -d postgres redis minio

    # Run tests locally:
    pytest tests/ -v

    # Run only unit tests (no database required):
    pytest tests/ -v -m "not integration"
"""

import os
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import responses
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

# Set test environment with fallbacks - allows override via environment
os.environ.setdefault("STORAGE_BACKEND", "s3")
os.environ.setdefault("S3_ENDPOINT_URL", "http://localhost:9000")
os.environ.setdefault("S3_ACCESS_KEY", "minioadmin")
os.environ.setdefault("S3_SECRET_KEY", "minioadmin")
os.environ.setdefault("S3_BUCKET_NAME", "sulekha-test")
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha_test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("LOG_FORMAT", "console")
os.environ.setdefault("LOG_LEVEL", "DEBUG")

from sulekha.db.models import Base, District, LocalBody, Project, ScrapeRun
from sulekha.storage.gcs import reset_storage


# =============================================================================
# Database Fixtures
# =============================================================================


def _ensure_test_database_exists():
    """Create the test database if it doesn't exist.
    
    Connects to the default 'sulekha' database to create 'sulekha_test'.
    """
    # Parse the DATABASE_URL to get the test db name
    db_url = os.environ["DATABASE_URL"]
    # Connect to the main database to create test db
    main_db_url = db_url.rsplit("/", 1)[0] + "/sulekha"
    
    try:
        engine = create_engine(main_db_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            # Check if test database exists
            result = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = 'sulekha_test'")
            )
            if not result.fetchone():
                conn.execute(text("CREATE DATABASE sulekha_test"))
        engine.dispose()
    except Exception as e:
        # If we can't connect to create the db, the test db might already exist
        # or postgres isn't running - let the actual test fixture handle the error
        pass


@pytest.fixture(scope="session")
def test_engine():
    """Create a test database engine.
    
    Skips tests if PostgreSQL is not available.
    """
    _ensure_test_database_exists()
    
    try:
        engine = create_engine(
            os.environ["DATABASE_URL"],
            pool_pre_ping=True,
            echo=False,
        )
        # Test the connection
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as e:
        pytest.skip(f"PostgreSQL not available: {e}")


@pytest.fixture(scope="session")
def setup_test_db(test_engine):
    """Create all tables for the test database."""
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def db_session(test_engine, setup_test_db) -> Generator[Session, None, None]:
    """Provide a clean database session for each test."""
    connection = test_engine.connect()
    transaction = connection.begin()

    SessionLocal = sessionmaker(bind=connection)
    session = SessionLocal()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


# =============================================================================
# Storage Fixtures
# =============================================================================


@pytest.fixture(scope="session")
def minio_client():
    """Create a Minio client for testing."""
    try:
        from minio import Minio

        client = Minio(
            "localhost:9000",
            access_key="minioadmin",
            secret_key="minioadmin",
            secure=False,
        )

        # Create test bucket if it doesn't exist
        bucket_name = "sulekha-test"
        if not client.bucket_exists(bucket_name):
            client.make_bucket(bucket_name)

        yield client

        # Cleanup: remove all objects from test bucket
        objects = client.list_objects(bucket_name, recursive=True)
        for obj in objects:
            client.remove_object(bucket_name, obj.object_name)

    except Exception as e:
        pytest.skip(f"Minio not available: {e}")


@pytest.fixture
def storage(minio_client):
    """Provide a storage instance for testing."""
    from sulekha.storage.gcs import S3Storage

    reset_storage()
    storage = S3Storage(
        bucket_name="sulekha-test",
        endpoint_url="http://localhost:9000",
        access_key="minioadmin",
        secret_key="minioadmin",
    )
    yield storage

    # Cleanup uploaded files
    for obj in storage.list_objects():
        storage.delete(obj)


@pytest.fixture
def mock_storage():
    """Provide a mock storage for unit tests that don't need real storage."""
    mock = MagicMock()
    mock.bucket_name = "test-bucket"
    mock.upload_pdf.return_value = ("pdfs/test/path.pdf", "abc123hash", 1000)
    mock.download.return_value = b"%PDF-1.4 mock pdf content"
    mock.exists.return_value = True
    mock.delete.return_value = True
    mock.list_objects.return_value = []
    return mock


# =============================================================================
# HTTP Mocking Fixtures
# =============================================================================


@pytest.fixture
def mock_responses():
    """Provide responses mocking for HTTP requests."""
    with responses.RequestsMock() as rsps:
        yield rsps


@pytest.fixture
def sulekha_base_html():
    """Sample HTML for the Sulekha portal base page."""
    return """
    <!DOCTYPE html>
    <html>
    <head><title>Sulekha Portal</title></head>
    <body>
    <form id="form1" method="post" action="./Public.aspx">
        <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="test_viewstate" />
        <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="test_generator" />
        <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="test_validation" />
        
        <select name="drpYear" id="drpYear">
            <option value="0">--Select Year--</option>
            <option value="28">2024-2025</option>
            <option value="27">2023-2024</option>
            <option value="26">2022-2023</option>
        </select>
        
        <select name="drpType" id="drpType">
            <option value="0">--Select LB Type--</option>
            <option value="1">District Panchayat</option>
            <option value="2">Block Panchayat</option>
            <option value="3">Grama Panchayat</option>
            <option value="4">Municipality</option>
            <option value="5">Corporation</option>
        </select>
    </form>
    </body>
    </html>
    """


@pytest.fixture
def sulekha_districts_html():
    """Sample HTML for the districts table (gvState)."""
    return """
    <!DOCTYPE html>
    <html>
    <body>
    <form id="form1" method="post" action="./Public.aspx">
        <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="districts_viewstate" />
        <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="districts_generator" />
        <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="districts_validation" />
        
        <select name="drpYear" id="drpYear">
            <option value="28" selected="selected">2024-2025</option>
        </select>
        
        <select name="drpType" id="drpType">
            <option value="1" selected="selected">District Panchayat</option>
        </select>
        
        <table id="gvState">
            <tr><th>Sl No</th><th>District</th><th>LBs</th><th>Projects</th><th>Details</th></tr>
            <tr>
                <td>1</td>
                <td>Thiruvananthapuram</td>
                <td>1</td>
                <td>1166</td>
                <td><a href="javascript:__doPostBack('gvState','Select$0')">Details</a></td>
            </tr>
            <tr>
                <td>2</td>
                <td>Kollam</td>
                <td>1</td>
                <td>950</td>
                <td><a href="javascript:__doPostBack('gvState','Select$1')">Details</a></td>
            </tr>
            <tr>
                <td>3</td>
                <td>Pathanamthitta</td>
                <td>1</td>
                <td>800</td>
                <td><a href="javascript:__doPostBack('gvState','Select$2')">Details</a></td>
            </tr>
            <tr><td colspan="5">Footer</td></tr>
        </table>
    </form>
    </body>
    </html>
    """


@pytest.fixture
def sulekha_local_bodies_html():
    """Sample HTML for the local bodies table (gvStat)."""
    return """
    <!DOCTYPE html>
    <html>
    <body>
    <form id="form1" method="post" action="./Public.aspx">
        <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="lb_viewstate" />
        <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="lb_generator" />
        <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="lb_validation" />
        
        <table id="gvStat">
            <tr><th>Sl No</th><th>Local Body</th><th>Projects</th><th>Details</th></tr>
            <tr>
                <td>1</td>
                <td>Thiruvananthapuram District Panchayat</td>
                <td>1166</td>
                <td><a href="javascript:__doPostBack('gvStat','Select$0')">Details</a></td>
            </tr>
            <tr><td colspan="4">Footer</td></tr>
        </table>
    </form>
    </body>
    </html>
    """


@pytest.fixture
def sulekha_projects_html():
    """Sample HTML for the projects table (gvProjects)."""
    return """
    <!DOCTYPE html>
    <html>
    <body>
    <form id="form1" method="post" action="./Public.aspx">
        <input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="projects_viewstate" />
        <input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="projects_generator" />
        <input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="projects_validation" />
        
        <table id="gvProjects">
            <tr><th>Project No</th><th>Project Name</th><th>Formulation</th><th>Expense</th></tr>
            <tr>
                <td>PRJ001</td>
                <td>Road Construction</td>
                <td>50000</td>
                <td>45000</td>
                <td><a href="javascript:__doPostBack('gvProjects','Select$0')">View</a></td>
            </tr>
            <tr>
                <td>PRJ002</td>
                <td>School Building</td>
                <td>100000</td>
                <td>95000</td>
                <td><a href="javascript:__doPostBack('gvProjects','Select$1')">View</a></td>
            </tr>
            <tr>
                <td>PRJ003</td>
                <td>Water Supply</td>
                <td>75000</td>
                <td>70000</td>
                <td><a href="javascript:__doPostBack('gvProjects','Select$2')">View</a></td>
            </tr>
            <tr>
                <table>
                    <tr>
                        <td><span>1</span></td>
                        <td><a href="javascript:__doPostBack('gvProjects','Page$2')">2</a></td>
                        <td><a href="javascript:__doPostBack('gvProjects','Page$3')">...</a></td>
                    </tr>
                </table>
            </tr>
        </table>
    </form>
    </body>
    </html>
    """


# =============================================================================
# Sample Data Fixtures
# =============================================================================


@pytest.fixture
def sample_district(db_session) -> District:
    """Create a sample district in the database."""
    from sulekha.db.models import DistrictStatus

    district = District(
        year_val=28,
        year_label="2024-2025",
        lb_type_val=1,
        lb_type_label="District Panchayat",
        district_index=1,
        district_name="Thiruvananthapuram",
        num_local_bodies=1,
        num_projects=1166,
        postback_target="gvState",
        postback_argument="Select$0",
        status=DistrictStatus.PENDING,
    )
    db_session.add(district)
    db_session.flush()
    return district


@pytest.fixture
def sample_local_body(db_session, sample_district) -> LocalBody:
    """Create a sample local body in the database."""
    from sulekha.db.models import LocalBodyStatus

    lb = LocalBody(
        district_id=sample_district.id,
        lb_index=1,
        lb_name="Thiruvananthapuram District Panchayat",
        expected_projects=1166,
        postback_target="gvStat",
        postback_argument="Select$0",
        status=LocalBodyStatus.PENDING,
    )
    db_session.add(lb)
    db_session.flush()
    return lb


@pytest.fixture
def sample_project(db_session, sample_local_body) -> Project:
    """Create a sample project in the database."""
    from sulekha.db.models import PdfStatus

    project = Project(
        local_body_id=sample_local_body.id,
        project_no="PRJ001",
        project_name="Road Construction",
        formulation="50000",
        expense="45000",
        page_number=1,
        select_argument="Select$0",
        pdf_status=PdfStatus.PENDING,
    )
    db_session.add(project)
    db_session.flush()
    return project


@pytest.fixture
def sample_pdf_bytes():
    """Return sample PDF content for testing."""
    # Minimal valid PDF
    return b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >> endobj
xref
0 4
0000000000 65535 f 
0000000009 00000 n 
0000000058 00000 n 
0000000115 00000 n 
trailer << /Size 4 /Root 1 0 R >>
startxref
190
%%EOF
"""
