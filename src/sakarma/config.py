"""Configuration management for SAKARMA scraper.

Uses pydantic-settings for environment-based configuration with validation.
All settings are read from ``SAKARMA_*`` environment variables (case-insensitive)
or a ``.env`` file. Same pattern as ``src/sulekha/config.py``, but scoped under
the ``SAKARMA_`` prefix so the two tenants can coexist in one process or
``.env`` file without collisions.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """SAKARMA application settings loaded from ``SAKARMA_*`` env vars."""

    model_config = SettingsConfigDict(
        env_prefix="SAKARMA_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ==========================================================================
    # Database Configuration
    # ==========================================================================
    database_url: str = Field(
        default="postgresql+psycopg://sulekha:sulekha@localhost:5432/sulekha",
        description="PostgreSQL connection URL (DB shared with sulekha; tables live in 'sakarma' schema)",
    )

    # ==========================================================================
    # Redis Configuration
    # ==========================================================================
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for the SAKARMA Celery broker",
    )

    # ==========================================================================
    # Storage Configuration — dedicated SAKARMA bucket
    # ==========================================================================
    storage_backend: Literal["gcs", "s3"] = Field(
        default="gcs",
        description="Storage backend: 'gcs' for production, 's3' for local Minio",
    )
    gcs_bucket_name: str = Field(
        default="",
        description="GCS bucket dedicated to SAKARMA artifacts (must be set; separate from sulekha)",
    )
    gcs_project_id: str = Field(
        default="",
        description="GCP project ID (optional if using default credentials)",
    )
    s3_bucket_name: str = Field(
        default="sakarma-test",
        description="S3/Minio bucket name for local development and testing",
    )
    s3_endpoint_url: str = Field(
        default="http://localhost:9000",
        description="S3/Minio endpoint URL",
    )
    s3_access_key: str = Field(
        default="minioadmin",
        description="S3/Minio access key",
    )
    s3_secret_key: str = Field(
        default="minioadmin",
        description="S3/Minio secret key",
    )

    # ==========================================================================
    # Scraper Configuration
    # ==========================================================================
    scraper_base_url: str = Field(
        default="https://meeting.lsgkerala.gov.in",
        description="Base URL for the SAKARMA portal",
    )
    scraper_lbwise_path: str = Field(
        default="/Pages/LBWiseDashBoard.aspx",
        description="Path to the LBWise dashboard (the only page exposing per-meeting drill-downs)",
    )
    scraper_minutes_path: str = Field(
        default="/Pages/PublicMinutes.aspx",
        description="Path to the Minutes artifact page (session-bound)",
    )
    scraper_dregister_path: str = Field(
        default="/Pages/PublicDRegister.aspx",
        description="Path to the Decision Register artifact page (session-bound)",
    )
    scraper_delay_min: float = Field(
        default=0.5,
        ge=0.1,
        le=30.0,
        description="Minimum delay between requests in seconds (random range)",
    )
    scraper_delay_max: float = Field(
        default=1.5,
        ge=0.2,
        le=60.0,
        description="Maximum delay between requests in seconds (random range)",
    )
    scraper_request_timeout: int = Field(
        default=60,
        ge=10,
        le=300,
        description="Request timeout in seconds",
    )
    scraper_max_retries: int = Field(
        default=8,
        ge=1,
        le=20,
        description="Maximum retries for failed requests",
    )
    scraper_backoff_base: float = Field(
        default=2.0,
        description="Base for exponential backoff calculation",
    )
    scraper_backoff_max: float = Field(
        default=180.0,
        description="Maximum backoff time in seconds",
    )

    # ==========================================================================
    # Rate Limiting Configuration
    # ==========================================================================
    rate_limit_enabled: bool = Field(
        default=True,
        description="Enable global rate limiting across all SAKARMA workers",
    )
    rate_limit_max_concurrent: int = Field(
        default=4,
        ge=1,
        le=50,
        description="Maximum concurrent HTTP requests allowed globally for SAKARMA",
    )

    # ==========================================================================
    # Logging Configuration
    # ==========================================================================
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(
        default="INFO",
        description="Logging level",
    )
    log_format: Literal["json", "console"] = Field(
        default="json",
        description="Log output format",
    )

    # ==========================================================================
    # Celery Configuration
    # ==========================================================================
    celery_worker_concurrency: int = Field(
        default=4,
        ge=1,
        le=20,
        description="Number of concurrent SAKARMA Celery worker processes",
    )
    celery_task_time_limit: int = Field(
        default=7200,  # 2 hours per LB-burst
        description="Hard time limit for tasks in seconds",
    )
    celery_task_soft_time_limit: int = Field(
        default=6900,  # 1h55m
        description="Soft time limit for tasks in seconds",
    )

    # ==========================================================================
    # User Agent for HTTP Requests
    # ==========================================================================
    user_agent: str = Field(
        default=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        description="User agent string for HTTP requests",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached SAKARMA settings instance."""
    return Settings()


# Module-level convenience accessor
settings = get_settings()
