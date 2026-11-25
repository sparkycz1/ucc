"""releases.base_risk_level, release_ratings (history), job_runs (status)

All additive: `base_risk_level` is nullable (a release rated before 0.3.0
is rated the same for every console until it is re-assessed), and the two
new tables start empty — the rating history begins with the next
assessment, the Status page with each job's next run.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("base_risk_level", sa.Integer(), nullable=True))
    op.create_table(
        "release_ratings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("release_id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("risk_level", sa.Integer(), nullable=True),
        sa.Column("effective_risk_level", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(length=200), nullable=True),
        sa.Column("actor", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_release_ratings_release_id", "release_ratings", ["release_id"])
    op.create_index("ix_release_ratings_created_at", "release_ratings", ["created_at"])
    op.create_table(
        "job_runs",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("last_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_ok", sa.Boolean(), nullable=True),
        sa.Column("last_result", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("job_runs")
    op.drop_index("ix_release_ratings_created_at", table_name="release_ratings")
    op.drop_index("ix_release_ratings_release_id", table_name="release_ratings")
    op.drop_table("release_ratings")
    op.drop_column("releases", "base_risk_level")
