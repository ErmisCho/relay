"""The executor agent: one Pydantic AI ``Agent`` that researches and codes, made durable with DBOS.

Tools come from pydantic-ai-harness instead of being hand-rolled: ``Researcher`` (web search +
SSRF-safe web fetch) and the ``Coder`` capability set (file tools, shell) rooted
in the per-idea project folder the run receives as ``ExecutorDeps.workspace``.

Durability uses the ``DBOSDurability`` capability: every model request is a DBOS step when the
agent runs inside a DBOS workflow (``relay.research.run`` in ``runner.py``). DBOS only wraps
MCP and *dynamic* toolsets in steps, so all harness tools are contributed through ONE
``DynamicCapability`` (id ``executor``): its tool listing and every tool call run as DBOS steps,
and it is built per run, which is also what roots the file tools and the shell in that run's
own project folder.

Difficulty routing (TASK-46): the one agent carries a model per route, registered with
``DBOSDurability(models=...)`` under the keys ``easy`` and ``hard``, and each run picks one by
key (``agent.run_sync(..., model="hard")``). Step names do not depend on the model, so the
durable history of a run is the same whichever route it took. The agent's own default model
(``research_model`` -> ``research_fallback_model``) serves runs without a route.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx2
from pydantic import BaseModel, Field, HttpUrl
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    Capability,
    CombinedCapability,
    DynamicCapability,
    WebFetch,
    WebSearch,
)
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.compaction import ClearToolResults
from pydantic_ai_harness.researcher import DEFAULT_RESEARCHER_INSTRUCTIONS
from pydantic_ai_harness.shell import LLM_API_KEY_ENV_PATTERNS, Shell
from pydantic_ai_harness.tool_output_limits import Band, ToolOutputLimits, Truncate
from pydantic_settings import BaseSettings, SettingsConfigDict

from relay.config import Settings, get_settings, parse_model_ref
from relay.executor.agent.sandbox import sandboxed_shell
from relay.executor.routing import LABELS, Difficulty
from relay.executor.workspace import repo_root

log = logging.getLogger(__name__)

# Persisted in DBOS step names (`relay_research__model.request`): kept across the rename.
AGENT_NAME = "relay_research"
# Id of the per-run dynamic toolset; also persisted in DBOS step names.
TOOLSET_ID = "executor"
# A route is a router difficulty; it is also the model's key in the DBOSDurability registry.
Route = Difficulty
ROUTES: tuple[Route, ...] = LABELS
# Reasoning effort for `openai:` executor models. Research quality matters more than latency
# (a run takes minutes; a reasoning pass per request is cheap next to that), so this keeps
# OpenAI's own default for gpt-6-luna, "medium", but pins it so a provider default change is
# not silent. "none" is what the voice path uses; "default" omits the parameter.
DEFAULT_OPENAI_REASONING_EFFORT = "medium"
_PROVIDER_DEFAULT_EFFORTS = frozenset({"", "default"})
# Local generation is slow, so reads get 2 minutes; a dead endpoint fails fast on connect.
MODEL_TIMEOUT = httpx2.Timeout(120.0, connect=5.0)  # openai/anthropic SDKs are built on httpx2
# FallbackModel is the retry: an SDK retry loop would only delay switching to the fallback.
MODEL_MAX_RETRIES = 0

# The shell gets an explicit environment instead of inheriting the worker's, which carries
# DATABASE_URL, DELEGATOR_SHARED_SECRET, ELEVENLABS_* and provider keys. The sandbox then
# points HOME and TMPDIR into the workspace and prunes PATH (see sandbox.sandbox_env).
SHELL_ENV_KEYS: tuple[str, ...] = ("PATH", "HOME", "LANG", "TMPDIR")
# Stripped even from the explicit env above, in case SHELL_ENV_KEYS is ever widened.
SHELL_DENIED_ENV_PATTERNS: tuple[str, ...] = (
    *LLM_API_KEY_ENV_PATTERNS,
    "DATABASE_URL",
    "DBOS_*",
    "DELEGATOR_*",
    "ELEVENLABS_*",
    "*_API_KEY",
    "*_SECRET",
    "*_TOKEN",
)
# CLIs whose whole job is publishing (opening/merging PRs, posting issues) with stored tokens.
# Not the boundary, only an early, readable refusal. The boundary is the macOS sandbox every
# shell command runs in (sandbox.py): no network (so no `git push`, no curl to Postgres,
# Ollama or the Delegator), writes only inside the workspace, no reads of the home directory
# (credentials, keychains, git config) or the relay repository and its .env.
SHELL_DENIED_COMMANDS: tuple[str, ...] = ("gh", "glab")

# Context budget. Every earlier tool result is re-sent with each model request, so unbounded
# results (the harness defaults: 64k chars per result, 60k per read_file, clearing only at 70%
# of a 200k window) grew one request past 61k tokens: a 429 TPM from gpt-6-luna and a 120 s
# prefill timeout on local qwen (task 0952c583). Each result is cut to TOOL_OUTPUT_MAX_CHARS
# (~2.5k tokens) and, once the history passes CLEAR_TOOL_RESULTS_TOKENS (4 chars/token
# estimate), all but the last KEEP_TOOL_PAIRS results are replaced by a placeholder, so a
# request stays near 12k tokens however many pages the agent reads. Raise these only together
# with the local model's read timeout (MODEL_TIMEOUT).
TOOL_OUTPUT_MAX_CHARS = 10_000
CLEAR_TOOL_RESULTS_TOKENS = 8_000
KEEP_TOOL_PAIRS = 3

# pydantic-ai prints a multi-line "observability" banner on first run; keep worker logs clean.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")


@dataclass(frozen=True)
class ExecutorDeps:
    """Per-run dependencies: the idea's project folder, root of the file tools and shell."""

    workspace: Path


