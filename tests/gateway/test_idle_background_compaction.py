"""Behavioral contract for post-turn idle background compaction."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

import pytest

from gateway.idle_compaction import IdleCompactionCoordinator
from gateway.run_turn_runner import TurnRunner


@pytest.mark.asyncio
async def test_disabled_does_not_create_task():
    coordinator = IdleCompactionCoordinator()
    called = False

    async def worker(_record):
        nonlocal called
        called = True

    assert coordinator.schedule("session", delay_seconds=0, watermark=1, worker=worker) is None
    await asyncio.sleep(0)
    assert coordinator.tasks == {}
    assert called is False


@pytest.mark.asyncio
async def test_schedule_coalesces_one_delayed_task_per_session():
    coordinator = IdleCompactionCoordinator()
    release = asyncio.Event()
    calls = []

    async def worker(record):
        calls.append(record.watermark)
        await release.wait()

    first = coordinator.schedule("session", delay_seconds=60, watermark=1, worker=worker)
    second = coordinator.schedule("session", delay_seconds=0.01, watermark=2, worker=worker)
    assert first is not None and second is not None and first is not second
    await asyncio.sleep(0)
    assert first.cancelled()
    assert list(coordinator.tasks) == ["session"]
    await asyncio.sleep(0.03)
    assert calls == [2]
    release.set()
    await second


@pytest.mark.asyncio
async def test_inbound_cancel_is_synchronous_and_worker_never_starts():
    coordinator = IdleCompactionCoordinator()
    started = asyncio.Event()

    async def worker(_record):
        started.set()

    task = coordinator.schedule("session", delay_seconds=60, watermark=1, worker=worker)
    assert coordinator.cancel("session") is True
    await asyncio.sleep(0)
    assert task is not None and task.cancelled()
    assert not started.is_set()
    assert coordinator.tasks == {}


@pytest.mark.asyncio
async def test_stale_tip_is_skipped_at_wake():
    tip = 10
    called = False
    coordinator = IdleCompactionCoordinator(current_watermark=lambda _record: tip)

    async def worker(_record):
        nonlocal called
        called = True

    task = coordinator.schedule("session", delay_seconds=0.01, watermark=9, worker=worker)
    assert task is not None
    await task
    assert called is False


@pytest.mark.asyncio
async def test_shutdown_cancels_all_delayed_tasks():
    coordinator = IdleCompactionCoordinator()
    started = asyncio.Event()

    async def worker(_record):
        started.set()

    tasks = [
        coordinator.schedule(key, delay_seconds=60, watermark=1, worker=worker)
        for key in ("a", "b")
    ]
    await coordinator.shutdown()
    assert coordinator.tasks == {}
    assert all(task is not None and task.cancelled() for task in tasks)
    assert not started.is_set()


def test_gateway_turn_agent_disables_legacy_foreground_idle_compaction():
    """The same opt-in knob schedules post-turn work; it must not also block the next turn."""

    class FakeAgent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.compression_idle_compact_after_seconds = 300

    source = SimpleNamespace(
        user_id="u", user_id_alt=None, user_name="Ricci", chat_id="c",
        chat_name="chat", chat_type="private", thread_id=None,
    )
    ctx = SimpleNamespace(
        AIAgent=FakeAgent,
        user_config={"compression": {"idle_compact_after_seconds": 300}},
        enabled_toolsets=None,
        disabled_toolsets=None,
        session_id="sid",
        session_key="telegram:c",
        source=source,
    )
    runner = SimpleNamespace(
        _prefill_messages=None,
        _service_tier="auto",
        _session_db=None,
        _refresh_fallback_model=lambda: None,
    )
    turn_runner: Any = object.__new__(TurnRunner)
    turn_runner._ctx = ctx
    turn_runner._runner = runner

    agent = turn_runner._build_fresh_agent(
        {"model": "m", "runtime": {}, "request_overrides": {}},
        "telegram", "", 80, None,
        {"only": None, "ignore": None, "order": None, "sort": None,
         "require_parameters": False, "data_collection": None},
        False,
    )

    assert agent.compression_idle_compact_after_seconds == 0
