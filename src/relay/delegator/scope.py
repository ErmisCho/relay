"""The v1 scope boundary (SPEC §1 "unbounded action space", §3): two verticals, hard refusal.

Three layers, defence in depth:

1. ``prompts/system.md`` (rendered by :func:`render_scope_prompt`) tells the model what is in
   scope and exactly how to refuse. This is the primary layer.
2. :func:`validate_commitment_args` / :func:`scope_guard` reject any commitment whose kind or
   artifact is outside the enums, mismatched, or not enabled, whatever the model emitted.
3. :func:`detect_out_of_scope` is a deterministic, precision-first backstop that flags clear
   out-of-scope action requests so the turn hook can add a pointed refusal note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from relay.config import Settings
from relay.delegator.contracts import ToolContext, ToolResult
from relay.store.models import ARTIFACT_KINDS, TASK_KINDS

PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"

#: ``SessionState.extra`` key: category of the current user turn's out-of-scope request, set by
#: ``ScopeHook`` and cleared on the next in-scope user turn. Informational only: it never vetoes
#: a commitment (see :func:`scope_guard`).
REFUSED_KEY = "scope.refused"

#: The exact opening of every spoken refusal (SPEC §3).
REFUSAL_PHRASE = "I can't do that yet"


class Kind(StrEnum):
    RESEARCH = "research"
    CODE = "code"


class ArtifactKind(StrEnum):
    DOCUMENT = "document"
    PULL_REQUEST = "pull_request"


#: Each vertical has exactly one terminal artifact.
ARTIFACT_FOR_KIND: dict[Kind, ArtifactKind] = {
    Kind.RESEARCH: ArtifactKind.DOCUMENT,
    Kind.CODE: ArtifactKind.PULL_REQUEST,
}
KIND_FOR_ARTIFACT: dict[ArtifactKind, Kind] = {a: k for k, a in ARTIFACT_FOR_KIND.items()}

# The enums must mirror the store's CHECK constraints; fail at import if they drift.
assert {k.value for k in Kind} == set(TASK_KINDS), "Kind drifted from store TASK_KINDS"
assert {a.value for a in ArtifactKind} == set(ARTIFACT_KINDS), "ArtifactKind drifted"

_VERTICAL_TEXT: dict[Kind, str] = {
    Kind.RESEARCH: (
        "research and writing: web research, synthesis, briefs, drafts, competitive analysis. "
        "Terminal artifact: a Markdown document, never sent or published."
    ),
    Kind.CODE: (
        "code and repos: write code, refactor, run tests. "
        "Terminal artifact: a draft pull request on a branch, never merged."
    ),
}


def enabled_kinds(settings: Settings) -> frozenset[Kind]:
    """The ENABLED_KINDS gate: verticals that may be proposed and dispatched right now."""
    return frozenset(Kind(k) for k in settings.enabled_kinds)


# --- Layer 1: system prompt -------------------------------------------------------------


@lru_cache(maxsize=1)
def _prompt_template() -> Template:
    return Template(PROMPT_PATH.read_text(encoding="utf-8"))


def render_scope_prompt(settings: Settings) -> str:
    """The Delegator's scope rules, rendered with the currently enabled kinds."""
    enabled = enabled_kinds(settings)
    on = [k for k in Kind if k in enabled]
    off = [k for k in Kind if k not in enabled]
    in_scope = "\n".join(f"- {_VERTICAL_TEXT[k]}" for k in on) or "- nothing is enabled yet"
    not_enabled = (
        "Not enabled yet, so refuse these too:\n"
        + "\n".join(f"- {_VERTICAL_TEXT[k].split(':', 1)[0]}" for k in off)
        + "\n"
        if off
        else ""
    )
    return (
        _prompt_template()
        .substitute(
            in_scope=in_scope,
            not_enabled=not_enabled,
            refusal=REFUSAL_PHRASE,
            example_action="send emails",
        )
        .strip()
    )


# --- Layer 2: server-side commitment validation -----------------------------------------