class ResearchBrief(BaseModel):
    """The agent's structured output for research tasks, rendered to Markdown by the runner."""

    title: str = Field(min_length=1, description="Short document title, no Markdown.")
    summary: str = Field(min_length=1, description="Two or three plain sentences.")
    body_markdown: str = Field(
        min_length=1, description="The document body in Markdown, without a Sources section."
    )
    sources: list[HttpUrl] = Field(
        default_factory=list,
        description=(
            "URLs actually fetched or read and relied on; empty for a report built only from "
            "commands run on this computer."
        ),
    )


INSTRUCTIONS = """\
You are relay's executor. You produce ONE result for the user to review later. You never send,
post, book, buy, publish, push or merge anything: no git push, no pull requests, no messages.

Method:
1. Decide where the answer lives. For facts on the web, use your web search tool to find
   relevant, reputable pages, then web_fetch to read the best. When the answer is on this
   computer (hardware, operating system, memory, disk space, installed software, settings),
   web search is not needed: inspect it with read-only commands in your shell (run_command),
   e.g. `sw_vers`, `uname -a`, `sysctl -n machdep.cpu.brand_string hw.memsize hw.ncpu`,
   `system_profiler SPHardwareDataType SPDisplaysDataType`, `df -h`. Only read; never change
   settings, install, delete or kill anything. The shell has no network.
2. Your working folder is this idea's project folder; keep any files you write inside it.
   For temporary files use "$TMPDIR" (e.g. `mktemp "$TMPDIR/x.XXXXXX"`): bare `mktemp` fails.
3. Write the result from what you actually read or what the commands printed. Do not invent
   facts, figures or URLs. Leave out serial numbers and hardware UUIDs.
4. Stay strictly inside the goal. The "Out of scope" text is binding: do not research,
   discuss, recommend or even mention anything it excludes.
5. List in `sources` only URLs you fetched or that search results showed and you relied on.
   A report built only from this computer's commands has no URLs: leave `sources` empty and
   name the commands you ran in the body. Do not put a Sources section inside body_markdown;
   it is added automatically.
"""


# Persisted in DBOS step names of code runs (`relay_code__model.request`).
CODE_AGENT_NAME = "relay_code"


class CodeChangeSummary(BaseModel):
    """The agent's structured output for code tasks; becomes the draft PR description."""

    title: str = Field(min_length=1, description="Pull request title, imperative, no Markdown.")
    summary: str = Field(min_length=1, description="Two or three plain sentences: what changed.")
    changes: list[str] = Field(description="One short line per change made (file: what).")
    how_to_run: str = Field(
        min_length=1, description="How to run or try the result (commands, in Markdown)."
    )


