"""Runtime settings for every relay component.

This module is a contract: later waves (Delegator, executor, client) import these field
names. Values come from the environment and an optional `.env` file; see `.env.example`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy.engine import make_url

from relay.store.models import TASK_KINDS

Provider = Literal["ollama", "openai", "anthropic"]
PROVIDERS: tuple[Provider, ...] = ("ollama", "openai", "anthropic")


def parse_model_ref(ref: str) -> tuple[Provider, str]:
    """Split `<provider>:<model>` on the FIRST colon (model names may contain colons).

    A ref without a known provider prefix is a bare Ollama model name, which is
    what `ollama list` prints:

    >>> parse_model_ref("ollama:gemma4:e4b")
    ('ollama', 'gemma4:e4b')
    >>> parse_model_ref("qwen3.8:latest")
    ('ollama', 'qwen3.8:latest')
    """
    if not ref.strip():
        raise ValueError("model ref must not be empty")
    provider, sep, model = ref.partition(":")
    if provider not in PROVIDERS:
        return "ollama", ref
    if not sep or not model:
        raise ValueError(f"model ref {ref!r} must look like '<provider>:<model>'")
    return provider, model


def to_sync_url(url: str) -> str:
    """Return the psycopg (sync) form of a SQLAlchemy Postgres URL, for Alembic and DBOS."""
    return make_url(url).set(drivername="postgresql+psycopg").render_as_string(hide_password=False)


def to_libpq_url(url: str) -> str:
    """Return a driver-less `postgresql://` URL (for `psycopg.connect` and DBOS)."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def to_async_url(url: str) -> str:
    """Return the asyncpg form of a SQLAlchemy Postgres URL."""
    return make_url(url).set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)


_MODEL_FIELDS = (
    "delegator_model",
    "delegator_fallback_model",
    "assent_model",
    "ready_model",
    "summary_model",
    "research_model",
    "research_fallback_model",
    "router_model",
    "research_easy_model",
    "research_hard_model",
    "research_hard_fallback_model",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Store -----------------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://relay:relay@localhost:55432/relay"

    # --- LLM providers (local Ollama by default; frontier only by config) ------------------
    ollama_base_url: str = "http://localhost:11434/v1"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # Model refs: "<provider>:<model>", provider in {ollama, openai, anthropic}.
    delegator_model: str = "ollama:gemma4:e4b"
    delegator_fallback_model: str = "ollama:gemma4:e4b"
    assent_model: str = "ollama:gemma4:e4b"
    ready_model: str = "ollama:gemma4:e4b"
    summary_model: str = "ollama:gemma4:e4b"
    research_model: str = "ollama:qwen3.8:latest"
    research_fallback_model: str = "ollama:gemma4:e4b"

    # Reasoning effort sent to openai: delegator models (gpt-6-luna accepts none|low|medium|
    # high|xhigh). Keep "none" for voice: default reasoning measured 3.7 s TTFT vs 1.2 s, and
    # hidden reasoning tokens count against ElevenLabs' 300-token reply cap. "default" = omit.
    delegator_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "default"] = (
        "none"
    )

    # --- Task-difficulty routing (decision-5: gemma4 v2 router) ----------------------------
    # The router classifies each dispatched task easy/hard once; failure or timeout = hard.
    # Easy tasks run on research_easy_model; hard ones on research_hard_model, falling back
    # to research_hard_fallback_model. research_model/research_fallback_model remain the
    # pre-routing defaults.
    router_model: str = "ollama:gemma4:e4b"
    router_timeout_s: float = 5.0
    research_easy_model: str = "ollama:gemma4:e4b"
    research_hard_model: str = "openai:gpt-6-luna"
    research_hard_fallback_model: str = "ollama:qwen3.8:latest"

    # --- Delegator / voice -----------------------------------------------------------------
    delegator_shared_secret: str = "dev-secret-change-me"
    delegator_public_url: str = "http://localhost:8000"
    elevenlabs_api_key: str | None = None
    elevenlabs_agent_id: str | None = None

    # --- Executor --------------------------------------------------------------------------
    enabled_kinds: Annotated[list[str], NoDecode] = ["research"]
    artifacts_dir: str = "./artifacts"
    executor_url: str = "http://localhost:8001"

    # --- Client ----------------------------------------------------------------------------
    wake_model: str = "hey_jarvis"
    wake_threshold: float = 0.5
    silence_timeout_s: float = 60

    @field_validator("enabled_kinds", mode="before")
    @classmethod
    def _split_kinds(cls, value: object) -> object:
        if isinstance(value, str):
            return [k.strip() for k in value.split(",") if k.strip()]
        return value

    @field_validator("enabled_kinds")
    @classmethod
    def _check_kinds(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(TASK_KINDS))
        if unknown:
            raise ValueError(f"unknown task kinds {unknown}; expected a subset of {TASK_KINDS}")
        return value

    @field_validator(*_MODEL_FIELDS)
    @classmethod
    def _check_model_ref(cls, value: str) -> str:
        # Normalise bare Ollama names to `ollama:<name>` so readers see one form.
        provider, model = parse_model_ref(value)
        return f"{provider}:{model}"

    @property
    def sync_database_url(self) -> str:
        """psycopg URL (`postgresql+psycopg://...`) for Alembic and DBOS."""
        return to_sync_url(self.database_url)

    @property
    def libpq_database_url(self) -> str:
        """Plain `postgresql://` URL for DBOS / `psycopg.connect`."""
        return to_libpq_url(self.database_url)

    @property
    def async_database_url(self) -> str:
        """asyncpg URL for the application engine."""
        return to_async_url(self.database_url)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
