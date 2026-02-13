"""Configuration management for Sulekha Data Extraction Service.

Uses pydantic-settings for environment-based configuration with validation.
All settings can be overridden via environment variables.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    All settings can be overridden by setting the corresponding
    environment variable (case-insensitive).
    """

    model_config = SettingsConfigDict(
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
        description="PostgreSQL connection URL",
    )

    # ==========================================================================
    # Redis Configuration
    # ==========================================================================
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for Celery broker",
    )

    # ==========================================================================
    # Storage Configuration
    # ==========================================================================
    storage_backend: Literal["gcs", "s3"] = Field(
        default="gcs",
        description="Storage backend: 'gcs' for Google Cloud Storage, 's3' for S3/Minio",
    )

    # Google Cloud Storage (production)
    gcs_bucket_name: str = Field(
        default="sulekha-pdfs",
        description="GCS bucket name for storing PDFs",
    )
    gcs_project_id: str = Field(
        default="",
        description="GCP project ID (optional if using default credentials)",
    )

    # S3/Minio (development)
    s3_bucket_name: str = Field(
        default="sulekha-pdfs",
        description="S3/Minio bucket name for storing PDFs",
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
        default="https://plan.lsgkerala.gov.in/formulation/Public.aspx",
        description="Base URL for the Sulekha portal",
    )
    scraper_request_delay: float = Field(
        default=1.2,
        ge=0.5,
        le=10.0,
        description="Delay between requests in seconds (rate limiting)",
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
    scraper_max_workers: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum concurrent scraper workers",
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
    # Retry Configuration
    # ==========================================================================
    max_district_retries: int = Field(
        default=3,
        description="Maximum retries for district discovery",
    )
    max_lb_retries: int = Field(
        default=3,
        description="Maximum retries for local body project scraping",
    )
    max_pdf_retries: int = Field(
        default=3,
        description="Maximum retries for PDF downloads",
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
        default=3,
        ge=1,
        le=10,
        description="Number of concurrent Celery worker processes",
    )
    celery_task_time_limit: int = Field(
        default=3600,  # 1 hour
        description="Hard time limit for tasks in seconds",
    )
    celery_task_soft_time_limit: int = Field(
        default=3000,  # 50 minutes
        description="Soft time limit for tasks in seconds",
    )

    # ==========================================================================
    # Queue Management Configuration
    # ==========================================================================
    max_queue_size: int = Field(
        default=1000,
        ge=10,
        le=100000,
        description="Maximum tasks in queue before pausing task scheduling",
    )
    queue_check_interval: float = Field(
        default=5.0,
        ge=1.0,
        le=60.0,
        description="Seconds between queue size checks when waiting for space",
    )
    default_batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Default batch size for task scheduling",
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
    """Get cached settings instance.

    Returns:
        Settings instance loaded from environment
    """
    return Settings()


# Global settings instance for convenience
settings = get_settings()