CODE_INSTRUCTIONS = """\
You are relay's code executor. You make ONE change in this idea's project folder for the user to
review later. The folder is a git repository already on a fresh branch made for this task: relay
commits whatever you leave in the working tree, runs the tests, and leaves it as a draft pull
request. You never push, merge, publish or open pull requests yourself, and you do not commit,
switch branches or rewrite history (no git commit/checkout/switch/merge/rebase/reset/push).

Method:
1. Read the relevant files first (list_files, read_file, grep), then edit with the file tools.
2. Stay strictly inside the goal. The "Out of scope" text is binding: do not touch it.
3. The shell has no network: package installs and downloads fail, so do not try them. Use only
   what is already installed. For temporary files use "$TMPDIR" (bare `mktemp` fails).
4. Add or update tests for what you change when the project has tests. You may run them; relay
   runs them again afterwards and reports the result, pass or fail.
5. In `changes` list what you actually changed; in `how_to_run` say how to run or try it.
"""


def build_prompt(goal: str, scope_excludes: str) -> str:
    """User prompt carrying the commitment's goal and exclusions verbatim."""
    excludes = scope_excludes if scope_excludes.strip() else "(nothing explicitly excluded)"
    return f"Goal:\n{goal}\n\nOut of scope (must be respected):\n{excludes}\n"


def shell_env() -> dict[str, str]:
    """The shell's base environment: ``SHELL_ENV_KEYS`` copied from the worker, nothing else."""
    return {k: os.environ[k] for k in SHELL_ENV_KEYS if k in os.environ}


def shell_denied_read_roots() -> list[Path]:
    """Trees sandboxed commands may not read: the owner's home and the relay repository."""
    return [Path.home(), repo_root()]


def workspace_capabilities(workspace: Path) -> list[AbstractCapability[ExecutorDeps]]:
    """Researcher + the Coder set, rooted in ``workspace``, with a sandboxed shell.

    ``Coder``'s persistent ``Shell`` is swapped for run-scoped tools (``run_command``,
    ``start_command``, ...; killed when the run ends) that run every command under macOS
    ``sandbox-exec`` with an explicit env. Without the sandbox there is no shell at all (fail
    closed). The rest of the set (workspace-scoped file tools, context management) is kept.
    """
    root = workspace.resolve()
    shell = sandboxed_shell(
        root,
        env=shell_env(),
        denied_read_roots=shell_denied_read_roots(),
        denied_env_patterns=SHELL_DENIED_ENV_PATTERNS,
        denied_commands=SHELL_DENIED_COMMANDS,
    )
    # No RepoContext: it turns every model request into a streamed one (measured with harness
    # 0.36), which changes the durable step names and the model contract. A fresh project
    # folder has no CLAUDE.md/AGENTS.md to load anyway; revisit when the code runner (TASK-33)
    # works inside existing repositories.
    coder = Coder[ExecutorDeps](root, repo_context=False, sub_agents=False)
    coding: list[AbstractCapability[ExecutorDeps]] = []
    for c in coder.capabilities:
        if isinstance(c, Shell):
            if shell is not None:
                coding.append(shell)
        elif not isinstance(c, ClearToolResults | ToolOutputLimits):
            coding.append(c)  # Coder's own, looser context limits are replaced below
    # The Researcher capability minus its sub-agents and its output limits: those spill a long
    # result and add `read_tool_result`, whose returns are exempt from every limit, so a model
    # reading a spilled page back got it whole, and it was never cleared. Same instructions.
    # Sub-agents off: local models are slow and every delegation multiplies model calls, so
    # one agent does all the reading. Re-enable when a frontier model is the default executor
    # model.
    research: list[AbstractCapability[ExecutorDeps]] = [
        Capability[ExecutorDeps](instructions=DEFAULT_RESEARCHER_INSTRUCTIONS),
        WebSearch[ExecutorDeps](local=True),
        # Local fetch only: a provider-native fetch (OpenAI Responses) feeds whole pages to
        # the model server-side, where no output limit can reach them.
        WebFetch[ExecutorDeps](native=False, local=True),
    ]
    return [*research, *coding, *context_limits()]


class _TruncateToolOutputs(ToolOutputLimits[ExecutorDeps]):
    """Truncation-only output limits: nothing is spilled, so no retrieval tool is added."""

    def get_toolset(self) -> None:
        return None


