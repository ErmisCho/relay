"""Idea-graph tools: ``recall``, ``focus_idea`` and ``link_ideas`` (SPEC §6, §7).

Decision 2: an idea resumes across sessions only when the user asks for it (via ``recall`` and
then ``focus_idea``); there is no automatic "last focused idea".
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from relay.delegator.contracts import ToolContext, ToolResult
from relay.store import ideas_repo
from relay.store.ideas_repo import IdeaDigest
from relay.store.models import EDGE_RELATIONS

# ~1k tokens at ~4 characters per token.
RECALL_MAX_CHARS = 4000
RECALL_MAX_IDEAS = 3
MAX_TITLE_CHARS = 200
_ITEMS_PER_KIND = 3
_FIELD_CHARS = 300


def _clip(text: str | None, limit: int = _FIELD_CHARS) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value)) if value else None
    except ValueError:
        return None


def _edge_line(digest: IdeaDigest) -> str | None:
    parts = []
    for e in digest.edges[: _ITEMS_PER_KIND * 2]:
        other = f'"{_clip(e.other_title, 80)}" ({e.other_id})'
        relation = e.relation.replace("_", " ")
        parts.append(f"{relation} {other}" if e.outgoing else f"{other} {relation} this")
    return "Related: " + "; ".join(parts) + "." if parts else None


def format_digest(digest: IdeaDigest) -> list[str]:
    """Short speakable lines for one idea; the caller enforces the overall cap."""
    lines = [f'Idea "{_clip(digest.title, 120)}", {digest.status}, id {digest.id}.']
    lines.append(f"Summary: {_clip(digest.summary)}" if digest.summary else "No summary yet.")
    for c in digest.commitments[:_ITEMS_PER_KIND]:
        day = f"{c.assented_at:%B} {c.assented_at.day}"
        lines.append(f"Committed on {day}: {_clip(c.goal, 160)} ({c.artifact_kind}).")
    for t in digest.tasks[:_ITEMS_PER_KIND]:
        lines.append(f"{t.kind.capitalize()} task on {_clip(t.goal, 100)}: {t.status}.")
    for a in digest.artifacts[:_ITEMS_PER_KIND]:
        tail = f" {_clip(a.summary, 200)}" if a.summary else ""
        lines.append(f"{a.kind.replace('_', ' ').capitalize()}: {a.url}.{tail}")
    edge = _edge_line(digest)
    if edge:
        lines.append(edge)
    return lines


def render_recall(digests: list[IdeaDigest], max_chars: int = RECALL_MAX_CHARS) -> str:
    """Join digests line by line, never exceeding ``max_chars``."""
    out: list[str] = []
    used = 0
    for i, digest in enumerate(digests):
        for line in ([""] if i else []) + format_digest(digest):
            cost = len(line) + 1
            if used + cost > max_chars:
                return "\n".join(out).strip()
            out.append(line)
            used += cost
    return "\n".join(out).strip()


class RecallTool:
    """``InternalTool`` answering "what did we say about X" from the idea graph."""

    name = "recall"
    description = (
        "Look up ideas discussed in earlier conversations. Use it whenever the user refers to "
        'something from before ("where did we land on the routing thing?", "that bike lock '
        "idea\") or wants to pick an old idea back up. Returns each matching idea's id, status, "
        "latest summary, commitments, task statuses, delivered links and related ideas. To "
        "continue working on a result, call focus_idea with its id."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A few key words naming the idea, e.g. 'routing heuristics'.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    }

    def __init__(self, max_ideas: int = RECALL_MAX_IDEAS, max_chars: int = RECALL_MAX_CHARS):
        self._max_ideas = max_ideas
        self._max_chars = max_chars

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        query = str(args.get("query") or "").strip()
        if not query:
            return ToolResult("Recall needs a few words describing the idea.", rejected=True)
        ideas = await ideas_repo.search_ideas(ctx.db, query, self._max_ideas)
        digests = [
            d for i in ideas if (d := await ideas_repo.get_idea_digest(ctx.db, i.id)) is not None
        ]
        if not digests:
            return ToolResult("Nothing about that in earlier conversations.")
        return ToolResult(render_recall(digests, self._max_chars))


# Words that do not distinguish one idea title from another ("the bike lock idea").
_TITLE_FILLER = frozenset(
    "a an the my our your this that idea ideas concept plan project new for of to and with on in "
    "about using use via based by from as at is it its".split()
)


def title_tokens(title: str) -> frozenset[str]:
    """Normalised content words of a title: lowercase, alphanumeric, crude plural folding."""
    words = re.findall(r"[0-9a-z]+", title.lower())
    return frozenset(
        w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
        for w in words
        if w not in _TITLE_FILLER
    )


def same_idea_title(new: str, current: str) -> bool:
    """True when ``new`` (a title the model proposes) names the ``current`` idea.

    Deliberately strict so spin-offs still become their own ideas: the content words must be
    near-equal (Jaccard >= 0.8), or ``new`` must add no content word to ``current`` while
    covering at least half of it. "Bike lock idea" vs "Phone-proximity bike lock" is the same
    idea; "Bike lock marketing plan" vs "Bike lock" (adds "marketing") and "Phone app" vs
    "Phone-proximity bike lock app" (covers too little) are not.
    """
    tn, tc = title_tokens(new), title_tokens(current)
    if not tn or not tc:
        return new.strip().lower() == current.strip().lower()
    if len(tn & tc) >= 0.8 * len(tn | tc):
        return True
    return tn <= tc and 2 * len(tn) >= len(tc)


_NO_REFOCUS = "Do not call focus_idea again; answer the user."


class FocusIdeaTool:
    """``InternalTool`` setting the idea the conversation is about, so turns are attributed.

    Guards against a model that calls it every turn: re-focusing the current idea, or passing a
    ``new_title`` that names the current idea, is a no-op; a ``new_title`` equal to an existing
    idea's title (ignoring case) reuses that idea instead of creating a duplicate.
    """

    name = "focus_idea"
    description = (
        "Set which idea the conversation is about. Call it ONLY when the conversation starts a "
        "new idea or clearly switches to a different one; never to confirm or repeat the "
        "current idea. Pass idea_id for an existing idea (from recall), or new_title for a "
        "brand-new idea. Later turns and commitments are filed under it."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "idea_id": {"type": "string", "description": "UUID of an existing idea."},
            "new_title": {
                "type": "string",
                "description": "Short title for a new idea, e.g. 'Phone-proximity bike lock'.",
            },
        },
        "additionalProperties": False,
    }

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_id = args.get("idea_id")
        title = " ".join(str(args.get("new_title") or "").split())[:MAX_TITLE_CHARS]
        current = ctx.state.current_idea_id
        if raw_id:
            idea_id = _parse_uuid(raw_id)
            if idea_id is None:
                return ToolResult("That idea id is not valid.", rejected=True)
            if idea_id == current:
                return ToolResult(f"Already on that idea. {_NO_REFOCUS}")
            titles = await ideas_repo.get_idea_titles(ctx.db, [idea_id])
            if idea_id not in titles:
                return ToolResult("There is no idea with that id.", rejected=True)
            message = f'Now focused on "{titles[idea_id]}".'
        elif title:
            if current is not None:
                current_title = (await ideas_repo.get_idea_titles(ctx.db, [current])).get(current)
                if current_title is not None and same_idea_title(title, current_title):
                    return ToolResult(f'Already on "{current_title}". {_NO_REFOCUS}')
            existing = await ideas_repo.find_idea_by_title(ctx.db, title)
            if existing is not None:
                if existing.id == current:
                    return ToolResult(f'Already on "{existing.title}". {_NO_REFOCUS}')
                idea_id = existing.id
                message = f'Back on the existing idea "{existing.title}", id {idea_id}.'
            else:
                idea_id = await ideas_repo.create_idea(ctx.db, title)
                message = (
                    f'Started a new idea, "{title}", id {idea_id}. It stays the current idea; '
                    "do not call focus_idea again until the user moves to a different idea."
                )
        else:
            return ToolResult("Pass idea_id or new_title.", rejected=True)
        ctx.state.current_idea_id = idea_id
        if ctx.user_turn_id is not None:
            await ideas_repo.attribute_turn(ctx.db, ctx.user_turn_id, idea_id)
        return ToolResult(message)


class LinkIdeasTool:
    """``InternalTool`` recording how two ideas relate (``idea_edges``)."""

    name = "link_ideas"
    description = (
        "Record how two ideas relate, when the user says one idea refines, supersedes, blocks "
        "or was spun off from another. Reads as: from_idea <relation> to_idea."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "from_idea_id": {
                "type": "string",
                "description": "UUID of the idea doing the relating.",
            },
            "to_idea_id": {"type": "string", "description": "UUID of the idea related to."},
            "relation": {"type": "string", "enum": list(EDGE_RELATIONS)},
        },
        "required": ["from_idea_id", "to_idea_id", "relation"],
        "additionalProperties": False,
    }

    async def __call__(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        relation = args.get("relation")
        if relation not in EDGE_RELATIONS:
            allowed = ", ".join(r.replace("_", " ") for r in EDGE_RELATIONS)
            return ToolResult(f"Ideas can only be linked as: {allowed}.", rejected=True)
        from_id, to_id = _parse_uuid(args.get("from_idea_id")), _parse_uuid(args.get("to_idea_id"))
        if from_id is None or to_id is None:
            return ToolResult("Both idea ids are needed and must be valid.", rejected=True)
        if from_id == to_id:
            return ToolResult("An idea cannot be linked to itself.", rejected=True)
        titles = await ideas_repo.get_idea_titles(ctx.db, [from_id, to_id])
        if from_id not in titles or to_id not in titles:
            return ToolResult("One of those ideas does not exist.", rejected=True)
        created = await ideas_repo.link_ideas(ctx.db, from_id, to_id, str(relation))
        sentence = f'"{titles[from_id]}" {relation.replace("_", " ")} "{titles[to_id]}"'
        return ToolResult(f"Noted: {sentence}." if created else f"Already noted: {sentence}.")
