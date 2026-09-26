"""Render and (optionally) apply the versioned ElevenLabs agent config.

    uv run python scripts/apply_agent_config.py            # dry run (default): print payload
    uv run python scripts/apply_agent_config.py --apply    # create or update the agent

`config/elevenlabs/agent.json` holds `${NAME}` placeholders, filled from relay settings:

- `DELEGATOR_PUBLIC_URL`: the public base URL of the Delegator (e.g. a tunnel URL). The custom
  LLM URL is `<base>/v1`; ElevenLabs appends `/chat/completions` (OpenAI base-URL convention).
- `DELEGATOR_SECRET_ID`: the id of an ElevenLabs workspace secret holding
  `DELEGATOR_SHARED_SECRET`. The secret value never appears in the JSON; `--apply` creates or
  updates the secret and references it by id.
- `SILENCE_TIMEOUT_S`: server-side `turn.silence_end_call_timeout`.

`--apply` creates the agent when `ELEVENLABS_AGENT_ID` is unset, else updates that agent, and
prints the agent id. It spends no agent minutes but does change the ElevenLabs account.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from relay.config import Settings, get_settings
from relay.delegator.auth import DEV_SECRET

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "elevenlabs" / "agent.json"
SECRET_NAME = "relay_delegator_shared_secret"
REDACTED = "<redacted>"
_PLACEHOLDER = re.compile(r"\$\{([A-Z0-9_]+)\}")
_UNREACHABLE_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def load_template(path: Path = CONFIG_PATH) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return data


def placeholder_values(settings: Settings, secret_id: str) -> dict[str, object]:
    return {
        "DELEGATOR_PUBLIC_URL": settings.delegator_public_url.rstrip("/"),
        "DELEGATOR_SECRET_ID": secret_id,
        "SILENCE_TIMEOUT_S": settings.silence_timeout_s,
    }


def render(node: Any, values: dict[str, object]) -> Any:
    """Substitute `${NAME}` placeholders recursively.

    A string that is exactly one placeholder takes the value's own type (so numbers stay
    numbers); embedded placeholders are formatted with `str`. Unknown names raise `KeyError`.
    """
    if isinstance(node, dict):
        return {key: render(value, values) for key, value in node.items()}
    if isinstance(node, list):
        return [render(item, values) for item in node]
    if isinstance(node, str):
        whole = _PLACEHOLDER.fullmatch(node)
        if whole:
            return values[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), node)
    return node


def redact(node: Any, secrets: set[str]) -> Any:
    """Replace secret ids and any literal secret value with `REDACTED`."""
    if isinstance(node, dict):
        return {
            key: REDACTED if key == "secret_id" else redact(value, secrets)
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [redact(item, secrets) for item in node]
    if isinstance(node, str) and node in secrets:
        return REDACTED
    return node


def render_dry_run(settings: Settings, template: dict[str, Any] | None = None) -> str:
    """The dry-run output: rendered payload, secrets redacted, no network access."""
    payload = render(template or load_template(), placeholder_values(settings, REDACTED))
    secrets = {s for s in (settings.delegator_shared_secret, settings.elevenlabs_api_key) if s}
    return json.dumps(redact(payload, secrets), indent=2)


def check_apply_settings(settings: Settings) -> None:
    """Refuse settings ElevenLabs cannot use: it calls the Delegator from its own servers.

    Unlike the Delegator's own startup check there is no dev opt-in here: the dev secret would
    be stored on the ElevenLabs account and guard a publicly reachable endpoint.
    """
    secret = settings.delegator_shared_secret
    if not secret or secret == DEV_SECRET:
        raise SystemExit("DELEGATOR_SHARED_SECRET is empty or the dev placeholder; set a real one")
    parts = urlsplit(settings.delegator_public_url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        raise SystemExit(f"DELEGATOR_PUBLIC_URL {settings.delegator_public_url!r} is not a URL")
    if host in _UNREACHABLE_HOSTS or host.startswith("127."):
        raise SystemExit(
            f"DELEGATOR_PUBLIC_URL host {host!r} is not reachable from ElevenLabs; "
            "use a public tunnel URL"
        )


def _find_secret_id(client: Any) -> str | None:
    """Look up the workspace secret by exact name across all result pages."""
    cursor: str | None = None
    while True:
        page = client.conversational_ai.secrets.list(search=SECRET_NAME, cursor=cursor)
        for secret in page.secrets:
            if secret.name == SECRET_NAME:
                return str(secret.secret_id)
        cursor = page.next_cursor
        if not cursor:
            return None


def _ensure_secret(client: Any, value: str) -> str:
    """Create or update the workspace secret holding the Delegator shared secret."""
    secret_id = _find_secret_id(client)
    if secret_id is not None:
        client.conversational_ai.secrets.update(secret_id, name=SECRET_NAME, value=value)
        return secret_id
    created = client.conversational_ai.secrets.create(name=SECRET_NAME, value=value)
    return str(created.secret_id)


def apply(settings: Settings) -> str:
    from elevenlabs.client import ElevenLabs
    from elevenlabs.types import AgentPlatformSettingsRequestModel, ConversationalConfig

    if not settings.elevenlabs_api_key:
        raise SystemExit("ELEVENLABS_API_KEY is not set")
    check_apply_settings(settings)
    client = ElevenLabs(api_key=settings.elevenlabs_api_key)
    secret_id = _ensure_secret(client, settings.delegator_shared_secret)
    payload = render(load_template(), placeholder_values(settings, secret_id))
    conversation_config = ConversationalConfig.model_validate(payload["conversation_config"])
    platform_settings = AgentPlatformSettingsRequestModel.model_validate(
        payload["platform_settings"]
    )
    agents = client.conversational_ai.agents
    if settings.elevenlabs_agent_id:
        agents.update(
            settings.elevenlabs_agent_id,
            conversation_config=conversation_config,
            platform_settings=platform_settings,
            name=payload["name"],
            tags=payload["tags"],
        )
        return settings.elevenlabs_agent_id
    created = agents.create(
        conversation_config=conversation_config,
        platform_settings=platform_settings,
        name=payload["name"],
        tags=payload["tags"],
    )
    return str(created.agent_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="print the payload (default)")
    mode.add_argument("--apply", action="store_true", help="create or update the agent")
    args = parser.parse_args(argv)
    settings = get_settings()
    if not args.apply:
        print(render_dry_run(settings))
        return 0
    agent_id = apply(settings)
    verb = "updated" if settings.elevenlabs_agent_id else "created"
    print(f"agent {verb}: {agent_id}")
    if verb == "created":
        print("set ELEVENLABS_AGENT_ID in .env to this id")
    return 0


if __name__ == "__main__":
    sys.exit(main())
