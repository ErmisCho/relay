"""The two model calls of the commitment protocol: assent (critical path) and ready (audit).

Both ask for a single JSON object ``{"label": ...}`` and parse it strictly (a tool call carrying
the same object is accepted too). Plain JSON output measured ~0.2 s warm on gemma4:e4b vs ~0.37 s
for a forced tool call, and assent sits on the time-to-first-token path.
Anything that is not exactly one of the allowed labels is treated as "no answer", which for
assent means NOT affirmative.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from relay.delegator.llm.base import ChatModel

log = logging.getLogger(__name__)

#: Assent is on the time-to-first-token path: bound it tightly and fail safe.
ASSENT_TIMEOUT_S = 4.0
READY_TIMEOUT_S = 20.0


class AssentLabel(StrEnum):
    AFFIRMATIVE = "affirmative"
    HEDGE = "hedge"
    NEGATIVE = "negative"
    NEW_INFORMATION = "new_information"


ASSENT_SYSTEM_PROMPT = """\
You judge whether a user gave explicit, unconditional agreement to a plan that was just read \
back to them. A background job starts ONLY on "affirmative", and starting it by mistake is far \
worse than asking again, so when in any doubt do not answer "affirmative".

Labels:
- affirmative: a clear, unqualified yes to the plan exactly as read back ("yes", "yes please", \
"go ahead", "yep, do it", "sounds good, go for it", "that's right, start it").
- hedge: anything uncertain, lukewarm, conditional or questioning ("sure, I guess", "maybe", \
"I think so?", "probably", "hmm ok", "yeah but what about...", "if you want", "I suppose", \
"kind of", "let me think").
- negative: a no, a stop or a postponement ("no", "not yet", "wait", "hold on", "don't", \
"cancel that", "let's not").
- new_information: the user changes, adds to or corrects the plan, or talks about something \
else ("also include prices", "actually make it about Europe", "what's the weather").

