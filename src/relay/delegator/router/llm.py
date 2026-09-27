"""Zero-shot LLM router backend (TASK-41).

One call per turn asks the shared ``questions.py`` questions and returns structured JSON
``{"difficulty", "ready", "intent"}``. ``ROUTER_LLM_MODEL`` picks the transport:

* ``ollama:<model>``: Ollama's native ``/api/chat`` with the answer JSON schema as
  ``format`` (constrained decoding), ``think=false`` and ``temperature=0``;
* ``openai:<model>`` / ``anthropic:<model>``: a pydantic-ai ``Agent`` with a structured
  output type built from the same schema.

It must be a small model, never the frontier model (that would defeat the purpose). The
whole call runs under a hard ``ROUTER_LLM_TIMEOUT_MS`` budget; timeout, transport error or
invalid output yields ``None`` (callers treat that as ``frontier``). ``confidence`` is
always ``None``: no logprobs, no thresholds. Latency and token usage (plus the USD cost
when pydantic-ai can price the model; local Ollama costs nothing) are logged per call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import replace
from typing import Any

import httpx

from relay.config import Settings, parse_model_ref
from relay.delegator.router.base import RouterDecision, RouterStatus, Turn
from relay.delegator.router.questions import (
    answer_schema,
    decision_from_answers,
    render_context,
    system_prompt,
    user_prompt,
)

log = logging.getLogger(__name__)

BACKEND = "llm"
# Three short labels; generous enough for any tokenizer, small enough to cap runaway output.
MAX_OUTPUT_TOKENS = 64


def ollama_native_url(base_url: str) -> str:
    """Ollama's native API root from the OpenAI-compatible ``OLLAMA_BASE_URL`` (``.../v1``)."""
    root = base_url.rstrip("/")
    return root[: -len("/v1")] if root.endswith("/v1") else root


class LLMRouter:
    """``Router`` backed by a small local (Ollama) or cloud (pydantic-ai) model."""

    name = BACKEND

    def __init__(self, settings: Settings, *, http_client: httpx.AsyncClient | None = None) -> None:
        self.model_ref = settings.router_llm_model
        self.provider, self.model = parse_model_ref(self.model_ref)
        self.timeout_s = settings.router_llm_timeout_ms / 1000
        self.context_turns = settings.router_llm_context_turns
        self.context_chars = settings.router_llm_context_chars
        self._settings = settings
        self._http = http_client
        self._agent: Any = None

    async def decide(self, utterance: str, context: list[Turn]) -> RouterDecision | None:
        decision, _ = await self.decide_with_status(utterance, context)
        return decision

    async def decide_with_status(
        self, utterance: str, context: list[Turn]
    ) -> tuple[RouterDecision | None, RouterStatus]:
        """Never raises: ``(None, "timeout" | "error" | "invalid")`` on failure."""
        prompt = user_prompt(
            utterance, render_context(context, self.context_turns, self.context_chars)
        )
        start = time.perf_counter()
        answers: object = None
        usage: dict[str, Any] = {}
        status: RouterStatus
        try:
            async with asyncio.timeout(self.timeout_s):
                if self.provider == "ollama":
                    answers, usage = await self._ollama(prompt)
                else:
                    answers, usage = await self._pydantic_ai(prompt)
            status = "ok"
        except TimeoutError:
            status = "timeout"
        except Exception:  # noqa: BLE001 - a router failure must never reach the turn
            log.warning("router llm (%s) call failed", self.model_ref, exc_info=True)
            status = "error"
        latency_ms = round((time.perf_counter() - start) * 1000)
        decision = None
        if status == "ok":
            decision = decision_from_answers(answers, backend=BACKEND, latency_ms=latency_ms)
            status = "ok" if decision is not None else "invalid"
            if decision is not None:
                decision = replace(
                    decision,
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    cost_usd=usage.get("cost_usd"),
                )
        log.info(
            "router llm model=%s status=%s latency_ms=%d input_tokens=%s output_tokens=%s "
            "cost_usd=%s",
            self.model_ref,
            status,
            latency_ms,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            usage.get("cost_usd"),
        )
        return decision, status

    async def _ollama(self, prompt: str) -> tuple[object, dict[str, Any]]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt()},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "format": answer_schema(),
            # Without think=false gemma4 spends its output budget on hidden reasoning.
            "think": False,
            "options": {"temperature": 0, "seed": 0, "num_predict": MAX_OUTPUT_TOKENS},
        }
        url = ollama_native_url(self._settings.ollama_base_url) + "/api/chat"
        if self._http is not None:
            resp = await self._http.post(url, json=payload)
        else:
            async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                resp = await client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        usage = {
            "input_tokens": body.get("prompt_eval_count"),
            "output_tokens": body.get("eval_count"),
            "cost_usd": 0.0,
        }
        content = (body.get("message") or {}).get("content") or ""
        try:
            return json.loads(content), usage
        except json.JSONDecodeError:
            return None, usage

    async def _pydantic_ai(self, prompt: str) -> tuple[object, dict[str, Any]]:
        agent = self._agent or self._build_agent()
        self._agent = agent
        result = await agent.run(prompt)
        run_usage = result.usage  # a property in pydantic-ai 1.5x (was a method)
        run_usage = run_usage() if callable(run_usage) else run_usage
        usage: dict[str, Any] = {
            "input_tokens": run_usage.input_tokens,
            "output_tokens": run_usage.output_tokens,
        }
        try:
            usage["cost_usd"] = float(result.response.cost().total_price)
        except Exception:  # noqa: BLE001 - pricing is best effort (unknown models)
            usage["cost_usd"] = None
        return result.output, usage

    def _build_agent(self) -> Any:
        from pydantic_ai import Agent, StructuredDict
        from pydantic_ai.settings import ModelSettings

        model: Any
        if self.provider == "openai":
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            if not self._settings.openai_api_key:
                raise ValueError(f"router model {self.model_ref!r} needs OPENAI_API_KEY")
            model = OpenAIChatModel(
                self.model, provider=OpenAIProvider(api_key=self._settings.openai_api_key)
            )
        elif self.provider == "anthropic":
            from pydantic_ai.models.anthropic import AnthropicModel
            from pydantic_ai.providers.anthropic import AnthropicProvider

            if not self._settings.anthropic_api_key:
                raise ValueError(f"router model {self.model_ref!r} needs ANTHROPIC_API_KEY")
            model = AnthropicModel(
                self.model, provider=AnthropicProvider(api_key=self._settings.anthropic_api_key)
            )
        else:
            raise ValueError(f"router provider {self.provider!r} is not supported")
        output = StructuredDict(answer_schema(), name="routing_answers")
        return Agent(
            model,
            output_type=output,
            instructions=system_prompt(),
            model_settings=ModelSettings(temperature=0, max_tokens=MAX_OUTPUT_TOKENS * 4),
            retries=0,
        )
