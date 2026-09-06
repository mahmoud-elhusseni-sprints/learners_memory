"""Celery app: queues, routes and a deliberately small beat schedule."""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from learner_memory.core.config import get_settings
from learner_memory.core.logging import configure_logging

settings = get_settings()

celery_app = Celery(
    "learner_memory",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=[
        "learner_memory.workers.tasks.ingest",
        "learner_memory.workers.tasks.profile",
        "learner_memory.workers.tasks.maintenance",
    ],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_default_queue="ingest",
    task_routes={
        "ingest.*": {"queue": "ingest"},
        "extract.*": {"queue": "extract"},
        "profile.*": {"queue": "profile"},
        "maintenance.*": {"queue": "maintenance"},
    },
    result_expires=86400,
    broker_transport_options={"visibility_timeout": 3600},
)

# Only what the system genuinely cannot run without. Decay, expiry, archive
# mirroring and full resynthesis are deliberately left out until there is real
# data to justify their cost — they exist as on-demand admin tasks instead.
celery_app.conf.beat_schedule = {
    "refresh-stale-profiles": {
        # Picks up debounced recomputes and anything a crashed worker dropped.
        "task": "profile.refresh_stale_profiles",
        "schedule": crontab(minute="*/15"),
    },
    "retry-failed-documents": {
        # Bounded retry of documents that failed extraction (transient LLM/storage).
        "task": "maintenance.retry_failed_documents",
        "schedule": crontab(minute="*/30"),
    },
    "reconcile-vectors": {
        # Repairs ledger <-> Qdrant drift; the one guard on our only dual write.
        "task": "maintenance.reconcile_vectors",
        "schedule": crontab(minute=0),
    },
}


@worker_process_init.connect
def _init_worker(**_):
    from learner_memory.extractors.registry import load_extractors

    configure_logging(settings.log_level)
    load_extractors()
