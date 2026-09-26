"""Completion reports (SPEC §6, decision 3): announce finished tasks at a turn boundary.

Flow, only on requests whose last message is a *user* message (a new agent turn; tool-result
follow-ups inside the same turn are ignored):

1. ``before_model`` first *confirms* reports offered on the previous turn (see below);
   confirmed reports get ``delivered_at``.
2. It then offers every still-undelivered report (oldest first, regardless of which session
   dispatched the task) as a system note and sets ``offered_at``. Notes are only injected
   before the model runs, so an announcement can only land at the start of an agent turn,
   never in the middle of a response.
3. ``after_response`` receives exactly what was generated/streamed for the turn (partial on
   barge-in) and records, per offered report, the generated text up to and including the
   end of the announcement.

Delivery rule (exact, no fuzzy matching). The note asks the model to say the report's
one-sentence summary *verbatim*; that sentence is the announcement. Texts are normalised
(lowercase, punctuation to spaces, whitespace collapsed). A report is delivered iff

* the announcement occurs in the generated text of the previous turn, AND
* the previous assistant message as ElevenLabs recorded it (what was actually spoken; it is
  truncated on barge-in) starts with the generated text up to the end of the announcement.

A paraphrase is therefore never confirmed, nor is an on-topic reply without the sentence,
nor an announcement cut off before its last word. Unconfirmed reports stay pending and are
re-offered with a BRIEF note, at most ``MAX_REOFFERS`` times; after that the report is marked
delivered anyway and logged (the artifact link stays in the idea graph / ``get_status``).
Offer counts live in session state, so a Delegator restart resets them to "offered once".
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from relay.delegator.contracts import TurnContext
from relay.store.models import PendingReport

log = logging.getLogger(__name__)

OFFERED_KEY = "reports.offered"  # {report_id: summary} offered on the latest turn
OFFERS_KEY = "reports.offers"  # {report_id: times offered}
GENERATED_KEY = "reports.generated"  # {report_id: normalised generated text through announcement}
MAX_REOFFERS = 2
MAX_REPORTS_PER_TURN = 3


def normalise(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def announcement_prefix(summary: str, generated: str) -> str | None:
    """Normalised ``generated`` up to and including the announcement, or None if absent."""
    needle, hay = normalise(summary), normalise(generated)
    if not needle:
        return None
    at = hay.find(needle)
    return None if at < 0 else hay[: at + len(needle)]


def was_spoken(prefix: str, recorded: str) -> bool:
    """True when the recorded (spoken) message reaches past the end of the announcement."""
    return normalise(recorded).startswith(prefix)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("text")
        )
    return ""


def previous_assistant_text(messages: list[dict[str, Any]]) -> str:
    """Assistant text of the previous turn: assistant messages between the last two user
    messages, joined in order (tool-call messages without text contribute nothing)."""
    parts: list[str] = []
    for msg in reversed(messages[:-1]):
        if msg.get("role") == "user":
            break
        if msg.get("role") == "assistant":
            parts.append(_text(msg.get("content")))
    return " ".join(reversed([p for p in parts if p]))


def full_note(summary: str) -> str:
    return (
        f'A delegated task finished. At a natural point, tell the user this sentence verbatim: "'
        f'{summary}" The document link is stored in the idea.'
    )


def brief_note(summary: str, cut_off: bool) -> str:
    why = "Your earlier mention was cut off." if cut_off else "Not mentioned yet."
    return f'{why} Briefly say verbatim: "{summary}"'


class ReportsHook:
    """``TurnHook`` announcing completed tasks; construct with no arguments."""

    async def before_model(self, ctx: TurnContext) -> list[str]:
        if not ctx.messages or ctx.messages[-1].get("role") != "user":
            return []  # tool-result follow-up within the same agent turn
        extra = ctx.state.extra
        now = datetime.now(UTC)
        recorded = previous_assistant_text(ctx.messages)
        generated: dict[str, str] = extra.get(GENERATED_KEY, {})
        offers: dict[str, int] = extra.setdefault(OFFERS_KEY, {})

        async with ctx.db() as session, session.begin():
            reports = (
                await session.scalars(
                    select(PendingReport)
                    .where(PendingReport.delivered_at.is_(None))
                    .order_by(PendingReport.created_at, PendingReport.id)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            to_offer: list[PendingReport] = []
            for report in reports:
                rid = str(report.id)
                prefix = generated.get(rid)
                if report.offered_at is not None and prefix and was_spoken(prefix, recorded):
                    report.delivered_at = now
                    offers.pop(rid, None)
                elif report.offered_at is not None and offers.get(rid, 1) > MAX_REOFFERS:
                    report.delivered_at = now
                    log.info(
                        "report %s never confirmed as spoken after %d offers; marking delivered",
                        rid,
                        offers.pop(rid, 1),
                    )
                else:
                    to_offer.append(report)
            to_offer = to_offer[:MAX_REPORTS_PER_TURN]
            notes = [
                full_note(r.summary)
                if r.offered_at is None
                else brief_note(r.summary, cut_off=str(r.id) in generated)
                for r in to_offer
            ]
            for r in to_offer:
                rid = str(r.id)
                offers[rid] = offers.get(rid, 1 if r.offered_at is not None else 0) + 1
            if to_offer:
                await session.execute(
                    update(PendingReport)
                    .where(PendingReport.id.in_([r.id for r in to_offer]))
                    .values(offered_at=now)
                )
        extra[OFFERED_KEY] = {str(r.id): r.summary for r in to_offer}
        extra[GENERATED_KEY] = {}
        return notes

    async def after_response(self, ctx: TurnContext, assistant_text: str) -> None:
        offered: dict[str, str] = ctx.state.extra.get(OFFERED_KEY, {})
        found: dict[str, str] = {}
        for rid, summary in offered.items():
            prefix = announcement_prefix(summary, assistant_text)
            if prefix is not None:
                found[rid] = prefix
        ctx.state.extra[GENERATED_KEY] = found
