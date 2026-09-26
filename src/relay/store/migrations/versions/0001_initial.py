"""Initial idea-graph schema (SPEC section 7, with router_decisions + pending_reports).

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

from relay.store.models import (
    EDGE_RELATIONS,
    IDEA_STATUSES,
    IDEAS_FTS_EXPR,
    ROUTER_BACKENDS,
    TASK_KINDS,
    TASK_STATUSES,
    TURN_ROLES,
    TURN_ROUTES,
    in_list,
)

# Frozen: 0004 widened the CHECK; this revision keeps the list it originally created.
ARTIFACT_KINDS_0001 = ("pull_request", "document")

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TSTZ = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def _id() -> sa.Column[Any]:
    return sa.Column(
        "id", UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")
    )


def _created_at() -> sa.Column[Any]:
    return sa.Column("created_at", TSTZ, nullable=False, server_default=NOW)


def upgrade() -> None:
    # CHECK names use op.f(): they are already final, so the metadata naming convention
    # (ck_%(table_name)s_%(constraint_name)s) must not prefix them again.
    op.create_table(
        "ideas",
        _id(),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="exploring"),
        sa.Column("maturity", sa.Integer()),
        _created_at(),
        sa.Column("updated_at", TSTZ, nullable=False, server_default=NOW),
        sa.CheckConstraint(in_list("status", IDEA_STATUSES), name=op.f("ck_ideas_status")),
    )
    op.execute(f"CREATE INDEX ix_ideas_fts ON ideas USING gin ({IDEAS_FTS_EXPR})")

    op.create_table(
        "sessions",
        _id(),
        sa.Column("started_at", TSTZ, nullable=False, server_default=NOW),
        sa.Column("ended_at", TSTZ),
        sa.Column("wake_trigger", sa.Text()),
    )

    op.create_table(
        "turns",
        _id(),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id"), nullable=False),
        sa.Column("idea_id", UUID(as_uuid=True), sa.ForeignKey("ideas.id")),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("ts", TSTZ, nullable=False, server_default=NOW),
        sa.Column("route", sa.Text()),
        sa.Column("model_used", sa.Text()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("metadata", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.CheckConstraint(in_list("role", TURN_ROLES), name=op.f("ck_turns_role")),
        sa.CheckConstraint(
            f"route IS NULL OR {in_list('route', TURN_ROUTES)}", name=op.f("ck_turns_route")
        ),
    )
    op.create_index("ix_turns_session_id_ts", "turns", ["session_id", "ts"])
    op.create_index("ix_turns_idea_id", "turns", ["idea_id"])

    op.create_table(
        "commitments",
        _id(),
        sa.Column("idea_id", UUID(as_uuid=True), sa.ForeignKey("ideas.id"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("scope_excludes", sa.Text(), nullable=False),
        sa.Column("artifact_kind", sa.Text(), nullable=False),
        sa.Column("readback_text", sa.Text(), nullable=False),
        sa.Column("assent_utterance", sa.Text(), nullable=False),
        sa.Column("assented_at", TSTZ, nullable=False),
        _created_at(),
    )
    op.create_index("ix_commitments_idea_id", "commitments", ["idea_id"])

    op.create_table(
        "tasks",
        _id(),
        sa.Column(
            "commitment_id", UUID(as_uuid=True), sa.ForeignKey("commitments.id"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("dbos_workflow_id", sa.Text(), unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("started_at", TSTZ),
        sa.Column("finished_at", TSTZ),
        sa.Column("error", sa.Text()),
        _created_at(),
        sa.CheckConstraint(in_list("kind", TASK_KINDS), name=op.f("ck_tasks_kind")),
        sa.CheckConstraint(in_list("status", TASK_STATUSES), name=op.f("ck_tasks_status")),
    )
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_commitment_id", "tasks", ["commitment_id"])

    op.create_table(
        "artifacts",
        _id(),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text()),
        sa.Column("reviewed_at", TSTZ),
        _created_at(),
        sa.CheckConstraint(in_list("kind", ARTIFACT_KINDS_0001), name=op.f("ck_artifacts_kind")),
    )
    op.create_index("ix_artifacts_task_id", "artifacts", ["task_id"])

    op.create_table(
        "idea_edges",
        sa.Column("from_idea", UUID(as_uuid=True), sa.ForeignKey("ideas.id"), nullable=False),
        sa.Column("to_idea", UUID(as_uuid=True), sa.ForeignKey("ideas.id"), nullable=False),
        sa.Column("relation", sa.Text(), nullable=False),
        _created_at(),
        sa.PrimaryKeyConstraint("from_idea", "to_idea", "relation"),
        sa.CheckConstraint(
            in_list("relation", EDGE_RELATIONS), name=op.f("ck_idea_edges_relation")
        ),
    )
    op.create_index("ix_idea_edges_to_idea", "idea_edges", ["to_idea"])

    op.create_table(
        "router_decisions",
        _id(),
        sa.Column("turn_id", UUID(as_uuid=True), sa.ForeignKey("turns.id"), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column("difficulty", sa.Text()),
        sa.Column("ready", sa.Text()),
        sa.Column("intent", sa.Text()),
        sa.Column("confidence", sa.Float()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        _created_at(),
        sa.CheckConstraint(
            in_list("backend", ROUTER_BACKENDS), name=op.f("ck_router_decisions_backend")
        ),
    )
    op.create_index("ix_router_decisions_turn_id", "router_decisions", ["turn_id"])
    # At most one decision per turn may be the one that actually drove routing.
    op.create_index(
        "uq_router_decisions_active",
        "router_decisions",
        ["turn_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    op.create_table(
        "pending_reports",
        _id(),
        sa.Column("session_id", UUID(as_uuid=True), sa.ForeignKey("sessions.id")),
        sa.Column("idea_id", UUID(as_uuid=True), sa.ForeignKey("ideas.id")),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        _created_at(),
        sa.Column("offered_at", TSTZ),
        sa.Column("delivered_at", TSTZ),
    )
    op.create_index("ix_pending_reports_session_id", "pending_reports", ["session_id"])
    op.create_index("ix_pending_reports_idea_id", "pending_reports", ["idea_id"])
    op.create_index("ix_pending_reports_task_id", "pending_reports", ["task_id"])


def downgrade() -> None:
    # Reverse dependency order; dropping a table drops its indexes and constraints.
    for table in (
        "pending_reports",
        "router_decisions",
        "idea_edges",
        "artifacts",
        "tasks",
        "commitments",
        "turns",
        "sessions",
        "ideas",
    ):
        op.drop_table(table)
