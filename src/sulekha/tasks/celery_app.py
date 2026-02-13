"""Celery application configuration for Sulekha service.

This module configures the Celery app with Redis broker,
task routing, and worker settings optimized for web scraping.
"""

from celery import Celery

from sulekha.config import settings

# Create Celery app
celery_app = Celery(
    "sulekha",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "sulekha.tasks.discovery",
        "sulekha.tasks.table_scraper",
        "sulekha.tasks.pdf_scraper",
        "sulekha.tasks.orchestrator",
    ],
)

# Celery configuration
celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    
    # Task execution settings
    task_time_limit=settings.celery_task_time_limit,
    task_soft_time_limit=settings.celery_task_soft_time_limit,
    task_acks_late=True,  # Tasks are acked after execution (for reliability)
    task_reject_on_worker_lost=True,  # Requeue tasks if worker dies
    
    # Worker settings
    worker_prefetch_multiplier=1,  # Only prefetch one task at a time (important for long tasks)
    worker_concurrency=settings.celery_worker_concurrency,
    
    # Result backend settings
    result_expires=86400,  # Results expire after 24 hours
    result_extended=True,  # Store task args/kwargs in result
    
    # Task routing - different queues for different task types
    task_routes={
        "sulekha.tasks.discovery.*": {"queue": "discovery"},
        "sulekha.tasks.table_scraper.*": {"queue": "scraper"},
        "sulekha.tasks.pdf_scraper.*": {"queue": "pdf"},
        "sulekha.tasks.orchestrator.*": {"queue": "orchestrator"},
    },
    
    # Default queue
    task_default_queue="default",
    
    # Task retry settings (global defaults)
    task_default_retry_delay=60,  # 1 minute
    task_max_retries=3,
    
    # Beat scheduler settings (for periodic tasks)
    beat_scheduler="celery.beat:PersistentScheduler",
    beat_schedule_filename="celerybeat-schedule",
    
    # Broker settings
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,
)

# Define task queues with priorities
celery_app.conf.task_queues = {
    "default": {
        "exchange": "default",
        "routing_key": "default",
    },
    "discovery": {
        "exchange": "discovery",
        "routing_key": "discovery",
    },
    "scraper": {
        "exchange": "scraper",
        "routing_key": "scraper",
    },
    "pdf": {
        "exchange": "pdf",
        "routing_key": "pdf",
    },
    "orchestrator": {
        "exchange": "orchestrator",
        "routing_key": "orchestrator",
    },
}


# Task base class with common error handling
class SulekhaTask(celery_app.Task):
    """Base task class with error handling and logging."""

    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """Called when task fails."""
        import structlog

        logger = structlog.get_logger(__name__)
        logger.error(
            "Task failed",
            task_name=self.name,
            task_id=task_id,
            args=args,
            kwargs=kwargs,
            exception=str(exc),
        )

    def on_success(self, retval, task_id, args, kwargs):
        """Called when task succeeds."""
        import structlog

        logger = structlog.get_logger(__name__)
        logger.info(
            "Task succeeded",
            task_name=self.name,
            task_id=task_id,
        )

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        """Called when task is retried."""
        import structlog

        logger = structlog.get_logger(__name__)
        logger.warning(
            "Task retrying",
            task_name=self.name,
            task_id=task_id,
            exception=str(exc),
        )


# Set the default task base class
celery_app.Task = SulekhaTask
