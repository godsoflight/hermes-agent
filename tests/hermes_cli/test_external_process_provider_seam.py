"""An external-process (ACP) provider can ship from outside this tree.

The known-provider gate, the binary, the argv and the env var names used to be
spelled out for one vendor, so a profile registered from a plugin died with
"Unknown provider" before any client was built. This registers a provider the
way a standalone package does — before importing ``hermes_cli`` — and walks the
real resolution path, asserting ``copilot-acp`` is unchanged alongside it.
"""

from __future__ import annotations

import os
import stat

import pytest

from providers import register_provider
from providers.base import ProviderProfile


class _AcmeACPProfile(ProviderProfile):
    def create_client(self, **kwargs):
        return ("acme-client", kwargs)

    def fetch_models(self, **kwargs):
        return None


ACME_PROFILE = _AcmeACPProfile(
        name="acme-acp",
        aliases=("acme",),
        display_name="Acme ACP",
        base_url="acp://acme",
        auth_type="external_process",
        process_command="acme-cli",
        process_args=("--acp",),
        process_command_env_vars=("ACME_CLI_PATH",),
        process_args_env_var="ACME_ACP_ARGS",
    )
register_provider(ACME_PROFILE)


@pytest.fixture
def fake_cli(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("acme-cli", "copilot", "custom-acme"):
        exe = bindir / name
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}")
    return bindir


def test_an_out_of_tree_external_process_provider_resolves_end_to_end(fake_cli, monkeypatch):
    # Pytest imports every selected module before running tests. If another selected
    # module imported hermes_cli.auth first, replay the same startup registration
    # hook so this test remains independent of collection order.
    from hermes_cli import auth as auth_mod
    if "acme-acp" not in auth_mod.PROVIDER_REGISTRY:
        auth_mod._register_plugin_provider(ACME_PROFILE)
    from hermes_cli.auth import PROVIDER_REGISTRY, resolve_external_process_provider_credentials, resolve_provider
    from hermes_cli.runtime_provider import resolve_runtime_provider

    assert PROVIDER_REGISTRY["acme"] is PROVIDER_REGISTRY["acme-acp"]
    assert PROVIDER_REGISTRY["acme-acp"].auth_type == "external_process"
    assert resolve_provider("acme") == "acme-acp"

    creds = resolve_external_process_provider_credentials("acme-acp")
    assert (creds["command"], creds["args"], creds["api_key"]) == (str(fake_cli / "acme-cli"), ["--acp"], "acme-acp")

    monkeypatch.setenv("ACME_CLI_PATH", str(fake_cli / "custom-acme"))
    monkeypatch.setenv("ACME_ACP_ARGS", "--acp=true --verbose")
    creds = resolve_external_process_provider_credentials("acme-acp")
    assert (creds["command"], creds["args"]) == (str(fake_cli / "custom-acme"), ["--acp=true", "--verbose"])

    runtime = resolve_runtime_provider(requested="acme", target_model="acme")
    assert (runtime["provider"], runtime["base_url"], runtime["source"]) == ("acme-acp", "acp://acme", "process")


def test_copilot_acp_launch_details_are_unchanged(fake_cli, monkeypatch):
    from hermes_cli.auth import resolve_external_process_provider_credentials
    from hermes_cli.runtime_provider import resolve_runtime_provider

    creds = resolve_external_process_provider_credentials("copilot-acp")
    assert creds["command"] == str(fake_cli / "copilot")
    assert (creds["args"], creds["api_key"], creds["base_url"]) == (["--acp", "--stdio"], "copilot-acp", "acp://copilot")

    monkeypatch.setenv("COPILOT_CLI_PATH", str(fake_cli / "custom-acme"))
    assert resolve_external_process_provider_credentials("copilot-acp")["command"] == str(fake_cli / "custom-acme")
    assert resolve_runtime_provider(requested="copilot-acp", target_model="x")["base_url"] == "acp://copilot"


def test_copilot_acp_launch_details_follow_active_multiplex_profile_scope(fake_cli, monkeypatch):
    from agent.secret_scope import is_multiplex_active, reset_secret_scope, set_multiplex_active, set_secret_scope
    from hermes_cli.auth import resolve_external_process_provider_credentials

    monkeypatch.setenv("HERMES_COPILOT_ACP_COMMAND", str(fake_cli / "copilot"))
    token = set_secret_scope({
        "HERMES_COPILOT_ACP_COMMAND": str(fake_cli / "custom-acme"),
        "HERMES_COPILOT_ACP_ARGS": "acp",
    })
    previous_multiplex_state = is_multiplex_active()
    set_multiplex_active(True)
    try:
        creds = resolve_external_process_provider_credentials("copilot-acp")
    finally:
        reset_secret_scope(token)
        set_multiplex_active(previous_multiplex_state)

    assert creds["command"] == str(fake_cli / "custom-acme")
    assert creds["args"] == ["acp"]
