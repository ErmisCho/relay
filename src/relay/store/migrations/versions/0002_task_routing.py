"""Task-level router decisions and the served model per task (TASK-46).

``router_decisions`` can now record the task-difficulty router's decision for a dispatched
task: ``task_id`` (FK -> tasks), ``turn_id`` becomes nullable with a CHECK that at least one of
the two is set, plus ``model_chosen`` and ``router_status``. At most one active decision per
task (partial unique index), so a replayed routing step cannot log a second one.
``tasks.served_model`` records the model that actually produced the task's result.

Revision ID: 0002_task_routing
Revises: 0001_initial
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

from relay.store.models import ROUTER_STATUSES, in_list

revision: str = "0002_task_routing"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("router_decisions", sa.Column("task_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        op.f("fk_router_decisions_task_id_tasks"), "router_decisions", "tasks", ["task_id"], ["id"]
    )
    op.add_column("router_decisions", sa.Column("model_chosen", sa.Text()))
    op.add_column("router_decisions", sa.Column("router_status", sa.Text()))
    op.alter_column("router_decisions", "turn_id", existing_type=UUID(as_uuid=True), nullable=True)
    op.create_check_constraint(
        op.f("ck_router_decisions_turn_or_task"),
        "router_decisions",
        "turn_id IS NOT NULL OR task_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_router_decisions_router_status"),
        "router_decisions",
        f"router_status IS NULL OR {in_list('router_status', ROUTER_STATUSES)}",
    )
    op.create_index("ix_router_decisions_task_id", "router_decisions", ["task_id"])
    op.create_index(
        "uq_router_decisions_task_active",
        "router_decisions",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.add_column("tasks", sa.Column("served_model", sa.Text()))


def downgrade() -> None:
    op.drop_column("tasks", "served_model")
    # Task-level decisions have no turn and cannot survive `turn_id NOT NULL`; they are audit
    # rows only (the served model is gone with tasks.served_model anyway).
    op.execute("DELETE FROM router_decisions WHERE turn_id IS NULL")
    op.drop_index("uq_router_decisions_task_active", table_name="router_decisions")
    op.drop_index("ix_router_decisions_task_id", table_name="router_decisions")
    op.drop_constraint(op.f("ck_router_decisions_router_status"), "router_decisions", "check")
    op.drop_constraint(op.f("ck_router_decisions_turn_or_task"), "router_decisions", "check")
    op.alter_column("router_decisions", "turn_id", existing_type=UUID(as_uuid=True), nullable=False)
    op.drop_column("router_decisions", "router_status")
    op.drop_column("router_decisions", "model_chosen")
    # Dropping the column drops its FK constraint too.
    op.drop_column("router_decisions", "task_id")
