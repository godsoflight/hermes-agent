"""Profile capability checks performed before Kanban worker claims.

The dispatcher uses this module at the last reversible point: after ordinary
spawn guards, but before a ready/review card is claimed. Checks execute in the
assigned profile's home and secret scope so a multiplexer cannot accidentally
borrow the launch profile's skills or credentials.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


@dataclass(frozen=True)
class CapabilityCheck:
    ready: bool
    reason: Optional[str] = None


def authorized_fallback_profile(profile: str) -> Optional[str]:
    """Return the one explicitly configured capability fallback for ``profile``.

    The policy is read from the dispatching home's user config, never from the
    task payload or the failing profile. Invalid/missing policy fails closed.
    """
    try:
        from hermes_cli.config_effective import load_user_config_effective
        from hermes_cli.profiles import normalize_profile_name

        cfg = load_user_config_effective(fail_closed=True) or {}
        policy = (cfg.get("kanban") or {}).get("capability_fallback_profiles")
        if not isinstance(policy, dict):
            return None
        source = normalize_profile_name(profile)
        raw_target = policy.get(source)
        if not isinstance(raw_target, str) or not raw_target.strip():
            return None
        target = normalize_profile_name(raw_target)
        return target if target != source else None
    except Exception:
        return None


@contextmanager
def _profile_scope(profile: str) -> Iterator[str]:
    from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
    from hermes_cli.profiles import resolve_profile_env
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    hermes_home = resolve_profile_env(profile)
    home_token = set_hermes_home_override(hermes_home)
    secret_token = set_secret_scope(build_profile_secret_scope(Path(hermes_home)))
    try:
        yield hermes_home
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)


def check_profile_capabilities(task, profile: str, *, extra_skills=()) -> CapabilityCheck:
    """Resolve a task's skills and inference runtime in ``profile``.

    No network request is made. A failure is returned as an operator-facing
    reason suitable for a durable ``kind='capability'`` block.
    """
    skills = list(dict.fromkeys([*(task.skills or ()), *extra_skills]))
    try:
        with _profile_scope(profile):
            if skills:
                from agent.skill_commands import build_preloaded_skills_prompt

                _prompt, _loaded, missing = build_preloaded_skills_prompt(skills, task_id=task.id)
                if missing:
                    names = ", ".join(missing)
                    return CapabilityCheck(
                        False,
                        f"Profile '{profile}' is missing required skill(s): {names}. "
                        f"Install or enable them for that profile, then unblock task {task.id}.",
                    )

            from hermes_cli.auth import has_usable_secret
            from hermes_cli.runtime_provider import resolve_runtime_provider

            runtime = resolve_runtime_provider(
                requested=task.provider_override,
                target_model=task.model_override,
            )
            if runtime.get("auth_error"):
                raise runtime["auth_error"]
            provider = str(runtime.get("provider") or task.provider_override or "auto")
            if provider in {"openrouter", "custom"} and not has_usable_secret(runtime.get("api_key")):
                raise RuntimeError(f"provider '{provider}' resolved without usable credentials")
    except Exception as exc:
        return CapabilityCheck(
            False,
            f"Profile '{profile}' cannot resolve the task inference runtime: {exc}. "
            f"Configure credentials/provider for that profile, then unblock task {task.id}.",
        )
    return CapabilityCheck(True)
