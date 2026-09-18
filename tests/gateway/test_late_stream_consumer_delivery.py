"""Regression coverage for stream consumers created after slow turn setup."""

import asyncio
from typing import Any

import pytest

from gateway.run_turn import GatewayTurnMixin


@pytest.mark.asyncio
async def test_stream_consumer_remains_available_past_three_minutes(monkeypatch):
    """Context compression may delay consumer creation well beyond the former 10s poll cap."""
    holder: list[Any] = [None]

    class Consumer:
        def __init__(self):
            self.ran = False

        async def run(self):
            self.ran = True

    consumer = Consumer()
    sleep_calls = 0

    async def delayed_creation_sleep(_delay):
        nonlocal sleep_calls
        sleep_calls += 1
        # Simulate more than three minutes of 50 ms polls without making the
        # test wait.  The former implementation stopped after only 200 polls.
        if sleep_calls == 3601:
            holder[0] = consumer

    monkeypatch.setattr(asyncio, "sleep", delayed_creation_sleep)

    runner = GatewayTurnMixin.__new__(GatewayTurnMixin)
    await runner._run_agent_stream_consumer_task(holder)

    assert sleep_calls == 3601
    assert consumer.ran is True


@pytest.mark.asyncio
async def test_stream_consumer_wait_is_cancelled_with_owning_turn():
    """The cancellation-bounded wait must not leak after turn cleanup."""
    runner = GatewayTurnMixin.__new__(GatewayTurnMixin)
    task = asyncio.create_task(runner._run_agent_stream_consumer_task([None]))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
