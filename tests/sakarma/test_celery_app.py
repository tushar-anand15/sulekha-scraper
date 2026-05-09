"""Tests for the SAKARMA Celery app configuration."""

from __future__ import annotations

import structlog

from sakarma.tasks.celery_app import SakarmaTask, celery_app


def _queue_names() -> set[str]:
    """Extract queue names from ``celery_app.conf.task_queues``."""
    queues = celery_app.conf.task_queues
    names: set[str] = set()
    if queues is None:
        return names
    # task_queues may be a tuple/list of kombu.Queue objects or a dict.
    if isinstance(queues, dict):
        names.update(queues.keys())
    else:
        for q in queues:
            # kombu.Queue exposes ``.name``; a dict-style entry would be a tuple.
            name = getattr(q, "name", None)
            if name is not None:
                names.add(name)
    return names


def test_celery_app_main_name() -> None:
    """The Celery app is named ``sakarma`` (not ``sulekha``)."""
    assert celery_app.main == "sakarma"


def test_celery_app_has_six_queues() -> None:
    """All six namespaced sakarma queues are registered."""
    expected = {
        "sakarma_default",
        "sakarma_discovery",
        "sakarma_manifest",
        "sakarma_fetch",
        "sakarma_reconcile",
        "sakarma_orchestrate",
    }
    assert expected.issubset(_queue_names())


def test_celery_app_has_five_task_routes() -> None:
    """The five module-namespaced task routes are configured."""
    routes = celery_app.conf.task_routes
    assert routes == {
        "sakarma.tasks.discovery.*": {"queue": "sakarma_discovery"},
        "sakarma.tasks.manifest.*": {"queue": "sakarma_manifest"},
        "sakarma.tasks.artifacts.*": {"queue": "sakarma_fetch"},
        "sakarma.tasks.reconciliation.*": {"queue": "sakarma_reconcile"},
        "sakarma.tasks.orchestrator.*": {"queue": "sakarma_orchestrate"},
    }


def test_discovery_task_routes_to_discovery_queue() -> None:
    """A synthetic task in ``sakarma.tasks.discovery`` routes to ``sakarma_discovery``."""
    route = celery_app.amqp.router.route({}, "sakarma.tasks.discovery.foo")
    queue = route["queue"]
    queue_name = getattr(queue, "name", queue)
    assert queue_name == "sakarma_discovery"


def test_manifest_task_routes_to_manifest_queue() -> None:
    """Manifest-namespaced tasks route to the manifest queue."""
    route = celery_app.amqp.router.route({}, "sakarma.tasks.manifest.build")
    queue = route["queue"]
    queue_name = getattr(queue, "name", queue)
    assert queue_name == "sakarma_manifest"


def test_default_task_base_class_is_sakarma_task() -> None:
    """``celery_app.Task`` is overridden to ``SakarmaTask``."""
    assert celery_app.Task is SakarmaTask


def test_task_acks_late_and_reject_on_worker_lost() -> None:
    """Reliability settings match the sulekha pattern."""
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.result_expires == 86400
    assert celery_app.conf.result_extended is True


def test_sakarma_task_on_failure_emits_structlog_event() -> None:
    """``on_failure`` logs an event with task name and exception class."""

    @celery_app.task(name="sakarma.tasks.discovery.synthetic_failure", base=SakarmaTask)
    def synthetic_failure() -> None:  # pragma: no cover — body never runs in this test
        raise RuntimeError("boom")

    exc = RuntimeError("boom")
    with structlog.testing.capture_logs() as logs:
        synthetic_failure.on_failure(
            exc,
            "task-id-123",
            args=(),
            kwargs={},
            einfo=None,
        )

    assert len(logs) == 1
    event = logs[0]
    assert event["event"] == "Task failed"
    assert event["task_name"] == "sakarma.tasks.discovery.synthetic_failure"
    assert event["task_id"] == "task-id-123"
    assert event["exception"] == "RuntimeError"
    assert event["log_level"] == "error"


def test_sakarma_task_on_retry_emits_structlog_event() -> None:
    """``on_retry`` logs a warning event with the exception class."""

    @celery_app.task(name="sakarma.tasks.discovery.synthetic_retry", base=SakarmaTask)
    def synthetic_retry() -> None:  # pragma: no cover
        raise ValueError("retry me")

    exc = ValueError("retry me")
    with structlog.testing.capture_logs() as logs:
        synthetic_retry.on_retry(
            exc,
            "task-id-retry",
            args=(),
            kwargs={},
            einfo=None,
        )

    assert any(
        e["event"] == "Task retrying"
        and e["task_name"] == "sakarma.tasks.discovery.synthetic_retry"
        and e["exception"] == "ValueError"
        and e["log_level"] == "warning"
        for e in logs
    )


def test_sakarma_task_on_success_emits_structlog_event() -> None:
    """``on_success`` logs an info event with the task name."""

    @celery_app.task(name="sakarma.tasks.discovery.synthetic_success", base=SakarmaTask)
    def synthetic_success() -> None:  # pragma: no cover
        return None

    with structlog.testing.capture_logs() as logs:
        synthetic_success.on_success(None, "task-id-ok", args=(), kwargs={})

    assert any(
        e["event"] == "Task succeeded"
        and e["task_name"] == "sakarma.tasks.discovery.synthetic_success"
        and e["log_level"] == "info"
        for e in logs
    )
