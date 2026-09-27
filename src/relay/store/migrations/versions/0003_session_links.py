"""Session end reason, commitment -> session / assent turn, turn-scoped active index (TASK-31/46).

- ``sessions.end_reason``: why the session ended (CHECK against ``SESSION_END_REASONS``).
- ``commitments.session_id`` (FK sessions) and ``commitments.assent_turn_id`` (FK turns), both
  nullable: rows written before this revision have neither, so the Phase 1 audit falls back to
  its time window for them.
- ``uq_router_decisions_active`` now covers turn-level rows only (``task_id IS NULL``): a task
  decision may point at its assent turn, which can carry its own active turn-level decision.
  Task rows stay unique per task through ``uq_router_decisions_task_active`` (unchanged).

All additions are nullable without defaults, so no table rewrite.

Revision ID: 0003_session_links
Revises: 0002_task_routing
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from relay.store.models import SESSION_END_REASONS, in_list

revision: str = "0003_session_links"
down_revision: str | None = "0002_task_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("end_reason", sa.Text()))
    op.create_check_constraint(
        op.f("ck_sessions_end_reason"),
        "sessions",
        f"end_reason IS NULL OR {in_list('end_reason', SESSION_END_REASONS)}",
    )

    op.add_column("commitments", sa.Column("session_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        op.f("fk_commitments_session_id_sessions"),
        "commitments",
        "sessions",
        ["session_id"],
        ["id"],
    )
    op.create_index("ix_commitments_session_id", "commitments", ["session_id"])
    op.add_column("commitments", sa.Column("assent_turn_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        op.f("fk_commitments_assent_turn_id_turns"),
        "commitments",
        "turns",
        ["assent_turn_id"],
        ["id"],
    )
    op.create_index("ix_commitments_assent_turn_id", "commitments", ["assent_turn_id"])

    op.drop_index("uq_router_decisions_active", table_name="router_decisions")
    op.create_index(
        "uq_router_decisions_active",
        "router_decisions",
        ["turn_id"],
        unique=True,
        postgresql_where=sa.text("is_active AND task_id IS NULL"),
    )


def downgrade() -> None:
    # The pre-0003 index covers every active row. Task rows only gained a turn_id with this
    # revision (the assent turn), so drop that link before the wider index comes back.
    op.execute("UPDATE router_decisions SET turn_id = NULL WHERE task_id IS NOT NULL")
    op.drop_index("uq_router_decisions_active", table_name="router_decisions")
    op.create_index(
        "uq_router_decisions_active",
        "router_decisions",
        ["turn_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )

    # Dropping a column drops its FK constraint too.
    op.drop_index("ix_commitments_assent_turn_id", table_name="commitments")
    op.drop_column("commitments", "assent_turn_id")
    op.drop_index("ix_commitments_session_id", table_name="commitments")
    op.drop_column("commitments", "session_id")

    op.drop_constraint(op.f("ck_sessions_end_reason"), "sessions", "check")
    op.drop_column("sessions", "end_reason")
