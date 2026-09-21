"""Processing-hook parity for queued follow-up turns.

A message that arrives while a turn is already running is parked in the
adapter's ``_pending_messages`` slot and drained *in-band* by
``GatewayRunner._run_agent`` rather than by
``BasePlatformAdapter._process_message_background``.  The runner-side drain
must still fire the ``on_processing_start`` / ``on_processing_complete``
lifecycle hooks, otherwise every platform that renders a read-receipt
reaction from those hooks (Slack 👀, Discord, Telegram, Feishu, Matrix,
Signal, ...) silently skips the acknowledgement for mid-turn messages.
"""

import asyncio
import importlib
import sys
import types
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.session import SessionSource


class HookRecordingAdapter(BasePlatformAdapter):
    """Adapter that records the processing-hook lifecycle it is driven through."""

    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.started: list = []
        self.completed: list = []

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}

    async def on_processing_start(self, event: MessageEvent) -> None:
        self.started.append(getattr(event, "message_id", None))

    async def on_processing_complete(self, event, outcome) -> None:
        self.completed.append((getattr(event, "message_id", None), outcome))


class DeferredHookRecordingAdapter(HookRecordingAdapter):
    _processing_start_deferred_to_runner = True


class _TwoTurnAgent:
    calls: list = []

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
        type(self).calls.append(message)
        return {
            "final_response": f"done-{len(type(self).calls)}",
            "messages": [],
            "api_calls": 1,
        }


class _RaisingSecondTurnAgent:
    calls: list = []

    def __init__(self, **kwargs):
        self.tools = []

    def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
        type(self).calls.append(message)
        if len(type(self).calls) >= 2:
            raise RuntimeError("boom in the queued follow-up turn")
        return {
            "final_response": "done-1",
            "messages": [],
            "api_calls": 1,
        }


class _CancellingSecondTurnAgent(_RaisingSecondTurnAgent):
    def run_conversation(self, message, conversation_history=None, task_id=None, **_kwargs):
        type(self).calls.append(message)
        if len(type(self).calls) >= 2:
            raise asyncio.CancelledError()
        return {
            "final_response": "done-1",
            "messages": [],
            "api_calls": 1,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _install_fake_agent(monkeypatch, tmp_path, agent_cls):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"}
    )


SESSION_KEY = "agent:main:telegram:dm:4242"


@pytest.mark.asyncio
@pytest.mark.parametrize("diagnostic_last", [False, True])
async def test_queue_terminal_presentation_belongs_to_last_turn(monkeypatch, tmp_path, diagnostic_last):
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)
    (tmp_path / "config.yaml").write_text("display: {suppress_warning_notifications: true}")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="follow-up", source=_source(), internal=diagnostic_last,
        metadata={"notification_category": "diagnostic"} if diagnostic_last else {}, message_id="queued")
    result = await runner._run_agent(message="first", context_prompt="", history=[], source=_source(),
        session_id="queue-policy", session_key=SESSION_KEY,
        persist_user_display_metadata=None if diagnostic_last else {"notification_category": "diagnostic"})
    assert _TwoTurnAgent.calls == ["first", "follow-up"]
    assert result["final_response"] == "done-2"
    assert result["_notification_reply_muted"] is diagnostic_last
    from gateway.warning_notifications import diagnostic_wake_muted
    outer = MessageEvent(text="first", source=_source(), internal=not diagnostic_last)
    outer._notification_reply_muted = result["_notification_reply_muted"]
    assert diagnostic_wake_muted(outer) is diagnostic_last


def _source():
    return SessionSource(platform=Platform.TELEGRAM, chat_id="4242", chat_type="dm")


async def _async_result(value):
    return value


@pytest.mark.asyncio
async def test_deferred_completion_requires_durable_admission():
    """A command/rejection that never got a runner start receipt must not get
    a synthetic completion receipt from the adapter."""
    adapter = DeferredHookRecordingAdapter()

    async def command_handler(_event):
        return "command response"

    adapter._message_handler = command_handler
    event = MessageEvent(
        text="/status", message_type=MessageType.TEXT, source=_source(), message_id="command")
    await adapter._process_message_background(event, SESSION_KEY)

    assert adapter.started == []
    assert adapter.completed == []


