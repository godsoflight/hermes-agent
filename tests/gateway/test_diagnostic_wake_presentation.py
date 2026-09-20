"""Diagnostic-only wakes execute while their unsolicited free-form reply stays private."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


class Adapter(BasePlatformAdapter):
    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {"name": "fixture", "type": "dm"}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="sent")


@pytest.mark.asyncio
@pytest.mark.parametrize("suppressed", [False, True])
async def test_diagnostic_wake_executes_without_final_or_error_echo(tmp_path, monkeypatch, suppressed):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(f"display: {{suppress_warning_notifications: {str(suppressed).lower()}}}")
    adapter = Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.sent = []
    adapter._start_typing_refresh = lambda *a: None
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()
    adapter._fire_post_delivery_callback = AsyncMock()
    adapter._flush_text_debounce_now = AsyncMock()
    adapter._finish_session_task = lambda *a: None
    adapter.send_final_ledgered = AsyncMock(return_value=(SendResult(success=True), adapter))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    event = MessageEvent(text="internal diagnostic", source=source, internal=True,
                         metadata={"notification_category": "diagnostic"})
    adapter._message_handler = AsyncMock(return_value="I repeat the technical diagnostic")
    await adapter._process_message_background(event, "session")
    assert adapter._message_handler.await_count == 1
    assert adapter.send_final_ledgered.await_count == (0 if suppressed else 1)
    adapter._message_handler = AsyncMock(side_effect=RuntimeError("provider detail"))
    await adapter._process_message_background(event, "session")
    assert bool(adapter.sent) is not suppressed
    assert adapter._run_processing_hook.await_args.args[-1].value == "failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("runner_wired, expected_starts", [(False, 1), (True, 0)])
async def test_runner_wiring_alone_defers_eager_processing_start(
    tmp_path, monkeypatch, runner_wired, expected_starts
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
    adapter.sent = []
    adapter._processing_start_deferred_to_runner = runner_wired
    adapter._start_typing_refresh = lambda *a: None
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()
    adapter._fire_post_delivery_callback = AsyncMock()
    adapter._flush_text_debounce_now = AsyncMock()
    adapter._finish_session_task = lambda *a: None
    adapter.send_final_ledgered = AsyncMock(return_value=(SendResult(success=True), adapter))
    adapter._message_handler = AsyncMock(return_value="done")
    event = MessageEvent(
        text="hello", source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat")
    )

    await adapter._process_message_background(event, "session")

    starts = [
        call for call in adapter._run_processing_hook.await_args_list
        if call.args and call.args[0] == "on_processing_start"
    ]
    assert len(starts) == expected_starts
