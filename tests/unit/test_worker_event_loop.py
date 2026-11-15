"""Tasks in one worker process must share one event loop.

Process-wide async resources (DB pool, Qdrant client, LLM semaphore) bind to
the loop that first used them, so a later task on a fresh loop would hit
objects tied to a closed loop.
"""
from __future__ import annotations

import asyncio

import pytest

from learner_memory.workers.tasks import base


@pytest.fixture(autouse=True)
def fresh_worker_loop(monkeypatch):
    monkeypatch.setattr(base, "_worker_loop", None)
    yield
    if base._worker_loop is not None:
        base._worker_loop[1].close()


async def _running_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


def test_consecutive_tasks_run_on_the_same_open_loop():
    first = base.run_async(_running_loop())
    second = base.run_async(_running_loop())

    assert first is second
    assert not first.is_closed()


def test_loop_bound_resource_from_one_task_is_usable_by_the_next():
    # A contended wait binds the queue to the running loop, the same way a
    # pooled connection binds to the loop that opened it.
    shared: dict[str, asyncio.Queue] = {}

    async def first_task():
        shared["queue"] = asyncio.Queue()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(shared["queue"].get(), timeout=0.01)

    async def second_task():
        asyncio.get_running_loop().call_later(0.01, shared["queue"].put_nowait, "ready")
        return await asyncio.wait_for(shared["queue"].get(), timeout=1)

    base.run_async(first_task())
    received = base.run_async(second_task())

    assert received == "ready"


def test_forked_child_gets_its_own_loop(monkeypatch):
    parent_loop = base.run_async(_running_loop())
    monkeypatch.setattr(base.os, "getpid", lambda: -1)

    child_loop = base.run_async(_running_loop())

    assert child_loop is not parent_loop
    parent_loop.close()