def context_limits() -> list[AbstractCapability[ExecutorDeps]]:
    """Per-result truncation plus clearing of old results (see ``TOOL_OUTPUT_MAX_CHARS``)."""
    return [
        ClearToolResults[ExecutorDeps](
            max_tokens=CLEAR_TOOL_RESULTS_TOKENS, keep_pairs=KEEP_TOOL_PAIRS
        ),
        _TruncateToolOutputs(
            id="executor_tool_output_limits",
            bands=[
                Band(over=TOOL_OUTPUT_MAX_CHARS, action=Truncate(max_chars=TOOL_OUTPUT_MAX_CHARS))
            ],
        ),
    ]


def _run_capabilities(ctx: RunContext[ExecutorDeps]) -> AbstractCapability[ExecutorDeps]:
    return CombinedCapability(workspace_capabilities(ctx.deps.workspace))


def executor_capability() -> DynamicCapability[ExecutorDeps]:
    """All harness tools as one per-run dynamic capability (a DBOS-wrapped toolset)."""
    return DynamicCapability(_run_capabilities, id=TOOLSET_ID)


class _ExecutorLLMEnv(BaseSettings):
    """Executor knobs not in ``relay.config.Settings``; read from the env and ``.env``.

    ``RESEARCH_REASONING_EFFORT``: reasoning effort sent to ``openai:`` executor models
    (gpt-6-luna accepts none|low|medium|high|xhigh; default ``medium``; ``default`` omits it).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    research_reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT


def reasoning_effort() -> str | None:
    """The reasoning effort for ``openai:`` executor models; None = provider default."""
    value = _ExecutorLLMEnv().research_reasoning_effort.strip().lower()
    return None if value in _PROVIDER_DEFAULT_EFFORTS else value


def build_model_from_ref(ref: str, settings: Settings) -> Model:
    """One concrete model for ``<provider>:<model>``; Ollama via its OpenAI-compatible API."""
    provider, name = parse_model_ref(ref)
    if provider in ("ollama", "openai"):
        from openai import AsyncOpenAI
        from pydantic_ai.models.openai import OpenAIChatModel

        if provider == "ollama":
            from pydantic_ai.providers.ollama import OllamaProvider

            # OllamaProvider's profile maps Ollama's `reasoning` field (qwen3/gemma thinking)
            # to ThinkingParts, so thinking never leaks into the structured output.
            ollama_client = AsyncOpenAI(
                base_url=settings.ollama_base_url,
                api_key=os.environ.get("OLLAMA_API_KEY") or "ollama",
                timeout=MODEL_TIMEOUT,
                max_retries=MODEL_MAX_RETRIES,
            )
            return OpenAIChatModel(name, provider=OllamaProvider(openai_client=ollama_client))
        from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
        from pydantic_ai.providers.openai import OpenAIProvider

        if not settings.openai_api_key:
            raise ValueError(f"{ref!r} needs OPENAI_API_KEY")
        openai_client = AsyncOpenAI(
            api_key=settings.openai_api_key, timeout=MODEL_TIMEOUT, max_retries=MODEL_MAX_RETRIES
        )
        # The Responses API, not Chat Completions: gpt-6-luna rejects function tools together
        # with a reasoning effort on /v1/chat/completions (400, verified live 2026-09-26), and
        # the agent needs both. Responses also carries reasoning across tool calls.
        # pydantic-ai's OpenAI profile knows gpt-6-luna as a reasoning model (drops sampling
        # params while reasoning is on).
        model_settings = OpenAIResponsesModelSettings()
        if (effort := reasoning_effort()) is not None:
            model_settings["openai_reasoning_effort"] = effort  # type: ignore[typeddict-item]
        return OpenAIResponsesModel(
            name, provider=OpenAIProvider(openai_client=openai_client), settings=model_settings
        )
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    anthropic_client = AsyncAnthropic(
        api_key=settings.anthropic_api_key, timeout=MODEL_TIMEOUT, max_retries=MODEL_MAX_RETRIES
    )
    return AnthropicModel(name, provider=AnthropicProvider(anthropic_client=anthropic_client))


def build_executor_model(settings: Settings | None = None) -> FallbackModel:
    """``research_model`` with ``research_fallback_model`` behind it (on any ModelAPIError)."""
    s = settings or get_settings()
    return FallbackModel(
        build_model_from_ref(s.research_model, s),
        build_model_from_ref(s.research_fallback_model, s),
    )


def _chain(primary: str, fallback: str | None, s: Settings) -> Model:
    """``primary`` alone, or wrapped with ``fallback`` when that is a different model."""
    first = build_model_from_ref(primary, s)
    if fallback is None or fallback == primary:
        return first
    return FallbackModel(first, build_model_from_ref(fallback, s))


def route_model_refs(route: Route, settings: Settings | None = None) -> tuple[str, str | None]:
    """(primary, fallback) model refs for a route.

    easy: ``research_easy_model``; ``research_fallback_model`` backs it only when it differs
    (both default to local gemma4, and a second attempt on the same dead Ollama would only
    double the wait). hard: ``research_hard_model`` -> ``research_hard_fallback_model``.
    """
    s = settings or get_settings()
    if route == "easy":
        fallback = s.research_fallback_model
        return s.research_easy_model, None if fallback == s.research_easy_model else fallback
    return s.research_hard_model, s.research_hard_fallback_model


def build_route_models(settings: Settings | None = None) -> dict[Route, Model]:
    """The model (chain) per route, from settings.

    If the hard primary cannot be built (e.g. no OPENAI_API_KEY) hard tasks run on the hard
    fallback alone, with a warning: the served model is recorded per task, so it is visible.
    """
    s = settings or get_settings()
    models: dict[Route, Model] = {}
    for route in ROUTES:
        primary, fallback = route_model_refs(route, s)
        try:
            models[route] = _chain(primary, fallback, s)
        except ValueError as exc:
            if fallback is None:
                raise
            log.warning(
                "executor %s model %s unavailable (%s); using %s", route, primary, exc, fallback
            )
            models[route] = build_model_from_ref(fallback, s)
    return models


def build_executor_agent(
    model: Model, routes: Mapping[Route, Model] | None = None
) -> Agent[ExecutorDeps, ResearchBrief]:
    return _build_agent(AGENT_NAME, ResearchBrief, INSTRUCTIONS, model, routes)


def build_code_agent(
    model: Model, routes: Mapping[Route, Model] | None = None
) -> Agent[ExecutorDeps, CodeChangeSummary]:
    """Same tools, routing and durability as the research agent; code output and instructions."""
    return _build_agent(CODE_AGENT_NAME, CodeChangeSummary, CODE_INSTRUCTIONS, model, routes)


def _build_agent[T: BaseModel](
    name: str,
    output_type: type[T],
    instructions: str,
    model: Model,
    routes: Mapping[Route, Model] | None,
) -> Agent[ExecutorDeps, T]:
    return Agent(
        model,
        name=name,
        deps_type=ExecutorDeps,
        output_type=output_type,
        instructions=instructions,
        capabilities=[
            executor_capability(),
            DBOSDurability(models={str(k): m for k, m in (routes or {}).items()}),
        ],
        retries=2,
    )


_agent: Agent[ExecutorDeps, ResearchBrief] | None = None
_lock = threading.Lock()


def configure_executor_agent(
    model: Model | None = None, routes: Mapping[Route, Model] | None = None
) -> Agent[ExecutorDeps, ResearchBrief]:
    """(Re)build the process-wide agent, e.g. with test models before DBOS launches.

    ``model`` serves runs without a route; ``routes`` maps ``easy``/``hard`` to their models.
    Each defaults to the models built from settings.
    """
    global _agent
    with _lock:
        _agent = build_executor_agent(
            model if model is not None else build_executor_model(),
            routes if routes is not None else build_route_models(),
        )
        return _agent


def get_executor_agent() -> Agent[ExecutorDeps, ResearchBrief]:
    """The configured agent, built lazily from settings on first use."""
    with _lock:
        if _agent is not None:
            return _agent
    return configure_executor_agent()


_code_agent: Agent[ExecutorDeps, CodeChangeSummary] | None = None


def configure_code_agent(
    model: Model | None = None, routes: Mapping[Route, Model] | None = None
) -> Agent[ExecutorDeps, CodeChangeSummary]:
    """(Re)build the process-wide code agent; same defaults as ``configure_executor_agent``."""
    global _code_agent
    with _lock:
        _code_agent = build_code_agent(
            model if model is not None else build_executor_model(),
            routes if routes is not None else build_route_models(),
        )
        return _code_agent


def get_code_agent() -> Agent[ExecutorDeps, CodeChangeSummary]:
    """The configured code agent, built lazily from settings on first use."""
    with _lock:
        if _code_agent is not None:
            return _code_agent
    return configure_code_agent()
