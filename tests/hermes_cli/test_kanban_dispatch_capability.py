"""Capability preflight for Kanban worker dispatch.

A ready card must not be claimed until its assigned profile can resolve every
force-loaded skill and an inference runtime. Capability failures are durable,
actionable board state rather than short-lived worker crashes.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_dispatch_capability import check_profile_capabilities


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n",
        encoding="utf-8",
    )
    kb.init_db()
    return home


def test_missing_required_skill_blocks_before_claim(
    kanban_home: Path,
) -> None:
    spawned = []
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="requires unavailable capability",
            assignee="default",
            skills=["skill-that-does-not-exist-anywhere"],
        )
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda *args, **kwargs: spawned.append((args, kwargs)) or 1234,
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert spawned == []
    assert result.spawned == []
    assert result.capability_blocked == [task_id]
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert task.claim_lock is None
    assert "skill-that-does-not-exist-anywhere" in (task.result or task.last_failure_error or "") or any(
        "skill-that-does-not-exist-anywhere" in json.dumps(event.payload or {}) for event in events
    )
    blocked = [event for event in events if event.kind == "blocked"]
    assert blocked
    reason = str((blocked[-1].payload or {}).get("reason") or "")
    assert "required skill" in reason.lower()
    assert "default" in reason


def test_missing_provider_credentials_blocks_with_actionable_reason(
    kanban_home: Path,
) -> None:
    (kanban_home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: openrouter/auto\n",
        encoding="utf-8",
    )
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="needs auth", assignee="default")
        result = kbd.dispatch_once(conn, spawn_fn=lambda *_args: 1234)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.capability_blocked == [task_id]
    assert task is not None and task.status == "blocked"
    assert task.current_run_id is None
    assert task.consecutive_failures == 0
    reason = str(([e for e in events if e.kind == "blocked"][-1].payload or {}).get("reason") or "")
    assert "inference runtime" in reason.lower()
    assert "credential" in reason.lower()
    assert "default" in reason


def test_authorized_fallback_routes_once_and_preserves_task_overrides(
    kanban_home: Path,
) -> None:
    (kanban_home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n"
        "kanban:\n  capability_fallback_profiles:\n    default: capable\n",
        encoding="utf-8",
    )
    capable = kanban_home / "profiles" / "capable"
    capable.mkdir(parents=True)
    (capable / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n",
        encoding="utf-8",
    )
    skill_dir = capable / "skills" / "fallback-only-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: fallback-only-skill\ndescription: fallback test skill\n---\n\n# Fallback\n",
        encoding="utf-8",
    )
    spawned = []
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn,
            title="route me",
            assignee="default",
            skills=["fallback-only-skill"],
            model_override="local-test",
            provider_override="custom",
        )
        result = kbd.dispatch_once(
            conn,
            spawn_fn=lambda task, workspace: spawned.append((task, workspace)) or 1234,
        )
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.capability_rerouted == [(task_id, "default", "capable")]
    assert len(spawned) == 1
    spawned_task, _workspace = spawned[0]
    assert spawned_task.id == task_id
    assert spawned_task.assignee == "capable"
    assert spawned_task.model_override == "local-test"
    assert spawned_task.provider_override == "custom"
    assert task is not None and task.assignee == "capable" and task.status == "running"
    fallback_events = [event for event in events if event.kind == "capability_fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0].payload == {
        "from_profile": "default",
        "to_profile": "capable",
        "reason": "required_capability_unavailable",
    }


def test_capability_scope_does_not_leak_across_profiles(
    kanban_home: Path,
) -> None:
    capable = kanban_home / "profiles" / "capable"
    capable.mkdir(parents=True)
    (capable / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n",
        encoding="utf-8",
    )
    skill_dir = capable / "skills" / "profile-local-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: profile-local-skill\ndescription: profile scope test\n---\n\n# Local\n",
        encoding="utf-8",
    )
    task = kb.Task(
        id="scope-probe", title="scope", body=None, assignee="default", status="ready",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="scratch", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None, skills=["profile-local-skill"],
    )

    assert check_profile_capabilities(task, "default").ready is False
    assert check_profile_capabilities(task, "capable").ready is True
    assert check_profile_capabilities(task, "default").ready is False


def test_named_profile_does_not_borrow_launch_process_secret(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = kanban_home / "profiles" / "worker"
    worker.mkdir(parents=True)
    (worker / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  default: openrouter/auto\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "launch-profile-secret")
    task = kb.Task(
        id="secret-scope-probe", title="scope", body=None, assignee="worker", status="ready",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="scratch", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None,
    )

    result = check_profile_capabilities(task, "worker")

    assert result.ready is False
    assert "credential" in (result.reason or "").lower()


def test_skill_readiness_preflight_is_noninteractive_and_blocks_missing_setup(
    kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = kanban_home / "profiles" / "worker"
    worker.mkdir(parents=True)
    (worker / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n",
        encoding="utf-8",
    )
    skill_dir = worker / "skills" / "needs-setup"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: needs-setup\ndescription: setup probe\n"
        "required_environment_variables:\n  - PROFILE_ONLY_KEY\n---\n\n# Probe\n",
        encoding="utf-8",
    )
    prompted = []
    from tools import skills_tool

    monkeypatch.setattr(
        skills_tool,
        "_secret_capture_callback",
        lambda *args: prompted.append(args) or {"cancelled": True},
    )
    task = kb.Task(
        id="skill-readiness-probe", title="scope", body=None, assignee="worker", status="ready",
        priority=0, created_by=None, created_at=0, started_at=None, completed_at=None,
        workspace_kind="scratch", workspace_path=None, claim_lock=None, claim_expires=None,
        tenant=None, skills=["needs-setup"],
    )

    result = check_profile_capabilities(task, "worker")

    assert result.ready is False
    assert "profile_only_key" in (result.reason or "").lower()
    assert prompted == []


def test_existing_fallback_provenance_prevents_a_second_route(
    kanban_home: Path,
) -> None:
    (kanban_home / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n"
        "kanban:\n  capability_fallback_profiles:\n    capable: other\n",
        encoding="utf-8",
    )
    other = kanban_home / "profiles" / "other"
    other.mkdir(parents=True)
    (other / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n",
        encoding="utf-8",
    )
    skill_dir = other / "skills" / "second-hop-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: second-hop-skill\ndescription: second hop\n---\n\n# Second\n",
        encoding="utf-8",
    )
    capable = kanban_home / "profiles" / "capable"
    capable.mkdir(parents=True)
    (capable / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: local-test\n"
        "  base_url: http://127.0.0.1:9999/v1\n",
        encoding="utf-8",
    )
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="no chains", assignee="capable", skills=["second-hop-skill"])
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "capability_fallback", {
                "from_profile": "default", "to_profile": "capable",
                "reason": "required_capability_unavailable",
            })
        result = kbd.dispatch_once(conn, spawn_fn=lambda *_args: 1234)
        task = kb.get_task(conn, task_id)
        events = kb.list_events(conn, task_id)

    assert result.spawned == []
    assert result.capability_rerouted == []
    assert result.capability_blocked == [task_id]
    assert task is not None and task.assignee == "capable" and task.status == "blocked"
    assert len([event for event in events if event.kind == "capability_fallback"]) == 1


def test_zero_workers_with_executable_work_is_machine_detectable(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        task_id = kb.create_task(conn, title="queued", assignee="default")
        stalled = kbd.detect_liveness_stall(conn)

    assert stalled == [task_id]


def test_terminal_and_dependency_wait_are_not_liveness_stalls(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="parent", assignee="default")
        child = kb.create_task(conn, title="child", assignee="default", parents=[parent])
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
            conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (child,))
        stalled = kbd.detect_liveness_stall(conn)
        child_task = kb.get_task(conn, child)

    assert child_task is not None and child_task.status == "todo"
    assert stalled == []
