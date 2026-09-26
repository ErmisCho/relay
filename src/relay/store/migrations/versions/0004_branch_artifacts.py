"""Artifact kind ``branch``: a code task's committed branch in the idea's project folder.

A code task (TASK-33) ends in a draft pull request; without a GitHub token, or when publishing
fails, the result is the local task branch instead, recorded as ``artifacts.kind = 'branch'``.
Only the CHECK constraint changes (no table rewrite). Downgrade fails while ``branch`` rows
exist, rather than deleting them.

Revision ID: 0004_branch_artifacts
Revises: 0003_session_links
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from relay.store.models import in_list

revision: str = "0004_branch_artifacts"
down_revision: str | None = "0003_session_links"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Literal lists: a migration must not change when models.ARTIFACT_KINDS does.
BEFORE = ("pull_request", "document")
AFTER = ("pull_request", "document", "branch")


def _replace_check(kinds: tuple[str, ...]) -> None:
    op.drop_constraint(op.f("ck_artifacts_kind"), "artifacts", "check")
    op.create_check_constraint(op.f("ck_artifacts_kind"), "artifacts", in_list("kind", kinds))


def upgrade() -> None:
    _replace_check(AFTER)


def downgrade() -> None:
    _replace_check(BEFORE)