@pytest.mark.asyncio
async def test_queued_followup_fires_processing_hooks(monkeypatch, tmp_path):
    """The runner-drained follow-up gets the same start/complete hooks as a
    message that arrives while the session is idle."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-1",
    )

    result = await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks",
        session_key=SESSION_KEY,
    )

    # The follow-up really did run in-band.
    assert result["final_response"] == "done-2"
    assert _TwoTurnAgent.calls == ["the first turn", "the follow-up"]

    # ...and it was acknowledged through the lifecycle hooks.
    assert adapter.started == ["queued-1"]
    assert adapter.completed == [("queued-1", ProcessingOutcome.SUCCESS)]


@pytest.mark.asyncio
async def test_queued_followup_clears_durable_marker_only_after_final_send(monkeypatch, tmp_path):
    """The outer adapter owns delivery for an in-band follow-up, so the queued
    turn's durable marker must survive model completion until that send ends."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    order = []
    adapter = HookRecordingAdapter()
    heartbeat_acceptance = importlib.import_module("gateway.run_heartbeat_acceptance")
    monkeypatch.setattr(heartbeat_acceptance, "heartbeat_owner_is_current", lambda *_args: True)
    runner = _make_runner(adapter)
    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the durable follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-durable",
    )

    async def begin_durable(event, _source, _session_key):
        assert event._gateway_delivery_managed is True

        async def clear_after_delivery():
            order.append("clear")

        event._gateway_durable_turn_completion = clear_after_delivery
        return True

    async def handle(event):
        prepared = runner._PreparedTurn(
            [], "", "the first turn", "the first turn", None, "user",
            "sess-durable-followup", "owner",
        )
        runner._hmwa_resolve_session = lambda *_args: _async_result(
            (_source(), SimpleNamespace(session_id="sess-durable-followup"), SESSION_KEY)
        )
        runner._hmwa_prepare_turn = lambda *_args: _async_result((prepared, {}))
        runner.hooks.emit = lambda *_args: _async_result(None)
        runner._hmwa_stop_typing_for_turn = lambda *_args: _async_result(None)
        runner._is_session_run_current = lambda *_args: True
        runner._hmwa_shape_agent_response = lambda *_args, **_kwargs: _async_result(
            (_args[0]["final_response"], False, [])
        )
        runner._hmwa_prepend_reasoning = lambda _result, response, *_args: response
        runner._hmwa_runtime_footer_line = lambda *_args: None
        runner._hmwa_post_turn_hooks = lambda *_args: _async_result(None)
        runner._hmwa_classify_turn_failure = lambda *_args: (False, False, False)
        runner._hmwa_compression_exhaustion_reset = (
            lambda _result, response, session, *_args: _async_result((response, session))
        )
        runner._hmwa_persist_turn_transcript = lambda **_kwargs: _async_result(None)
        runner._schedule_idle_compaction_after_turn = lambda *_args: None
        runner._hmwa_deliver_turn_response = lambda *_args: _async_result(_args[6])
        runner._clear_session_env = lambda *_args: None
        runner._reply_anchor_for_event = lambda *_args: None
        return await runner._handle_message_with_agent(event, _source(), SESSION_KEY, 1)

    async def send(chat_id, content, reply_to=None, metadata=None):
        order.append("send")
        return SendResult(success=True, message_id="sent-durable")

    runner._begin_durable_turn_processing = begin_durable
    adapter._message_handler = handle
    monkeypatch.setattr(adapter, "send", send)
    outer = MessageEvent(
        text="the first turn",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="outer-durable",
    )

    await adapter._process_message_background(outer, SESSION_KEY)

    assert order == ["send", "clear"]


