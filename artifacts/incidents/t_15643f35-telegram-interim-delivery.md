# t_15643f35 — Telegram interim delivery regression evidence

Date: 2026-09-17 PDT

## Outcome

Root cause is a local race in `GatewayTurnMixin._run_agent_stream_consumer_task` (`gateway/run_turn.py`). The bridge task waited only 200 × 50 ms (10 seconds) for the per-turn `StreamConsumer`, then returned permanently. Turn setup can exceed 10 seconds. Later commentary/tool-boundary callbacks still enqueue events, but no bridge remains to drain the queue to Telegram.

This is not caused by Telegram display configuration or by `platform_message_id` persistence semantics:

- `display.platforms.telegram.interim_assistant_messages` currently resolves to `true`.
- `gateway/run_turn.py` reads configuration per turn and passes the enabled value into `StreamConsumer`; it is not dependent on a gateway restart to see this setting.
- The incident's assistant/tool-boundary rows were persisted after the inbound Telegram row with `platform_message_id = NULL` and `display_kind = NULL`; this is consistent with generation/persistence occurring while the delivery bridge is absent. A null platform ID is evidence that no platform send ID was attached, not the suppression decision itself.

## Incident evidence (read-only)

Database: `/Users/ricci/.hermes/state.db`, queried with SQLite URI `mode=ro`.

- Inbound row: message `405850`, Telegram `platform_message_id = 24185`, content `Stick again`, timestamp `1789705053.0` (`2026-09-17 21:17:33 PDT`).
- First assistant tool boundary: message `405851`, timestamp `1789705063.330583` (10.331 seconds after inbound), `platform_message_id = NULL`, `display_kind = NULL`. Its reasoning includes the intended acknowledgement: `Yes—confirmed as a regression until proven otherwise...`.
- Later assistant boundaries: message `405855` at +21.732 seconds and message `405857` at +27.643 seconds; both have null platform/display IDs.
- Gateway log at `2026-09-17 21:18:14.967` shows turn setup was still processing prompt context (`AGENTS.md TRUNCATED`) more than 10 seconds after inbound.

The source's former 10-second waiter could therefore exit before the consumer was installed. The regression test reproduces this seam without Telegram/network traffic by simulating 3,601 × 50 ms polls (>3 minutes) before installing a fake consumer.

## Red-capable regression test

Added `tests/gateway/test_late_stream_consumer_delivery.py::test_stream_consumer_remains_available_past_three_minutes`.

The pre-fix implementation cannot satisfy this test: it returns after poll 200, before the fake consumer appears at poll 3,601, leaving `consumer.ran` false. The current implementation remains cancellation-bounded for the full turn and runs the late consumer.

## Repair

Changed `_run_agent_stream_consumer_task` from a fixed 200-iteration wait to cancellation-bounded waiting. Its owning `_run_agent` routine already cancels the bridge in `finally`, so cancellation—not a setup-duration guess—is the correct lifetime bound.

Exact implementation delta:

```diff
-        for _ in range(200):
-            if stream_consumer_holder[0] is not None:
-                await stream_consumer_holder[0].run()
-                return
+        while stream_consumer_holder[0] is None:
             await asyncio.sleep(0.05)
+        await stream_consumer_holder[0].run()
```

Config delta: none. `hermes config get display.platforms.telegram.interim_assistant_messages` returned `true`, and `gateway/run_turn.py` resolves this setting per turn. The failure was the expired delivery bridge, not a disabled configuration flag.

No gateway restart was performed; the running gateway has therefore not loaded this working-tree repair.

## Green verification

Parent verification on the current tree:

```console
$ scripts/run_tests.sh \
  tests/gateway/test_late_stream_consumer_delivery.py \
  tests/gateway/test_silent_partial_delivery.py \
  tests/tui_gateway/test_interim_assistant_callback.py \
  tests/gateway/test_display_config.py \
  tests/gateway/test_run_progress_topics.py \
  tests/gateway/test_interim_only_consumer_warning.py \
  tests/gateway/test_telegram_final_delivery.py -q

=== Summary: 7 files, 100 tests passed, 0 failed (100% complete) in 39.6s (36 workers) ===
```

An independent cleanup probe also reported:

```text
stream_done=True stream_cancelled=True
tracking_done=True tracking_cancelled=True
pending_tasks=0
```

`git diff --check` passed. Ruff was not available in the repository venv (`No module named ruff`).

## Files changed

- `gateway/run_turn.py`
- `tests/gateway/test_late_stream_consumer_delivery.py`
- `artifacts/incidents/t_15643f35-telegram-interim-delivery.md`

## Safety constraints observed

No Telegram messages or other external test traffic were sent. The gateway was not restarted. No credentials/config were altered. No paid fallback model was used. No Kanban card was created, and incident `t_15643f35` was not closed.
