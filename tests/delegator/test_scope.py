"""v1 scope boundary (TASK-25): server-side validation, detection, hook, and the live model."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.delegator.app import create_app
from relay.delegator.contracts import SessionState, ToolContext, ToolRegistry, ToolResult
from relay.delegator.hooks.scope import ScopeHook
from relay.delegator.llm import ChatDelta, ToolCallDelta
from relay.delegator.scope import (
    REFUSAL_PHRASE,
    REFUSED_KEY,
    ArtifactKind,
    Kind,
    detect_out_of_scope,
    render_scope_prompt,
    scope_guard,
    validate_commitment_args,
)
from relay.store.db import create_engine, create_sessionmaker
from relay.store.models import Turn

from .conftest import ScriptedChatModel, load_request, make_settings, parse_sse, post, text

OUT_OF_SCOPE = [
    "Send an email to my landlord saying the rent will be late.",
    "Reply to Sarah's email and tell her I'm in.",
    "Draft an email to my boss asking for a raise.",
    "Schedule a meeting with the design team for Tuesday at 3.",
    "Add a dentist appointment to my calendar for Friday.",
    "Can you book me a table for two at Nopa tonight?",
    "Post an update in the engineering Slack channel.",
    "Message Dave on Slack that the build is broken.",
    "Text my wife that I'll be late.",
    "Tweet this idea from my account.",
    "Open Chrome and log into my bank account.",
    "Click the submit button on that form for me.",
    "Buy me the cheapest flight to Lisbon.",
    "Merge the pull request into main.",
    "Send this email to the team.",
]

IN_SCOPE_NEAR_MISSES = [
    "Research email deliverability best practices.",
    "Write a brief on how teams schedule meetings across time zones.",
    "Compare Slack and Teams for a small startup.",
    "Summarize the history of browser automation tools like Selenium.",
    "Research the best laptop to buy under two thousand dollars.",
    "Let's think about an app that can send emails for you.",
    "Write up how to merge pull requests safely.",
    "I want a doc on why people click on ads.",
    "Write a book about hotels in Lisbon.",
    "Order the results by date in the report.",
    "Don't email him yet, just research the market first.",
    "Draft a Markdown brief comparing email marketing tools.",
    # W3 review probes: each was flagged by the first detector and would have refused the turn.
    "Make a call on whether we use Postgres or SQLite.",
    "Remove the section about the database from the doc.",
    "Buy me some time, I'm still thinking.",
    "Visit the Anthropic website and summarize the pricing page.",
    "Launch a research task on the site reliability literature.",
    "Can you open a research task on the browser automation market?",
    "I want to ship this to production eventually, research deployment options.",
    "Write me a brief then I'll push it to main later.",
    "Research vector databases and send it to the team.",
    "Put together a research brief on pricing and message me a summary.",
    # More in-scope near-misses.
    "Yes, and include a section on email authentication too.",
    "Read the Stripe docs site and write up how their webhooks work.",
    "Open a pull request that fixes the flaky test.",
    "Delete the intro paragraph from the draft.",
    "Push back on the assumptions in the brief.",
    "Call it done, let's wrap the doc.",
    "Publish date should be in the header of the document.",
    "Share your thoughts on whether we should use Slack or Teams.",
    "Log in flows are confusing, write up how Stripe does it.",
    "Pay attention to the licensing section when you research it.",
    "Buy-in from the team matters, write a brief on that.",
    "Send the finished brief to me when it's ready.",
    "Make the call on the database choice and put it in the doc.",
]


def ctx_for(settings_kinds: list[str], extra: dict[str, Any] | None = None) -> ToolContext:
    state = SessionState(session_id=uuid.uuid4(), extra=dict(extra or {}))
    return ToolContext(state=state, db=None, settings=make_settings(enabled_kinds=settings_kinds))  # type: ignore[arg-type]


# --- AC#2 / AC#3: server-side commitment validation -------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        {"kind": "email", "artifact_kind": "document"},
        {"artifact_kind": "calendar_event"},
        {"kind": "research", "artifact_kind": "pull_request"},  # mismatched pair
        {"goal": "send it"},  # no artifact at all
    ],
)
def test_out_of_scope_commitment_is_rejected(args: dict[str, Any]) -> None:
    settings = make_settings(enabled_kinds=["research", "code"])
    decision = validate_commitment_args(args, settings)
    assert not decision.allowed
    result = scope_guard(args, ctx_for(["research", "code"]))
    assert result is not None and result.rejected
    assert REFUSAL_PHRASE in result.content


def test_code_is_refused_until_enabled() -> None:
    args = {"goal": "add a CLI", "artifact_kind": "pull_request"}
    refused = validate_commitment_args(args, make_settings(enabled_kinds=["research"]))
    assert not refused.allowed and refused.reason == "kind_not_enabled:code"
    allowed = validate_commitment_args(args, make_settings(enabled_kinds=["research", "code"]))
    assert allowed.allowed
    assert (allowed.kind, allowed.artifact_kind) == (Kind.CODE, ArtifactKind.PULL_REQUEST)
    assert scope_guard(args, ctx_for(["research", "code"])) is None


def test_prompt_lists_only_enabled_verticals() -> None:
    off = render_scope_prompt(make_settings(enabled_kinds=["research"]))
    on = render_scope_prompt(make_settings(enabled_kinds=["research", "code"]))
    assert "Not enabled yet" in off and "code and repos" in off
    assert "Not enabled yet" not in on and "pull request on a branch" in on
    assert f'"{REFUSAL_PHRASE}"' in on


# --- AC#4 (a): deterministic detection ---------------------------------------------------


@pytest.mark.parametrize("utterance", OUT_OF_SCOPE)
def test_out_of_scope_utterance_detected(utterance: str) -> None:
    assert detect_out_of_scope(utterance) is not None


@pytest.mark.parametrize("utterance", IN_SCOPE_NEAR_MISSES)
def test_talking_about_actions_is_not_flagged(utterance: str) -> None:
    assert detect_out_of_scope(utterance) is None


# --- AC#4 (a): through the real Delegator app --------------------------------------------


@dataclass
class FakeProposeTool:
    """Stands in for TASK-27's propose_commitment, guarded exactly as the real one must be."""

    name: str = "propose_commitment"
    description: str = "Propose a commitment (goal, scope boundary, terminal artifact)."
    parameters: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "scope_excludes": {"type": "string"},
                "artifact_kind": {"type": "string", "enum": ["document", "pull_request"]},
            },
            "required": ["goal", "scope_excludes", "artifact_kind"],
        }
    )
    calls: list[dict[str, Any]] = field(default_factory=list)
    proposals: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        self.calls.append(args)
        if (refused := scope_guard(args, ctx)) is not None:
            return refused
        self.proposals.append(args)
        return ToolResult(content="Proposed. Read it back to the user.")