class CommitmentScope(BaseModel):
    """The scope-relevant part of ``propose_commitment`` / ``dispatch_task`` arguments.

    ``kind`` is optional because ``propose_commitment`` only carries ``artifact_kind``; it is
    derived from the artifact then. When both are given they must be the matching pair.
    Other arguments (goal, idea_id, ...) are ignored here.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    artifact_kind: ArtifactKind
    kind: Kind | None = None

    @model_validator(mode="after")
    def _pair_matches(self) -> CommitmentScope:
        if self.kind is not None and ARTIFACT_FOR_KIND[self.kind] is not self.artifact_kind:
            raise ValueError(
                f"kind {self.kind.value!r} produces {ARTIFACT_FOR_KIND[self.kind].value!r}, "
                f"not {self.artifact_kind.value!r}"
            )
        return self

    @property
    def resolved_kind(self) -> Kind:
        return self.kind if self.kind is not None else KIND_FOR_ARTIFACT[self.artifact_kind]


@dataclass(frozen=True)
class ScopeDecision:
    """Outcome of :func:`validate_commitment_args`."""

    allowed: bool
    kind: Kind | None = None
    artifact_kind: ArtifactKind | None = None
    # Machine-readable reason for logs / turn metadata; None when allowed.
    reason: str | None = None
    # Instruction for the model, to be returned as a rejected ToolResult; None when allowed.
    refusal: str | None = None

    def to_tool_result(self) -> ToolResult | None:
        """``ToolResult(rejected=True)`` for a refusal, ``None`` when the call may proceed."""
        if self.allowed:
            return None
        return ToolResult(content=self.refusal or refusal_instruction(), rejected=True)


def refusal_instruction(detail: str | None = None) -> str:
    """Text telling the model to refuse out loud instead of proposing."""
    why = f" ({detail})" if detail else ""
    return (
        f"REJECTED: this request is out of scope{why}. Nothing was proposed or dispatched. "
        f'Tell the user in one short sentence starting with "{REFUSAL_PHRASE}", '
        "adding that it might be supported in a future version. "
        "Do not retry this tool for it and do not offer a workaround."
    )


def validate_commitment_args(args: dict[str, Any], settings: Settings) -> ScopeDecision:
    """Server-side scope check for a commitment, independent of what the model believes.

    Rejects: a kind or artifact_kind outside the enums, a mismatched pair (e.g. research +
    pull_request), and a kind not in ``settings.enabled_kinds``.
    """
    try:
        scope = CommitmentScope.model_validate(args)
    except ValidationError as exc:
        fields = sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]}) or ["arguments"]
        reason = "invalid_scope:" + ",".join(fields)
        detail = "; ".join(e["msg"] for e in exc.errors())
        return ScopeDecision(
            allowed=False, reason=reason, refusal=refusal_instruction(f"{reason}: {detail}")
        )
    kind = scope.resolved_kind
    if kind not in enabled_kinds(settings):
        reason = f"kind_not_enabled:{kind.value}"
        return ScopeDecision(
            allowed=False,
            kind=kind,
            artifact_kind=scope.artifact_kind,
            reason=reason,
            refusal=refusal_instruction(f"{kind.value} work is not enabled yet"),
        )
    return ScopeDecision(allowed=True, kind=kind, artifact_kind=scope.artifact_kind)


def scope_guard(args: dict[str, Any], ctx: ToolContext) -> ToolResult | None:
    """First line of every commitment tool: a rejected ToolResult, or None to proceed.

    Relies on :func:`validate_commitment_args` alone. The detector's per-turn flag
    (``REFUSED_KEY``) deliberately does NOT veto here: a false positive would block a
    legitimate proposal or an assent turn, and the enums already bound the artifact.

    Usage inside a tool::

        async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            if (refused := scope_guard(args, ctx)) is not None:
                return refused
            ...
    """
    return validate_commitment_args(args, ctx.settings).to_tool_result()


# --- Layer 3: deterministic out-of-scope detection --------------------------------------


class Category(StrEnum):
    EMAIL = "email"
    CALENDAR = "calendar"
    MESSAGING = "messaging"
    BROWSER = "browser"
    PURCHASE = "purchase"
    IRREVERSIBLE = "irreversible"


@dataclass(frozen=True)
class OutOfScope:
    category: Category
    # The span of the utterance that triggered detection.
    matched: str

    @property
    def reason(self) -> str:
        return f"{self.category.value}: {self.matched}"


# Someone else (never the user themself: "message me a summary" is our own report path).
_OBJ = (
    r"(?:him|her|them|my \w+|our \w+|his \w+|their \w+|the (?:team|client|group)|everyone|mom|dad)"
)
_PRODUCTS = (
    r"(?:tickets?|flights?|laptops?|phones?|books?|domains?|subscriptions?|licen[cs]es?|shares?"
    r"|stocks?|crypto|bitcoin|gifts?|groceries|food|pizza|coffee|shoes|clothes|headphones"
    r"|monitors?|keyboards?|cars?|plan|seats?|gift cards?|products?|items?)"
)

# (category, pattern, hard). A soft rule is suppressed when the sentence is a research or
# writing request ("visit the X website and summarize it", "... and send it to the team"):
# we produce the document, the user shares it. Hard rules name an explicit outbound artifact
# (an email, a message to a person, an invite, a payment) and always fire.
_RULES: list[tuple[Category, str, bool]] = [
    # email
    (Category.EMAIL, r"\b(?:send|shoot|fire off|forward)\b.{0,40}\be-?mails?\b", True),
    (Category.EMAIL, r"\b(?:reply|respond|answer|write back)\b.{0,30}\be-?mails?\b", True),
    (Category.EMAIL, rf"^e-?mail\s+{_OBJ}\b", True),
    (Category.EMAIL, r"\b(?:draft|write|compose)\b.{0,20}\be-?mail (?:to|for)\b", True),
    # calendar / scheduling / bookings
    (
        Category.CALENDAR,
        r"^(?:schedule|book|set up|arrange|reschedule|cancel)\b.{0,40}"
        r"\b(?:meetings?|appointments?|invites?|reservations?|a table|flights?|hotels?"
        r"|(?:a|the) call with)\b",
        True,
    ),
    (
        Category.CALENDAR,
        r"\b(?:add|put|block)\b.{0,40}\b(?:to|on|in|off) (?:my|our) calendar\b",
        True,
    ),
    (Category.CALENDAR, r"\bsend\b.{0,30}\b(?:calendar )?invites?\b", True),
    # messaging / social
    (Category.MESSAGING, rf"^(?:text|message|dm|ping|slack)\s+{_OBJ}\b", True),
    (Category.MESSAGING, r"^tweet\s+(?:this|that|it|about)\b", True),
    (Category.MESSAGING, r"\bsend\b.{0,30}\b(?:sms|text message|dm)\b", True),
    (
        Category.MESSAGING,
        r"^(?:post|share|announce|message|dm|ping|send)\b.{0,50}"
        r"\b(?:on|in|to) (?:the )?(?:\w+ )?(?:slack|teams|twitter|linkedin|facebook|instagram"
        r"|reddit|discord|whatsapp|telegram)\b",
        False,
    ),
    # browser / computer / GUI: an explicit UI object, never just "site" or "page"
    (
        Category.BROWSER,
        r"^(?:open|launch|start)\s+(?:up\s+)?(?:the\s+|my\s+|a\s+)?(?:web\s+)?"
        r"(?:browser|chrome|safari|firefox|edge)\b",
        False,
    ),
    (
        Category.BROWSER,
        r"\bclick(?:\s+on)?\s+(?:the|that|this|a)\s+(?:\w+\s+){0,2}"
        r"(?:button|link|checkbox|icon|menu|tab)\b",
        False,
    ),
    (Category.BROWSER, r"^(?:fill (?:out|in)|submit)\b.{0,30}\b(?:form|application)\b", False),
    (Category.BROWSER, r"^(?:log|sign) ?(?:in(?:to)?|on to)\s+(?:my|our)\b", False),
    (
        Category.BROWSER,
        r"^(?:control|drive|automate)\s+(?:my|the)\s+"
        r"(?:computer|screen|mouse|browser|mac|laptop|desktop)\b",
        False,
    ),
    # purchases / payments: a product or payment object is required
    (
        Category.PURCHASE,
        rf"^(?:buy|purchase|order)\s+(?:me\s+|us\s+)?(?:\w+\s+){{0,4}}?{_PRODUCTS}\b",
        True,
    ),
    (Category.PURCHASE, r"^(?:buy|order)\b.{0,40}\b(?:on amazon|online)\b", True),
    (
        Category.PURCHASE,
        r"^pay\s+(?:for\s+)?(?:the|my|this|that)\s+(?:\w+\s+)?(?:bill|invoice|rent|order)\b",
        True,
    ),
    (
        Category.PURCHASE,
        r"^(?:pay|venmo|wire|transfer|send)\b.{0,20}(?:\$|\bdollars\b|\bmoney\b)",
        True,
    ),
    # other outward-facing or irreversible actions
    (
        Category.IRREVERSIBLE,
        r"^merge\b.{0,30}\b(?:pr|pull request|branch|into main|into master)\b",
        True,
    ),
    (
        Category.IRREVERSIBLE,
        r"^(?:deploy|ship|release|push)\b.{0,30}\b(?:prod|production|live)\b",
        False,
    ),
    (Category.IRREVERSIBLE, r"^push\b.{0,20}\bto (?:main|master)\b", False),
    (Category.IRREVERSIBLE, r"^publish\b.{0,30}\b(?:post|article|blog|it|this|that|page)\b", False),
    (
        Category.IRREVERSIBLE,
        r"^(?:delete|wipe|erase|remove)\s+(?:all\s+)?(?:of\s+)?(?:my|our)\s+(?:\w+\s+)?"
        r"(?:e-?mails?|files?|accounts?|photos?|messages?|inbox)\b",
        True,
    ),
    (Category.IRREVERSIBLE, r"^(?:call|phone|ring)\s+(?:him|her|them|my \w+|mom|dad)\b", True),
]
_COMPILED = [(cat, re.compile(p), hard) for cat, p, hard in _RULES]

# Words before the action in the same clause that turn it into talk ABOUT the action, or
# into something the USER will do ("I'll push it later"), not a request to us.
_FRAMING = re.compile(
    r"\b(?:how|why|what|whether|which|when|about|research|compare|explain|summari[sz]e"
    r"|analy[sz]e|study|article|essay|report|brief|doc(?:ument)?|guide|write-?up|best"
    r"|tips|ways|history|idea for|app that|tool that|feature"
    r"|i'll|i will|i'm|i am|i want to|i need to|i'd like to|i plan to|i might|i may|i could"
    r"|we'll|we will|we want to|we need to|we're)\b"
)
_NEGATION = re.compile(r"\b(?:don'?t|do not|won'?t|never|not|without|no need to)\b")
# Research / writing requests: soft rules do not fire anywhere in such a sentence.
_RESEARCH = re.compile(
    r"\b(?:research|summari[sz]e|summary|brief|look into|read up|dig into|compare"
    r"|analy[sz]e|analysis|report|write-?up|write (?:up|me)|find out|literature"
    r"|investigate|doc(?:ument)?|options)\b"
)
# Politeness / delegation lead-ins stripped so imperatives anchor at the clause start.
_LEAD_IN = re.compile(
    r"^(?:(?:hey|ok(?:ay)?|so|yes|yeah|and|then|also|now|just|please|relay)\b[\s,]*)*"
    r"(?:(?:can|could|would|will) you\s+|i (?:want|need|'d like) you to\s+"
    r"|go ahead and\s+|let's\s+|help (?:me|us)\s+)?"
    r"(?:please\s+|just\s+)?"
)
_SENTENCES = re.compile(r"[.;!?]+")
_CLAUSES = re.compile(r",+|\s(?:and then|then|and|but)\s")


def _normalise(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split())


def detect_out_of_scope(user_text: str) -> OutOfScope | None:
    """Flag clear requests to take an out-of-scope action; ``None`` otherwise.

    Precision over recall: the system prompt is the primary layer. Rules only match an
    imperative at the start of a clause with a concrete action object; anything framed as
    talk about the action ("research email deliverability"), as the user's own plan ("I'll
    push it to main later") or negated ("don't email him yet") is left alone, and in a
    research or writing request only rules naming an explicit outbound artifact fire.
    """
    for sentence in _SENTENCES.split(_normalise(user_text)):
        research = _RESEARCH.search(sentence) is not None
        for clause in _CLAUSES.split(sentence):
            clause = _LEAD_IN.sub("", clause.strip())
            if not clause:
                continue
            for category, pattern, hard in _COMPILED:
                if research and not hard:
                    continue
                match = pattern.search(clause)
                if match is None:
                    continue
                prefix = clause[: match.start()]
                if _FRAMING.search(prefix) or _NEGATION.search(prefix):
                    continue
                return OutOfScope(category=category, matched=match.group(0))
    return None