@pytest.mark.asyncio
async def test_queued_followup_failure_completes_the_hook(monkeypatch, tmp_path):
    """A follow-up turn that blows up still closes its hook, so a platform
    never strands a 'still working' marker on the user's message."""
    _RaisingSecondTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _RaisingSecondTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the doomed follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-2",
    )

    durable_clears = []

    async def begin_durable(event, _source, _session_key):
        async def clear_after_failure():
            durable_clears.append(event.message_id)

        event._gateway_durable_turn_completion = clear_after_failure
        await adapter._run_processing_hook("on_processing_start", event)
        return True

    runner._begin_durable_turn_processing = begin_durable

    with pytest.raises(RuntimeError):
        await runner._run_agent(
            message="the first turn",
            context_prompt="",
            history=[],
            source=_source(),
            session_id="sess-hooks-failure",
            session_key=SESSION_KEY,
        )

    assert adapter.started == ["queued-2"]
    assert adapter.completed == [("queued-2", ProcessingOutcome.FAILURE)]
    assert durable_clears == ["queued-2"]


@pytest.mark.asyncio
async def test_queued_followup_cancellation_clears_durable_marker(monkeypatch, tmp_path):
    _CancellingSecondTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _CancellingSecondTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)
    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the cancelled follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-cancelled",
    )
    durable_clears = []

    async def begin_durable(event, _source, _session_key):
        async def clear_after_cancellation():
            durable_clears.append(event.message_id)

        event._gateway_durable_turn_completion = clear_after_cancellation
        await adapter._run_processing_hook("on_processing_start", event)
        return True

    runner._begin_durable_turn_processing = begin_durable

    with pytest.raises(asyncio.CancelledError):
        await runner._run_agent(
            message="the first turn",
            context_prompt="",
            history=[],
            source=_source(),
            session_id="sess-hooks-cancelled",
            session_key=SESSION_KEY,
        )

    assert durable_clears == ["queued-cancelled"]
    assert adapter.started == ["queued-cancelled"]
    assert adapter.completed == [("queued-cancelled", ProcessingOutcome.FAILURE)]


@pytest.mark.asyncio
async def test_synthetic_followup_is_not_acknowledged(monkeypatch, tmp_path):
    """Drains with no inbound platform message — /goal continuations, wake-ups,
    CLI hand-offs — carry no message_id and must stay silent: there is nothing
    on the platform to react to."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="synthetic continuation",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id=None,
    )

    result = await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks-synthetic",
        session_key=SESSION_KEY,
    )

    # It still ran — we only suppressed the acknowledgement, not the turn.
    assert result["final_response"] == "done-2"
    assert _TwoTurnAgent.calls == ["the first turn", "synthetic continuation"]

    assert adapter.started == []
    assert adapter.completed == []


@pytest.mark.asyncio
async def test_raw_envelope_only_followup_is_acknowledged(monkeypatch, tmp_path):
    """Signal never sets message_id — its hook keys off the raw envelope
    (sender + timestamp_ms) — and Discord's reads raw_message. An event
    carrying only a raw envelope is still a real inbound message."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = HookRecordingAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="signal-shaped follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id=None,
        raw_message={"sender": "+15550100", "timestamp_ms": 1700000000000},
    )

    await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks-raw",
        session_key=SESSION_KEY,
    )

    assert adapter.started == [None]
    assert adapter.completed == [(None, ProcessingOutcome.SUCCESS)]


class CompleteOnlyAdapter(HookRecordingAdapter):
    """Google Chat and webhook implement on_processing_complete WITHOUT
    on_processing_start; theirs is end-of-cycle teardown (reap the typing
    card / end the delivery session), not a reaction."""

    on_processing_start = BasePlatformAdapter.on_processing_start


@pytest.mark.asyncio
async def test_complete_only_adapter_is_left_alone(monkeypatch, tmp_path):
    """We bracket, so both halves must belong to us. An adapter that only
    implements the completion half must not be handed a completion here: at
    this point the follow-up's reply has not been delivered yet, so its
    teardown would fire against a live turn."""
    _TwoTurnAgent.calls = []
    _install_fake_agent(monkeypatch, tmp_path, _TwoTurnAgent)

    adapter = CompleteOnlyAdapter()
    runner = _make_runner(adapter)

    adapter._pending_messages[SESSION_KEY] = MessageEvent(
        text="the follow-up",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="queued-3",
    )

    result = await runner._run_agent(
        message="the first turn",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="sess-hooks-complete-only",
        session_key=SESSION_KEY,
    )

    assert result["final_response"] == "done-2"
    assert adapter.completed == []
