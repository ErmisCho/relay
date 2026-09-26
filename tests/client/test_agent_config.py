"""Dry-run render of config/elevenlabs/agent.json (no network, no ElevenLabs account)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from elevenlabs.types import AgentPlatformSettingsRequestModel, ConversationalConfig

from relay.config import Settings

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "apply_agent_config.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("apply_agent_config", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_substitutes_placeholders_and_redacts_secrets() -> None:
    script = _load_script()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        delegator_public_url="https://relay-tunnel.example/",
        delegator_shared_secret="shared-secret-value",
        elevenlabs_api_key="xi-api-key-value",
        silence_timeout_s=45,
    )

    output = script.render_dry_run(settings)

    assert "${" not in output
    assert "shared-secret-value" not in output
    assert "xi-api-key-value" not in output
    payload = json.loads(output)
    agent = payload["conversation_config"]["agent"]
    custom_llm = agent["prompt"]["custom_llm"]
    assert custom_llm["url"] == "https://relay-tunnel.example/v1"
    assert custom_llm["api_key"] == {"secret_id": script.REDACTED}
    assert payload["conversation_config"]["turn"]["silence_end_call_timeout"] == 45
    assert payload["platform_settings"]["overrides"]["custom_llm_extra_body"] is True

    # The SDK models --apply sends must keep the fields relay depends on.
    config = ConversationalConfig.model_validate(payload["conversation_config"])
    assert config.agent and config.agent.prompt and config.agent.prompt.custom_llm
    assert config.agent.prompt.custom_llm.url == "https://relay-tunnel.example/v1"
    assert config.agent.prompt.llm == "custom-llm"
    assert config.agent.prompt.built_in_tools and config.agent.prompt.built_in_tools.end_call
    assert config.tts and config.tts.model_id == "eleven_flash_v2_5"
    platform = AgentPlatformSettingsRequestModel.model_validate(payload["platform_settings"])
    assert platform.overrides and platform.overrides.custom_llm_extra_body is True


@pytest.mark.parametrize(
    ("url", "secret"),
    [
        ("https://relay-tunnel.example", "dev-secret-change-me"),
        ("http://localhost:8000", "a-real-secret"),
        ("http://127.0.0.1:8000", "a-real-secret"),
    ],
)
def test_apply_refuses_dev_secret_and_unreachable_url(url: str, secret: str) -> None:
    script = _load_script()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None, delegator_public_url=url, delegator_shared_secret=secret
    )
    with pytest.raises(SystemExit):
        script.check_apply_settings(settings)
    script.check_apply_settings(
        Settings(  # type: ignore[call-arg]
            _env_file=None,
            delegator_public_url="https://relay-tunnel.example",
            delegator_shared_secret="a-real-secret",
        )
    )


def test_existing_secret_is_found_on_a_later_page() -> None:
    script = _load_script()

    class Secret:
        def __init__(self, name: str, secret_id: str) -> None:
            self.name, self.secret_id = name, secret_id

    class Page:
        def __init__(self, secrets: list[Secret], next_cursor: str | None) -> None:
            self.secrets, self.next_cursor = secrets, next_cursor

    class Secrets:
        def __init__(self) -> None:
            self.updated: list[str] = []
            self.created = 0

        def list(self, *, search: str, cursor: str | None) -> Page:
            assert search == script.SECRET_NAME
            if cursor is None:
                return Page([Secret(script.SECRET_NAME + "_old", "wrong")], "page2")
            return Page([Secret(script.SECRET_NAME, "sec_2")], None)

        def update(self, secret_id: str, *, name: str, value: str) -> None:
            self.updated.append(secret_id)

        def create(self, *, name: str, value: str) -> Any:
            self.created += 1

    secrets = Secrets()
    client = type("C", (), {"conversational_ai": type("A", (), {"secrets": secrets})()})()

    assert script._ensure_secret(client, "v") == "sec_2"
    assert secrets.updated == ["sec_2"] and secrets.created == 0


def test_agent_is_private_signed_url_only() -> None:
    """A public agent (auth unset) lets anyone with the agent id spend our minutes and
    reach the Delegator through ElevenLabs; our client always uses a signed URL."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        delegator_public_url="https://relay-tunnel.example/",
        delegator_shared_secret="shared-secret-value",
    )
    payload = json.loads(_load_script().render_dry_run(settings))
    platform = AgentPlatformSettingsRequestModel.model_validate(payload["platform_settings"])
    assert platform.auth is not None and platform.auth.enable_auth is True
    assert not platform.auth.allowlist