A yes combined with any condition, change, question or doubt is NOT affirmative.
Reply with only a JSON object and nothing else: {"label": "<one of the four labels>"}"""

ASSENT_TOOL = "classify_assent"


def assent_user_prompt(readback_text: str, utterance: str) -> str:
    return (
        f'Plan read back to the user: "{readback_text}"\n'
        f'User\'s reply: "{" ".join(utterance.split())}"\n'
        "Which label fits the reply?"
    )


# --- deterministic pre-check -------------------------------------------------------------
# May only ever short-circuit AWAY from affirmative; never to it.

_NEGATIVE = re.compile(
    r"\b(?:no|nope|nah|don'?t|do not|not yet|not now|stop|cancel|hold on|hold off|wait"
    r"|never ?mind|scratch that|let'?s not|forget it)\b"
)
_HEDGE = re.compile(
    r"\b(?:maybe|perhaps|probably|possibly|i guess|i suppose|i think|guess so|not sure|unsure"
    r"|kind of|sort of|kinda|sorta|hm+|um+|uh+|er+m?|might|could|would|what about|how about"
    r"|but|though|although|unless|if|depends|let me think|later|eventually|whatever"
    r"|i don'?t know|dunno|fine i guess|shall|should)\b"
)


def _words(text: str) -> list[str]:
    return re.sub(r"[^0-9a-z ]+", " ", text.lower().replace("’", "'").replace("'", "")).split()


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    it = iter(haystack)
    return all(word in it for word in needle)


def is_echo(utterance: str, readback_text: str) -> bool:
    """True when every word of the utterance appears, in order, in the read-back.

    Headset echo of the agent's own read-back ("should I start on it") gets transcribed as a
    user turn; it must never count as assent.
    """
    words = _words(utterance)
    return bool(words) and _is_subsequence(words, _words(readback_text))


def precheck(utterance: str, readback_text: str = "") -> AssentLabel | None:
    """Obvious negatives / hedges / questions / echoes without a model call.

    ``None`` means "ask the model". Never returns ``affirmative``.
    """
    text = " ".join(utterance.lower().replace("’", "'").split())
    if not text.strip(" .,!?…-"):
        return AssentLabel.HEDGE
    if _NEGATIVE.search(text):
        return AssentLabel.NEGATIVE
    if "?" in text or "…" in text or "..." in text or _HEDGE.search(text):
        return AssentLabel.HEDGE
    if readback_text and is_echo(utterance, readback_text):
        return AssentLabel.HEDGE
    return None


# --- structured single-label call --------------------------------------------------------

_JSON_OBJECT = re.compile(r"\{[^{}]*\}")


def _label_from(raw: str | None, options: tuple[str, ...]) -> str | None:
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if isinstance(data, dict):
        label = data.get("label")
        if isinstance(label, str) and label in options:
            return label
    return None


def parse_label(
    tool_calls: dict[int, tuple[str, str]], content: str, tool_name: str, options: tuple[str, ...]
) -> str | None:
    """Strict: one consistent label from the tool call(s), else one JSON object in the text."""
    if tool_calls:
        labels = {
            _label_from(args, options)
            for name, args in tool_calls.values()
            if name in ("", tool_name)
        }
        return labels.pop() if len(labels) == 1 else None
    objects = _JSON_OBJECT.findall(content)
    labels = {_label_from(o, options) for o in objects}
    return labels.pop() if len(labels) == 1 else None


async def structured_label(
    model: ChatModel,
    *,
    system: str,
    user: str,
    tool_name: str,
    options: tuple[str, ...],
    timeout: float,
) -> str | None:
    """Ask ``model`` for exactly one of ``options``; ``None`` on any failure (logged).

    ``tool_name`` labels the call in logs and is the only tool-call name accepted.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    calls: dict[int, tuple[str, str]] = {}
    content: list[str] = []
    try:
        async with asyncio.timeout(timeout):
            async for delta in model.stream(
                messages,
                None,
                temperature=0.0,
                max_tokens=40,
            ):
                if delta.content:
                    content.append(delta.content)
                for frag in delta.tool_calls:
                    name, args = calls.get(frag.index, ("", ""))
                    calls[frag.index] = (name or frag.name or "", args + (frag.arguments or ""))
    except TimeoutError:
        log.warning("commitment: %s timed out after %.1fs", tool_name, timeout)
        return None
    except Exception:
        log.warning("commitment: %s call failed", tool_name, exc_info=True)
        return None
    label = parse_label(calls, "".join(content), tool_name, options)
    if label is None:
        log.warning(
            "commitment: unparseable %s output: calls=%r text=%r",
            tool_name,
            calls,
            "".join(content)[:200],
        )
    return label


@dataclass(frozen=True)
class AssentResult:
    label: AssentLabel
    # "precheck", "model" or "error" (timeout / failure / unparseable -> HEDGE).
    source: str
    latency_ms: int


async def classify_assent(
    model: ChatModel | None,
    readback_text: str,
    utterance: str,
    *,
    timeout: float = ASSENT_TIMEOUT_S,
) -> AssentResult:
    """Label the reply to a read-back. Only a model-confirmed ``affirmative`` can pass."""
    started = time.perf_counter()

    def done(label: AssentLabel, source: str) -> AssentResult:
        return AssentResult(label, source, round((time.perf_counter() - started) * 1000))

    early = precheck(utterance, readback_text)
    if early is not None:
        return done(early, "precheck")
    if model is None:
        return done(AssentLabel.HEDGE, "error")
    raw = await structured_label(
        model,
        system=ASSENT_SYSTEM_PROMPT,
        user=assent_user_prompt(readback_text, utterance),
        tool_name=ASSENT_TOOL,
        options=tuple(label.value for label in AssentLabel),
        timeout=timeout,
    )
    if raw is None:
        return done(AssentLabel.HEDGE, "error")
    return done(AssentLabel(raw), "model")


# --- directive: may a proposal start without a read-back? ---------------------------------


class DirectiveLabel(StrEnum):
    DIRECTIVE = "directive"
    EXPLORING = "exploring"


