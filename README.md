# Sulekha Data Extraction Service

A production-grade data extraction service for scraping municipal project data from the Kerala Sulekha portal (`https://plan.lsgkerala.gov.in/formulation/Public.aspx`).

## Features

- **4-Phase Breadth-First Scraping Pipeline**
  - Phase 1: Discover all districts (Year × LB Type combinations)
  - Phase 2: Discover all local bodies for each district
  - Phase 3: Scrape all projects for each local body
  - Phase 4: Download PDFs for each project

- **Robust Architecture**
  - PostgreSQL for progress tracking and resumption
  - Celery + Redis for distributed task execution
  - Automatic retries with exponential backoff
  - Graceful resumption after crashes

- **Cloud Storage Integration**
  - Google Cloud Storage for production
  - Minio (S3-compatible) for local development
  - Structured path organization
  - Content hash deduplication

- **Monitoring & Observability**
  - Flower UI for task monitoring
  - Structured JSON logging
  - CLI for progress tracking

## Quick Start

### Prerequisites

- Docker and Docker Compose
- (Optional) GCP service account for GCS storage

### 1. Clone and Configure

```bash
# Copy environment file
cp env.example .env

# Edit .env with your settings
# - GCS_BUCKET_NAME
# - GCS_PROJECT_ID
```

### 2. Start Services

```bash
# Start all services
docker-compose up -d

# Run database migrations
docker-compose --profile migrate up migrate

# Check logs
docker-compose logs -f worker
```

### 3. Run the Pipeline

```bash
# Run full pipeline
docker-compose exec worker sulekha run-pipeline

# Or run individual phases
docker-compose exec worker sulekha run-phase 1  # Discover districts
docker-compose exec worker sulekha run-phase 2  # Discover local bodies
docker-compose exec worker sulekha run-phase 3  # Scrape projects
docker-compose exec worker sulekha run-phase 4  # Download PDFs

# Check progress
docker-compose exec worker sulekha progress
```

### 4. Monitor

- **Flower UI**: http://localhost:5555
- **PostgreSQL**: localhost:5432 (user: sulekha, password: sulekha)
- **Redis**: localhost:6379

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Orchestration Layer                       │
├─────────────────────────────────────────────────────────────┤
│  Celery Beat ──► Redis Queue ──► Workers (3-5 concurrent)   │
│                       │                                      │
│                   Flower UI                                  │
└─────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────┴───────────────────────────────┐
│                      Data Layer                            │
├────────────────────────────────────────────────────────────┤
│  PostgreSQL          │           GCS Bucket               │
│  - scrape_runs       │           - pdfs/{year}/...        │
│  - districts         │                                     │
│  - local_bodies      │                                     │
│  - projects          │                                     │
│  - pdfs              │                                     │
└────────────────────────────────────────────────────────────┘
                            │
┌───────────────────────────┴───────────────────────────────┐
│                   Sulekha Portal                           │
│           https://plan.lsgkerala.gov.in                    │
└────────────────────────────────────────────────────────────┘
```

## Database Schema

| Table | Purpose |
|-------|---------|
| `scrape_runs` | Track pipeline execution runs |
| `districts` | Year × LB Type × District combinations |
| `local_bodies` | Local bodies within districts |
| `projects` | Municipal projects with metadata |
| `pdfs` | PDF download and GCS upload tracking |

## Configuration

All settings can be configured via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | postgresql+psycopg://... | PostgreSQL connection URL |
| `REDIS_URL` | redis://localhost:6379/0 | Redis connection URL |
| `STORAGE_BACKEND` | gcs | Storage backend: `gcs` or `s3` |
| `GCS_BUCKET_NAME` | sulekha-pdfs | GCS bucket for PDFs |
| `GCS_PROJECT_ID` | - | GCP project ID |
| `S3_BUCKET_NAME` | sulekha-pdfs | S3/Minio bucket name |
| `S3_ENDPOINT_URL` | http://localhost:9000 | S3/Minio endpoint |
| `S3_ACCESS_KEY` | minioadmin | S3/Minio access key |
| `S3_SECRET_KEY` | minioadmin | S3/Minio secret key |
| `SCRAPER_REQUEST_DELAY` | 1.2 | Delay between requests (seconds) |
| `SCRAPER_MAX_RETRIES` | 8 | Max retries for failed requests |
| `SCRAPER_MAX_WORKERS` | 3 | Max concurrent workers |
| `LOG_LEVEL` | INFO | Logging level |
| `LOG_FORMAT` | json | Log format (json/console) |

## Development

### Development Mode (Minio for Storage)

For local development, use the dev compose file which includes Minio as an S3-compatible storage backend:

```bash
# Start development services (with Minio)
docker-compose -f docker-compose.dev.yml up -d

# Initialize database
docker-compose -f docker-compose.dev.yml --profile migrate up migrate

# Check services are running
docker-compose -f docker-compose.dev.yml ps

# View logs
docker-compose -f docker-compose.dev.yml logs -f worker
```

**Access Points:**
 (minioadmin/minioadmin)
- Flower: http://localhost:5555
- PostgreSQL: localhost:5432
- Redis: localhost:6379

### Running Tests

```bash
# Start test infrastructure
docker-compose -f docker-compose.dev.yml up postgres-test redis minio -d

# Run all tests
docker-compose -f docker-compose.dev.yml --profile test run test

# Or run locally with pytest
pip install -e ".[dev]"
pytest tests/ -v

# Run specific test file
pytest tests/test_phase1_discovery.py -v

# Run with coverage
pytest tests/ --cov=sulekha --cov-report=html
```

### Local Setup (without Docker)

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -e ".[dev]"

# Start only required services via Docker
docker-compose -f docker-compose.dev.yml up postgres redis minio minio-init -d

# Configure environment
export DATABASE_URL=postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha
export REDIS_URL=redis://localhost:6379/0
export STORAGE_BACKEND=s3
export S3_ENDPOINT_URL=http://localhost:9000
export S3_ACCESS_KEY=minioadmin
export S3_SECRET_KEY=minioadmin

# Initialize database
sulekha init-db

# Test scraper connection
sulekha test-scraper

# Start worker locally
sulekha worker
```

### CLI Commands

```bash
sulekha --help              # Show help
sulekha init-db             # Initialize database
sulekha test-scraper        # Test scraper connection
sulekha run-pipeline        # Run full pipeline
sulekha run-phase N         # Run specific phase (1-4)
sulekha progress            # Show pipeline progress
sulekha worker              # Start Celery worker
sulekha beat                # Start Celery beat
sulekha flower              # Start Flower UI
```

### Test Structure

```
tests/
├── conftest.py                  # Shared fixtures
├── test_phase1_discovery.py     # Phase 1: District discovery
├── test_phase2_local_bodies.py  # Phase 2: Local body discovery
├── test_phase3_projects.py      # Phase 3: Project scraping
├── test_phase4_pdfs.py          # Phase 4: PDF download
└── test_integration.py          # End-to-end integration tests
```

## Resumption

The service automatically resumes from where it left off:

1. **Phase-level resumption**: Completed phases are skipped
2. **Entity-level resumption**: Done entities are skipped
3. **Page-level resumption**: For project scraping, resumes from `last_page_scraped`

Example crash recovery:
```
Run 1: Crashes in Phase 3 after scraping 500/12000 local bodies
       └─ DB: 500 DONE, 11500 PENDING

Run 2: Automatically resumes
       └─ Skips Phase 1 & 2 (complete)
       └─ Queries WHERE status IN ('PENDING', 'PARTIAL')
       └─ Continues with remaining 11500 local bodies
```

## License

MIT
