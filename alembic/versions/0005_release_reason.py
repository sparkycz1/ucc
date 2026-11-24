"""releases.reason — the AI's few-word reason for its rating

Nullable: existing ratings keep working and fall back to their issue
titles (`Release.short_reason`) until they are re-assessed.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("reason", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("releases", "reason")