DIRECTIVE_SYSTEM_PROMPT = """\
You judge whether the user's latest message TELLS an assistant to go and do a piece of work \
now, or whether they are still exploring. On "directive" the work starts immediately without \
asking back, so when in any doubt answer "exploring".

Labels:
- directive: a plain instruction or request to do the work, possibly politely phrased \
("look into X and write it up", "build me a script that...", "check my disk space", "can you \
research Y for me", "yes, let's do that, build it" after the assistant suggested it).
- exploring: asking for the assistant's feedback, opinion, ideas or suggestions, weighing \
options, thinking out loud, asking a question about the topic, or anything hesitant \
("what do you think about building X?", "maybe we could research Y", "any ideas?", \
"should I...", "I'm thinking about...").

The work the assistant would start is given for context; judge only whether the user asked \
for it to be done now. Reply with only a JSON object: {"label": "directive"} or \
{"label": "exploring"}"""

DIRECTIVE_TOOL = "classify_directive"

# May only ever short-circuit AWAY from directive; never to it.
_EXPLORING = re.compile(
    r"\b(?:what do you think|your (?:thoughts|opinion|take|feedback)|any (?:ideas|thoughts)"
    r"|give me (?:some |new |more )?(?:\w+ )?(?:ideas|suggestions|feedback)"
    r"|should i|should we|maybe|perhaps|not sure|i wonder|wondering|what if"
    r"|thinking (?:about|of)|brainstorm|hold on|wait|not yet|don'?t)\b"
)


def directive_user_prompt(previous: str, utterance: str, goal: str) -> str:
    return (
        f'Assistant\'s previous message: "{" ".join(previous.split())}"\n'
        f'User\'s latest message: "{" ".join(utterance.split())}"\n'
        f'Work the assistant would start: "{goal}"\n'
        "Which label fits the user's latest message?"
    )


@dataclass(frozen=True)
class DirectiveResult:
    label: DirectiveLabel
    # "precheck", "model" or "error" (timeout / failure / unparseable -> EXPLORING).
    source: str
    latency_ms: int


async def classify_directive(
    model: ChatModel | None,
    previous: str,
    utterance: str,
    goal: str,
    *,
    timeout: float = ASSENT_TIMEOUT_S,
) -> DirectiveResult:
    """Label the user's request. Only a model-confirmed ``directive`` skips the read-back."""
    started = time.perf_counter()

    def done(label: DirectiveLabel, source: str) -> DirectiveResult:
        return DirectiveResult(label, source, round((time.perf_counter() - started) * 1000))

    text = " ".join(utterance.lower().replace("’", "'").split())
    if not text.strip(" .,!?…-") or _EXPLORING.search(text):
        return done(DirectiveLabel.EXPLORING, "precheck")
    if model is None:
        return done(DirectiveLabel.EXPLORING, "error")
    raw = await structured_label(
        model,
        system=DIRECTIVE_SYSTEM_PROMPT,
        user=directive_user_prompt(previous, utterance, goal),
        tool_name=DIRECTIVE_TOOL,
        options=tuple(label.value for label in DirectiveLabel),
        timeout=timeout,
    )
    if raw is None:
        return done(DirectiveLabel.EXPLORING, "error")
    return done(DirectiveLabel(raw), "model")


# --- ready score (hint + audit, never a trigger) -----------------------------------------

READY_OPTIONS = ("keep_talking", "ready_to_execute")
READY_TOOL = "answer_ready"
# SPEC §2, validated "ready" choice question.
READY_SYSTEM_PROMPT = """\
Decide whether this idea can be handed to a background worker right now.
- keep_talking: still exploratory - the goal or scope is not yet pinned down, or the user is \
still weighing options
- ready_to_execute: the goal and scope are explicit and the user has signalled agreement to \
proceed
Judge the latest user turn in the context of the conversation. Reply with only a JSON object \
and nothing else: {"label": "keep_talking"} or {"label": "ready_to_execute"}"""


def ready_user_prompt(transcript: list[tuple[str, str]]) -> str:
    lines = ["Conversation (oldest first):"]
    lines.extend(f"{role}: {' '.join(text.split())}" for role, text in transcript)
    return "\n".join(lines)


async def score_ready(
    model: ChatModel, transcript: list[tuple[str, str]], *, timeout: float = READY_TIMEOUT_S
) -> str | None:
    return await structured_label(
        model,
        system=READY_SYSTEM_PROMPT,
        user=ready_user_prompt(transcript),
        tool_name=READY_TOOL,
        options=READY_OPTIONS,
        timeout=timeout,
    )
