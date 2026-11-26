"""app_settings.ai_max_concurrent_assessments

How many release assessments may call the AI at once (`app.ai.slots`).
Server default 2, so an upgrading instance gets the limit without doing
anything.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "ai_max_concurrent_assessments", sa.Integer(), server_default="2", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "ai_max_concurrent_assessments")
