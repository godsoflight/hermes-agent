"""Coalesced delayed work used by gateway idle compaction.

The coordinator deliberately owns only asyncio task lifecycle.  The gateway worker
owns transcript/lease/compression guards, while inbound dispatch can synchronously
cancel a pending task without ever awaiting compaction.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any


logger = logging.getLogger("gateway.run")


@dataclasses.dataclass
class IdleCompactionRecord:
    session_key: str
    watermark: int | None
    payload: Any = None
    task: asyncio.Task[None] | None = None
    cancel_active: Callable[[], Any] | None = None


class IdleCompactionCoordinator:
    """Maintain at most one delayed idle-compaction task per resolved session."""

    def __init__(self, *, current_watermark: Callable[[IdleCompactionRecord], Any] | None = None) -> None:
        self.tasks: dict[str, IdleCompactionRecord] = {}
        self._current_watermark = current_watermark
        self._closing = False

    def schedule(
        self,
        session_key: str,
        *,
        delay_seconds: float,
        watermark: int | None,
        worker: Callable[[IdleCompactionRecord], Awaitable[None]],
        payload: Any = None,
    ) -> asyncio.Task[None] | None:
        """Replace this session's timer.  Zero is the opt-out and creates no task."""
        self.cancel(session_key)
        if self._closing or not session_key or delay_seconds <= 0:
            return None
        record = IdleCompactionRecord(
            session_key=session_key,
            watermark=None if watermark is None else int(watermark),
            payload=payload,
        )

        async def read_watermark() -> int:
            current = self._current_watermark(record)  # type: ignore[misc]
            if inspect.isawaitable(current):
                current = await current
            return int(current)

        async def delayed() -> None:
            try:
                if self._current_watermark is not None and record.watermark is None:
                    record.watermark = await read_watermark()
                await asyncio.sleep(delay_seconds)
                if self.tasks.get(session_key) is not record:
                    return
                if self._current_watermark is not None:
                    current = await read_watermark()
                    if current != record.watermark:
                        return
                await worker(record)
            except asyncio.CancelledError:
                raise
            finally:
                if self.tasks.get(session_key) is record:
                    self.tasks.pop(session_key, None)

        record.task = asyncio.create_task(delayed(), name=f"idle-compact:{session_key[:48]}")
        self.tasks[session_key] = record

        def consume_failure(done: asyncio.Task[None]) -> None:
            if done.cancelled():
                return
            try:
                exc = done.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                logger.warning(
                    "Idle background compaction failed for %s: %s",
                    session_key, exc, exc_info=(type(exc), exc, exc.__traceback__),
                )

        record.task.add_done_callback(consume_failure)
        return record.task

    def cancel(self, session_key: str) -> bool:
        """Cancel without awaiting; safe for the inbound fast path."""
        record = self.tasks.pop(session_key, None)
        if record is None:
            return False
        if record.cancel_active is not None:
            record.cancel_active()
        if record.task is not None:
            record.task.cancel()
        return True

    async def shutdown(self) -> None:
        """Prevent new work, cancel every task, and drain their cancellation."""
        self._closing = True
        records = list(self.tasks.values())
        self.tasks.clear()
        for record in records:
            if record.cancel_active is not None:
                record.cancel_active()
            if record.task is not None:
                record.task.cancel()
        await asyncio.gather(
            *(record.task for record in records if record.task is not None),
            return_exceptions=True,
        )
