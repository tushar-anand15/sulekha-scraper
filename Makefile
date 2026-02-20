# Sulekha Pipeline Makefile
# =========================
#
# This Makefile provides commands for running each phase of the
# data extraction pipeline with proper queue management.
#
# Usage:
#   make discovery       - Run Phase 1: Discover districts
#   make local_bodies    - Run Phase 2: Discover local bodies
#   make project_tables  - Run Phase 3: Scrape project tables
#   make pdfs            - Run Phase 4: Download PDFs
#   make pipeline        - Run all phases sequentially
#   make test-pipeline   - Run test pipeline with random sampling
#   make status          - Show pipeline status

.PHONY: help services discovery local_bodies project_tables pdfs pipeline test-pipeline status clean worker

# =============================================================================
# Configuration (can be overridden via environment or command line)
# =============================================================================

BATCH_SIZE ?= 100
MAX_QUEUE ?= 1000
SAMPLE_N ?= 3

# Docker compose file
COMPOSE_FILE ?= docker-compose.yml

# =============================================================================
# Help
# =============================================================================

help:
	@echo ""
	@echo "Sulekha Pipeline Commands"
	@echo "========================="
	@echo ""
	@echo "Pipeline Phases:"
	@echo "  make discovery        Run Phase 1: Discover all districts"
	@echo "  make local_bodies     Run Phase 2: Discover local bodies for all districts"
	@echo "  make project_tables   Run Phase 3: Scrape project tables for all local bodies"
	@echo "  make pdfs             Run Phase 4: Download PDFs for all projects"
	@echo "  make pipeline         Run all phases sequentially"
	@echo ""
	@echo "Testing:"
	@echo "  make test-pipeline    Run test pipeline with random sampling (n=$(SAMPLE_N))"
	@echo ""
	@echo "Utilities:"
	@echo "  make services         Start required services (postgres, redis)"
	@echo "  make worker           Start a Celery worker"
	@echo "  make status           Show pipeline status"
	@echo "  make queue-status     Show Celery queue status"
	@echo "  make clean            Stop all services"
	@echo ""
	@echo "Configuration (override with environment variables):"
	@echo "  BATCH_SIZE=$(BATCH_SIZE)    Number of items per batch"
	@echo "  MAX_QUEUE=$(MAX_QUEUE)     Maximum queue size before pausing"
	@echo "  SAMPLE_N=$(SAMPLE_N)       Number of samples per level in test pipeline"
	@echo ""

# =============================================================================
# Services
# =============================================================================

services:
	@echo "Starting services..."
	docker compose -f $(COMPOSE_FILE) up -d postgres redis
	@echo "Waiting for services to be ready..."
	@sleep 3
	@echo "Services started."

stop:
	@echo "Stopping services..."
	docker compose -f $(COMPOSE_FILE) down
	@echo "Services stopped."

clean: stop
	@echo "Cleaning up..."
	docker compose -f $(COMPOSE_FILE) down -v
	@echo "Cleanup complete."

# =============================================================================
# Worker
# =============================================================================

worker: services
	@echo "Starting Celery worker..."
	uv run sulekha worker

# =============================================================================
# Phase 1: Discovery
# =============================================================================

discovery: services init-db
	@echo ""
	@echo "============================================================"
	@echo "  Running Phase 1: Discovery"
	@echo "============================================================"
	@echo "  This phase discovers all Year x LB Type x District combinations"
	@echo ""
	uv run sulekha run-discovery --batch-size=$(BATCH_SIZE) --max-queue=$(MAX_QUEUE)

# =============================================================================
# Phase 2: Local Bodies
# =============================================================================

local_bodies: services
	@echo ""
	@echo "============================================================"
	@echo "  Running Phase 2: Local Bodies"
	@echo "============================================================"
	@echo "  This phase discovers all local bodies for each district"
	@echo "  Prerequisite: Phase 1 (discovery) must be complete"
	@echo ""
	uv run sulekha run-local-bodies --batch-size=$(BATCH_SIZE) --max-queue=$(MAX_QUEUE)

# =============================================================================
# Phase 3: Project Tables
# =============================================================================

project_tables: services
	@echo ""
	@echo "============================================================"
	@echo "  Running Phase 3: Project Tables"
	@echo "============================================================"
	@echo "  This phase scrapes project tables for all local bodies"
	@echo "  Prerequisite: Phase 2 (local bodies) must be complete"
	@echo ""
	uv run sulekha run-project-tables --batch-size=$(BATCH_SIZE) --max-queue=$(MAX_QUEUE)

# =============================================================================
# Phase 4: PDFs
# =============================================================================

pdfs: services
	@echo ""
	@echo "============================================================"
	@echo "  Running Phase 4: PDFs"
	@echo "============================================================"
	@echo "  This phase downloads PDFs for all projects"
	@echo "  Prerequisite: Phase 3 (project tables) must be complete"
	@echo ""
	uv run sulekha run-pdfs --batch-size=$(BATCH_SIZE) --max-queue=$(MAX_QUEUE)

# =============================================================================
# Full Pipeline
# =============================================================================

pipeline: services
	@echo ""
	@echo "============================================================"
	@echo "  Running Full Pipeline"
	@echo "============================================================"
	@echo "  This will run all phases sequentially:"
	@echo "    1. Discovery (districts)"
	@echo "    2. Local Bodies"
	@echo "    3. Project Tables"
	@echo "    4. PDFs"
	@echo ""
	uv run sulekha run-all --batch-size=$(BATCH_SIZE) --max-queue=$(MAX_QUEUE)

# =============================================================================
# Test Pipeline
# =============================================================================

test-pipeline: services
	@echo ""
	@echo "============================================================"
	@echo "  Running Test Pipeline (n=$(SAMPLE_N))"
	@echo "============================================================"
	@echo "  This will randomly sample data from each phase:"
	@echo "    - $(SAMPLE_N) random districts"
	@echo "    - $(SAMPLE_N) random local bodies per district"
	@echo "    - $(SAMPLE_N) random projects per local body"
	@echo "    - Download PDFs for sampled projects"
	@echo ""
	uv run python scripts/test_pipeline.py --n=$(SAMPLE_N)

# =============================================================================
# Status Commands
# =============================================================================

status:
	@echo ""
	uv run python scripts/status.py

queue-status:
	@echo ""
	uv run sulekha queue-status

# =============================================================================
# Database Commands
# =============================================================================

init-db:
	@echo "Initializing database schema..."
	@uv run sulekha init-db
	@echo "Database schema initialized."

migrate: services
	@echo "Running database migrations..."
	docker compose -f $(COMPOSE_FILE) --profile migrate up migrate
	@echo "Migrations complete."

# =============================================================================
# Development
# =============================================================================

test:
	@echo "Running tests..."
	uv run pytest tests/ -v

test-cov:
	@echo "Running tests with coverage..."
	uv run pytest tests/ --cov=sulekha --cov-report=html

lint:
	@echo "Running linter..."
	uv run ruff check src/ tests/

format:
	@echo "Formatting code..."
	uv run ruff format src/ tests/
