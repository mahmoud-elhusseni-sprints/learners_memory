"""The beat schedule must not dispatch work that can only fail.

profile.recompute_profile raises NotImplementedError until the synthesizer
exists, so nothing on the schedule may reach it; otherwise every stale profile
retries and dead-letters on every tick.
"""
from __future__ import annotations

from learner_memory.workers.celery_app import celery_app

UNIMPLEMENTED_PROFILE_TASKS = {"profile.refresh_stale_profiles", "profile.recompute_profile"}


def test_beat_schedule_does_not_dispatch_the_unimplemented_profile_recompute():
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

    assert scheduled.isdisjoint(UNIMPLEMENTED_PROFILE_TASKS)


def test_beat_schedule_keeps_the_maintenance_jobs():
    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}

    assert scheduled == {"maintenance.retry_failed_documents", "maintenance.reconcile_vectors"}