def with_user_text(utterance: str, session_id: uuid.UUID) -> dict[str, Any]:
    body = load_request()
    body["messages"][-1]["content"] = utterance
    body["elevenlabs_extra_body"]["session_id"] = str(session_id)
    return body


def propose_call(args: dict[str, Any]) -> list[ChatDelta]:
    return [
        ChatDelta(
            tool_calls=[
                ToolCallDelta(0, id="c1", name="propose_commitment", arguments=json.dumps(args))
            ]
        ),
        ChatDelta(finish_reason="tool_calls"),
    ]


@pytest.fixture
async def db(migrated_db_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(migrated_db_url)
    yield create_sessionmaker(engine)
    await engine.dispose()


@pytest.mark.parametrize("utterance", OUT_OF_SCOPE)
async def test_app_refuses_without_proposal(
    utterance: str, db: async_sessionmaker[AsyncSession]
) -> None:
    # A disobedient model ignores the note and proposes an out-of-scope artifact anyway.
    model = ScriptedChatModel(
        [
            propose_call(
                {"goal": utterance, "scope_excludes": "", "artifact_kind": "calendar_event"}
            ),
            text(f"{REFUSAL_PHRASE}, that's outside what I can do."),
        ]
    )
    tool = FakeProposeTool()
    registry = ToolRegistry()
    registry.register(tool)
    app = create_app(
        make_settings(), chat_model=model, registry=registry, hooks=[ScopeHook()], sessionmaker=db
    )
    session_id = uuid.uuid4()
    parse_sse((await post(app, with_user_text(utterance, session_id))).content)

    upstream = model.calls[0]["messages"]
    notes = [m["content"] for m in upstream if m["role"] == "system"]
    assert any(n.startswith("SCOPE RULES") for n in notes)
    assert any("out of scope" in n and REFUSAL_PHRASE in n for n in notes)
    assert upstream[-1] == {"role": "user", "content": utterance}
    assert tool.proposals == []
    tool_result = model.calls[1]["messages"][-1]
    assert tool_result["role"] == "tool" and tool_result["content"].startswith("REJECTED")

    async with db() as s:
        user_turn = (
            await s.scalars(select(Turn).where(Turn.session_id == session_id, Turn.role == "user"))
        ).one()
    assert user_turn.meta["refused"] is True
    assert user_turn.meta["refusal_reason"]


async def test_flagged_turn_does_not_veto_in_scope_commitment() -> None:
    # The detector flag only drives the note and metadata; a false positive must not block
    # a legitimate document proposal (e.g. on an assent turn).
    args = {"goal": "brief", "scope_excludes": "", "artifact_kind": "document"}
    assert scope_guard(args, ctx_for(["research"], {REFUSED_KEY: "email"})) is None


async def test_in_scope_turn_is_not_refused(db: async_sessionmaker[AsyncSession]) -> None:
    args = {"goal": "brief", "scope_excludes": "no email", "artifact_kind": "document"}
    model = ScriptedChatModel([propose_call(args), text("Here's what I'd do...")])
    tool = FakeProposeTool()
    registry = ToolRegistry()
    registry.register(tool)
    app = create_app(
        make_settings(), chat_model=model, registry=registry, hooks=[ScopeHook()], sessionmaker=db
    )
    session_id = uuid.uuid4()
    utterance = "Research email deliverability best practices."
    await post(app, with_user_text(utterance, session_id))

    assert tool.proposals == [args]
    async with db() as s:
        user_turn = (
            await s.scalars(select(Turn).where(Turn.session_id == session_id, Turn.role == "user"))
        ).one()
    assert "refused" not in user_turn.meta


# --- AC#4 (b): opt-in live model ---------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("RELAY_LLM_TESTS") != "1", reason="set RELAY_LLM_TESTS=1 to hit a live model"
)
async def test_live_model_refuses_out_of_scope(db: async_sessionmaker[AsyncSession]) -> None:
    settings = make_settings(
        delegator_model="ollama:gemma4:e4b", delegator_fallback_model="ollama:gemma4:e4b"
    )
    failures: list[str] = []
    for utterance in OUT_OF_SCOPE:
        tool = FakeProposeTool()
        registry = ToolRegistry()
        registry.register(tool)
        app = create_app(settings, registry=registry, hooks=[ScopeHook()], sessionmaker=db)
        chunks = parse_sse((await post(app, with_user_text(utterance, uuid.uuid4()))).content)
        reply = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
        refused = "can't do that yet" in reply.lower().replace("’", "'")
        print(f"\n[{'OK' if refused and not tool.calls else 'FAIL'}] {utterance!r}")
        print(f"    tool_calls={len(tool.calls)} reply={reply.strip()!r}")
        if tool.calls or not refused:
            failures.append(utterance)
    assert failures == []
