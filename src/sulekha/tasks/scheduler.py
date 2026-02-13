"""Smart task scheduler with queue size management.

This module provides functions for checking queue sizes and controlling
task scheduling to prevent overwhelming the system with too many pending tasks.
"""

import time
from typing import Optional

import redis
import structlog

from sulekha.config import settings

logger = structlog.get_logger(__name__)


def get_redis_client() -> redis.Redis:
    """Get a Redis client instance."""
    return redis.from_url(settings.redis_url)


def get_queue_size(queue_name: str) -> int:
    """Get the current size of a Celery queue.

    Args:
        queue_name: Name of the Celery queue (e.g., 'discovery', 'scraper', 'pdf')

    Returns:
        Number of tasks currently in the queue
    """
    try:
        client = get_redis_client()
        # Celery stores queues as Redis lists
        size = client.llen(queue_name)
        return size
    except Exception as e:
        logger.warning("Failed to get queue size", queue=queue_name, error=str(e))
        return 0


def get_all_queue_sizes() -> dict[str, int]:
    """Get sizes of all known Celery queues.

    Returns:
        Dictionary mapping queue names to their sizes
    """
    queues = ["default", "discovery", "scraper", "pdf", "orchestrator"]
    sizes = {}
    
    try:
        client = get_redis_client()
        for queue in queues:
            sizes[queue] = client.llen(queue)
    except Exception as e:
        logger.warning("Failed to get queue sizes", error=str(e))
        for queue in queues:
            sizes[queue] = 0
    
    return sizes


def get_total_queue_size() -> int:
    """Get total number of tasks across all queues.

    Returns:
        Total number of pending tasks
    """
    return sum(get_all_queue_sizes().values())


def can_enqueue(queue_name: str, max_size: Optional[int] = None) -> bool:
    """Check if we can enqueue more tasks to a queue.

    Args:
        queue_name: Name of the queue to check
        max_size: Maximum allowed queue size (defaults to settings.max_queue_size)

    Returns:
        True if queue has space, False otherwise
    """
    if max_size is None:
        max_size = settings.max_queue_size
    
    current_size = get_queue_size(queue_name)
    can_add = current_size < max_size
    
    if not can_add:
        logger.debug(
            "Queue is full",
            queue=queue_name,
            current_size=current_size,
            max_size=max_size,
        )
    
    return can_add


def wait_for_queue_space(
    queue_name: str,
    max_size: Optional[int] = None,
    timeout: Optional[float] = None,
    check_interval: Optional[float] = None,
) -> bool:
    """Block until queue has space or timeout is reached.

    Args:
        queue_name: Name of the queue to monitor
        max_size: Maximum allowed queue size (defaults to settings.max_queue_size)
        timeout: Maximum time to wait in seconds (None = wait forever)
        check_interval: Time between checks (defaults to settings.queue_check_interval)

    Returns:
        True if queue has space, False if timeout was reached
    """
    if max_size is None:
        max_size = settings.max_queue_size
    if check_interval is None:
        check_interval = settings.queue_check_interval
    
    start_time = time.time()
    
    while True:
        if can_enqueue(queue_name, max_size):
            return True
        
        if timeout is not None:
            elapsed = time.time() - start_time
            if elapsed >= timeout:
                logger.warning(
                    "Timeout waiting for queue space",
                    queue=queue_name,
                    timeout=timeout,
                    elapsed=elapsed,
                )
                return False
        
        current_size = get_queue_size(queue_name)
        logger.info(
            "Waiting for queue space",
            queue=queue_name,
            current_size=current_size,
            max_size=max_size,
            check_interval=check_interval,
        )
        time.sleep(check_interval)


def enqueue_with_limit(
    task_func,
    args: tuple = (),
    kwargs: Optional[dict] = None,
    queue_name: str = "default",
    max_size: Optional[int] = None,
    wait_timeout: Optional[float] = None,
) -> Optional[str]:
    """Enqueue a task only if the queue has space, waiting if necessary.

    Args:
        task_func: Celery task function to call
        args: Positional arguments for the task
        kwargs: Keyword arguments for the task
        queue_name: Queue to check for space
        max_size: Maximum allowed queue size
        wait_timeout: Maximum time to wait for space (None = wait forever)

    Returns:
        Task ID if enqueued, None if timeout was reached
    """
    if kwargs is None:
        kwargs = {}
    
    if wait_for_queue_space(queue_name, max_size, wait_timeout):
        result = task_func.delay(*args, **kwargs)
        return result.id
    
    return None


def get_active_task_count() -> int:
    """Get the number of tasks currently being processed by workers.

    Returns:
        Number of active tasks (approximate)
    """
    try:
        from sulekha.tasks.celery_app import celery_app
        
        inspect = celery_app.control.inspect()
        active = inspect.active()
        
        if active is None:
            return 0
        
        count = sum(len(tasks) for tasks in active.values())
        return count
    except Exception as e:
        logger.warning("Failed to get active task count", error=str(e))
        return 0


def get_scheduler_status() -> dict:
    """Get comprehensive scheduler status.

    Returns:
        Dictionary with queue sizes, active tasks, and limits
    """
    queue_sizes = get_all_queue_sizes()
    total_queued = sum(queue_sizes.values())
    active_count = get_active_task_count()
    
    return {
        "queues": queue_sizes,
        "total_queued": total_queued,
        "active_tasks": active_count,
        "max_queue_size": settings.max_queue_size,
        "can_enqueue": {
            queue: size < settings.max_queue_size
            for queue, size in queue_sizes.items()
        },
    }
