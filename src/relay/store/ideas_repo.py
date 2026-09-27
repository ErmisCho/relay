"""Idea-graph reads and writes behind the recall / idea-tracking tools (SPEC §6, §7).

Every function takes the async sessionmaker and opens its own short transaction, matching
``relay.executor.status.get_status``. Core ``update`` statements bypass the ORM ``onupdate``
hook, so ``ideas.updated_at`` is set explicitly wherever an idea changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import ColumnElement, Text, cast, func, literal_column, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from relay.store.models import (
    EDGE_RELATIONS,
    IDEAS_FTS_EXPR,
    Artifact,
    Commitment,
    Idea,
    IdeaEdge,
    Task,
    Turn,
)

Db = async_sessionmaker[AsyncSession]


@dataclass
class CommitmentInfo:
    goal: str
    artifact_kind: str
    assented_at: datetime


@dataclass
class TaskInfo:
    kind: str
    status: str
    goal: str


@dataclass
class ArtifactInfo:
    kind: str
    url: str
    summary: str | None


@dataclass
class EdgeInfo:
    """One idea edge seen from the digested idea; ``outgoing`` means this idea is ``from_idea``."""

    relation: str
    other_id: uuid.UUID
    other_title: str
    outgoing: bool


@dataclass
class IdeaDigest:
    id: uuid.UUID
    title: str
    summary: str | None
    status: str
    updated_at: datetime
    commitments: list[CommitmentInfo] = field(default_factory=list)
    tasks: list[TaskInfo] = field(default_factory=list)
    artifacts: list[ArtifactInfo] = field(default_factory=list)
    edges: list[EdgeInfo] = field(default_factory=list)


def _fts_document() -> ColumnElement[str]:
    # Must render exactly as the ``ix_ideas_fts`` index expression for the GIN index to apply.
    return literal_column(IDEAS_FTS_EXPR)


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def search_ideas(db: Db, query: str, limit: int = 3) -> list[Idea]:
    """Ideas matching ``query``, best first.

    Tries, in order: full-text with every term (``websearch_to_tsquery``), full-text with any
    term (a spoken question such as "where did we land on the routing thing" rarely has every
    word in the idea), then a substring match on the title. Ties break on recency.
    """
    query = query.strip()
    if not query:
        return []
    doc = _fts_document()
    all_terms = func.websearch_to_tsquery("english", query)
    any_term = func.to_tsquery(
        "english",
        func.replace(cast(func.plainto_tsquery("english", query), Text), "&", "|"),
    )
    async with db() as s:
        for tsq in (all_terms, any_term):
            rows = (
                await s.scalars(
                    select(Idea)
                    .where(doc.op("@@")(tsq))
                    .order_by(func.ts_rank(doc, tsq).desc(), Idea.updated_at.desc())
                    .limit(limit)
                )
            ).all()
            if rows:
                return list(rows)
        rows = (
            await s.scalars(
                select(Idea)
                .where(Idea.title.ilike(f"%{_escape_like(query)}%", escape="\\"))
                .order_by(Idea.updated_at.desc())
                .limit(limit)
            )
        ).all()
        return list(rows)


async def get_idea_digest(db: Db, idea_id: uuid.UUID) -> IdeaDigest | None:
    """Summary, status, commitments, tasks, artifacts and edges (both directions) of one idea."""
    async with db() as s:
        idea = await s.get(Idea, idea_id)
        if idea is None:
            return None
        digest = IdeaDigest(idea.id, idea.title, idea.summary, idea.status, idea.updated_at)
        commitments = (
            await s.scalars(
                select(Commitment)
                .where(Commitment.idea_id == idea_id)
                .order_by(Commitment.assented_at.desc())
            )
        ).all()
        digest.commitments = [
            CommitmentInfo(c.goal, c.artifact_kind, c.assented_at) for c in commitments
        ]
        task_rows = (
            await s.execute(
                select(Task.kind, Task.status, Commitment.goal)
                .join(Commitment, Task.commitment_id == Commitment.id)
                .where(Commitment.idea_id == idea_id)
                .order_by(Task.created_at.desc())
            )
        ).all()
        digest.tasks = [TaskInfo(k, st, g) for k, st, g in task_rows]
        artifact_rows = (
            await s.execute(
                select(Artifact.kind, Artifact.url, Artifact.summary)
                .join(Task, Artifact.task_id == Task.id)
                .join(Commitment, Task.commitment_id == Commitment.id)
                .where(Commitment.idea_id == idea_id)
                .order_by(Artifact.created_at.desc())
            )
        ).all()
        digest.artifacts = [ArtifactInfo(k, u, sm) for k, u, sm in artifact_rows]
        other = Idea.__table__.alias("other")
        edge_rows = (
            await s.execute(
                select(IdeaEdge.relation, IdeaEdge.from_idea, IdeaEdge.to_idea, other.c.title)
                .join(
                    other,
                    or_(
                        (IdeaEdge.from_idea == idea_id) & (other.c.id == IdeaEdge.to_idea),
                        (IdeaEdge.to_idea == idea_id) & (other.c.id == IdeaEdge.from_idea),
                    ),
                )
                .order_by(IdeaEdge.created_at)
            )
        ).all()
        for relation, from_id, to_id, title in edge_rows:
            outgoing = from_id == idea_id
            digest.edges.append(EdgeInfo(relation, to_id if outgoing else from_id, title, outgoing))
        return digest


async def get_idea_titles(db: Db, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """``{id: title}`` for the ids that exist."""
    async with db() as s:
        rows = (await s.execute(select(Idea.id, Idea.title).where(Idea.id.in_(ids)))).all()
    return {i: t for i, t in rows}


async def create_idea(db: Db, title: str) -> uuid.UUID:
    """Insert a new idea in status ``exploring`` and return its id."""
    async with db() as s, s.begin():
        idea = Idea(title=title.strip(), status="exploring")
        s.add(idea)
        await s.flush()
        return idea.id


async def link_ideas(db: Db, from_idea: uuid.UUID, to_idea: uuid.UUID, relation: str) -> bool:
    """Create the edge; idempotent. Returns False when it already existed."""
    if relation not in EDGE_RELATIONS:
        raise ValueError(f"unknown relation {relation!r}")
    async with db() as s, s.begin():
        result = await s.execute(
            insert(IdeaEdge)
            .values(from_idea=from_idea, to_idea=to_idea, relation=relation)
            .on_conflict_do_nothing()
            .returning(IdeaEdge.relation)
        )
        return result.first() is not None


async def recent_turns(db: Db, idea_id: uuid.UUID, n: int = 20) -> list[Turn]:
    """The last ``n`` user/assistant turns attributed to the idea, oldest first."""
    async with db() as s:
        rows = (
            await s.scalars(
                select(Turn)
                .where(Turn.idea_id == idea_id, Turn.role.in_(("user", "assistant")))
                .order_by(Turn.ts.desc(), Turn.id.desc())
                .limit(n)
            )
        ).all()
    return list(reversed(rows))


async def set_summary(db: Db, idea_id: uuid.UUID, summary: str) -> None:
    async with db() as s, s.begin():
        await s.execute(
            update(Idea).where(Idea.id == idea_id).values(summary=summary, updated_at=func.now())
        )


async def find_idea_by_title(db: Db, title: str) -> Idea | None:
    """Most recently updated idea whose title equals ``title`` ignoring case and outer spaces."""
    async with db() as s:
        return (
            await s.scalars(
                select(Idea)
                .where(func.lower(func.trim(Idea.title)) == title.strip().lower())
                .order_by(Idea.updated_at.desc())
                .limit(1)
            )
        ).first()


async def attribute_turn(db: Db, turn_id: uuid.UUID, idea_id: uuid.UUID) -> None:
    """(Re-)attribute an already-persisted turn to ``idea_id``.

    Used for the user turn that set or switched the focus: that turn belongs to the idea it
    moved the conversation to, even if it was persisted under the previous one.
    """
    async with db() as s, s.begin():
        await s.execute(update(Turn).where(Turn.id == turn_id).values(idea_id=idea_id))
