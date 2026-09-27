"""Persist router token usage and cost.

Revision ID: 0005_router_usage
Revises: 0004_branch_artifacts
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_router_usage"
down_revision: str | None = "0004_branch_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("router_decisions", sa.Column("input_tokens", sa.Integer()))
    op.add_column("router_decisions", sa.Column("output_tokens", sa.Integer()))
    op.add_column("router_decisions", sa.Column("cost_usd", sa.Float()))


def downgrade() -> None:
    op.drop_column("router_decisions", "cost_usd")
    op.drop_column("router_decisions", "output_tokens")
    op.drop_column("router_decisions", "input_tokens")
