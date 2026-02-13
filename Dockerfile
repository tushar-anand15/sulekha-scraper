# Sulekha Data Extraction Service
# Multi-stage build for smaller final image

# Build stage
FROM python:3.11-slim as builder

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy project files needed for dependency resolution
COPY pyproject.toml uv.lock README.md ./

# Install dependencies using uv (creates virtualenv in .venv)
RUN uv sync --frozen --no-dev --no-install-project

# Copy source code and install the project
COPY src/ ./src/
RUN uv sync --frozen --no-dev

# Runtime stage
FROM python:3.11-slim

WORKDIR /app

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Copy virtualenv from builder
COPY --from=builder /app/.venv /app/.venv

# Copy application code
COPY src/ ./src/
COPY alembic/ ./alembic/
COPY alembic.ini ./

# Set environment variables
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# Create non-root user
RUN useradd -m -u 1000 sulekha && chown -R sulekha:sulekha /app
USER sulekha

# Default command (can be overridden in docker-compose)
CMD ["python", "-m", "sulekha.cli", "worker"]
