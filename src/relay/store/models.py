"""SQLAlchemy 2.x declarative models for the idea graph (SPEC section 7).

Deviation from SPEC section 7: routing/gate decisions live in `router_decisions` (one row per
backend per turn) instead of `turns.laya_intent/laya_ready/laya_confidence`.
The migration in `migrations/versions/0001_initial.py` is the schema of record; these models
mirror it for ORM use and Alembic autogenerate diffs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    Text,
    func,
)
from sqlalchemy import (
    text as sa_text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

IDEA_STATUSES = ("exploring", "committed", "executing", "delivered", "abandoned")
TASK_KINDS = ("code", "research")
TASK_STATUSES = ("queued", "running", "succeeded", "failed")
ARTIFACT_KINDS = ("pull_request", "document")
EDGE_RELATIONS = ("refines", "supersedes", "blocks", "spun_off_from")
TURN_ROUTES = ("small_local", "frontier")
TURN_ROLES = ("user", "assistant", "system", "tool")
ROUTER_BACKENDS = ("frontier", "laya", "llm")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def in_list(column: str, values: tuple[str, ...]) -> str:
    """SQL text for `column IN ('a', 'b', ...)` used by CHECK constraints."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


IDEAS_FTS_EXPR = "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(summary, ''))"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Idea(Base):
    __tablename__ = "ideas"
    __table_args__ = (
        CheckConstraint(in_list("status", IDEA_STATUSES), name="status"),
        Index("ix_ideas_fts", sa_text(IDEAS_FTS_EXPR), postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="exploring")
    # Free-form maturity score; intentionally nullable until the Delegator starts scoring ideas.
    maturity: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wake_trigger: Mapped[str | None] = mapped_column(Text)


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (
        CheckConstraint(in_list("role", TURN_ROLES), name="role"),
        CheckConstraint(f"route IS NULL OR {in_list('route', TURN_ROUTES)}", name="route"),
        Index("ix_turns_session_id_ts", "session_id", "ts"),
        Index("ix_turns_idea_id", "idea_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id"), nullable=False)
    idea_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ideas.id"))
    role: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    route: Mapped[str | None] = mapped_column(Text)
    model_used: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # `metadata` is reserved on declarative classes, so the attribute is `meta`.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default=sa_text("'{}'::jsonb")
    )


class Commitment(Base):
    __tablename__ = "commitments"
    __table_args__ = (Index("ix_commitments_idea_id", "idea_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    idea_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ideas.id"), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    scope_excludes: Mapped[str] = mapped_column(Text, nullable=False)
    artifact_kind: Mapped[str] = mapped_column(Text, nullable=False)
    readback_text: Mapped[str] = mapped_column(Text, nullable=False)
    # Commitment-protocol invariant: no dispatch without recorded spoken assent.
    assent_utterance: Mapped[str] = mapped_column(Text, nullable=False)
    assented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(in_list("kind", TASK_KINDS), name="kind"),
        CheckConstraint(in_list("status", TASK_STATUSES), name="status"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_commitment_id", "commitment_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    commitment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("commitments.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    dbos_workflow_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="queued")
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(in_list("kind", ARTIFACT_KINDS), name="kind"),
        Index("ix_artifacts_task_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class IdeaEdge(Base):
    __tablename__ = "idea_edges"
    __table_args__ = (
        PrimaryKeyConstraint("from_idea", "to_idea", "relation"),
        CheckConstraint(in_list("relation", EDGE_RELATIONS), name="relation"),
        # from_idea is covered by the PK's leading column; to_idea needs its own index.
        Index("ix_idea_edges_to_idea", "to_idea"),
    )

    from_idea: Mapped[uuid.UUID] = mapped_column(ForeignKey("ideas.id"), nullable=False)
    to_idea: Mapped[uuid.UUID] = mapped_column(ForeignKey("ideas.id"), nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class RouterDecision(Base):
    __tablename__ = "router_decisions"
    __table_args__ = (
        CheckConstraint(in_list("backend", ROUTER_BACKENDS), name="backend"),
        Index("ix_router_decisions_turn_id", "turn_id"),
        # At most one decision per turn may be the one that actually drove routing.
        Index(
            "uq_router_decisions_active",
            "turn_id",
            unique=True,
            postgresql_where=sa_text("is_active"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"), nullable=False)
    backend: Mapped[str] = mapped_column(Text, nullable=False)
    difficulty: Mapped[str | None] = mapped_column(Text)
    ready: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    # True for the backend whose decision actually drove routing on this turn.
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    created_at: Mapped[datetime] = _created_at()


class PendingReport(Base):
    __tablename__ = "pending_reports"
    __table_args__ = (
        Index("ix_pending_reports_session_id", "session_id"),
        Index("ix_pending_reports_idea_id", "idea_id"),
        Index("ix_pending_reports_task_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id"))
    idea_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ideas.id"))
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
