"""Celery application configuration for SAKARMA scraper.

Sister Celery app to ``src/sulekha/tasks/celery_app.py``. Uses the
``"sakarma"`` app name, Redis broker/backend from ``sakarma.config.settings``,
and namespaced queues so SAKARMA workers do not collide with sulekha workers
sharing the same Redis instance.

Task module imports are resolved lazily by Celery at worker startup, so the
``include=[...]`` list may reference modules that do not yet exist while the
rest of Wave 7 is being built — this module still imports cleanly.
"""

from celery import Celery
from kombu import Exchange, Queue

from sakarma.config import settings
from sakarma.utils.logging import setup_logging

# Configure structlog as soon as the Celery app module is imported so
# workers and any code that imports the app get SAKARMA-formatted logs.
setup_logging()


# Create Celery app
celery_app = Celery(
    "sakarma",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "sakarma.tasks.discovery",
        "sakarma.tasks.manifest",
        "sakarma.tasks.artifacts",
        "sakarma.tasks.reconciliation",
        "sakarma.tasks.orchestrator",
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

    # Task routing - namespaced sakarma queues
    task_routes={
        "sakarma.tasks.discovery.*": {"queue": "sakarma_discovery"},
        "sakarma.tasks.manifest.*": {"queue": "sakarma_manifest"},
        "sakarma.tasks.artifacts.*": {"queue": "sakarma_fetch"},
        "sakarma.tasks.reconciliation.*": {"queue": "sakarma_reconcile"},
        "sakarma.tasks.orchestrator.*": {"queue": "sakarma_orchestrate"},
    },

    # Default queue (namespaced so sulekha's "default" is untouched)
    task_default_queue="sakarma_default",

    # Task retry settings (global defaults)
    task_default_retry_delay=60,  # 1 minute
    task_max_retries=3,

    # Beat scheduler settings (for periodic tasks)
    beat_scheduler="celery.beat:PersistentScheduler",
    beat_schedule_filename="celerybeat-schedule-sakarma",

    # Broker settings
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,
)


# Define task queues — explicit kombu Queue objects so AMQP routing has
# all six destinations registered.
celery_app.conf.task_queues = (
    Queue("sakarma_default", Exchange("sakarma_default"), routing_key="sakarma_default"),
    Queue("sakarma_discovery", Exchange("sakarma_discovery"), routing_key="sakarma_discovery"),
    Queue("sakarma_manifest", Exchange("sakarma_manifest"), routing_key="sakarma_manifest"),
    Queue("sakarma_fetch", Exchange("sakarma_fetch"), routing_key="sakarma_fetch"),
    Queue("sakarma_reconcile", Exchange("sakarma_reconcile"), routing_key="sakarma_reconcile"),
    Queue("sakarma_orchestrate", Exchange("sakarma_orchestrate"), routing_key="sakarma_orchestrate"),
)


# Task base class with common error handling
class SakarmaTask(celery_app.Task):
    """Base task class with error handling and structlog logging."""

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
            exception=type(exc).__name__,
            exception_message=str(exc),
        )

    def on_success(self, retval, task_id, args, kwargs):
        """Called when task succeeds."""
        import structlog

        logger = structlog.get_logger(__name__)
        logger.info(
            "Task succeeded",
            task_name=self.name,
            task_id=task_id,
            args=args,
            kwargs=kwargs,
        )

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        """Called when task is retried."""
        import structlog

        logger = structlog.get_logger(__name__)
        logger.warning(
            "Task retrying",
            task_name=self.name,
            task_id=task_id,
            args=args,
            kwargs=kwargs,
            exception=type(exc).__name__,
            exception_message=str(exc),
        )


# Set the default task base class
celery_app.Task = SakarmaTask
