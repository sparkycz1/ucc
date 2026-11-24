"""releases (AI assessment), newest community releases, acceptable risk level, assessment sources

Adds the `releases` table, the global acceptable risk level and assessment
source settings on `app_settings` (every column nullable or with a server
default, so an existing row upgrades in place), and the per-Fabric
override `fabrics.max_acceptable_risk_level` (nullable = use the global).

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.create_table(
        "releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("product", sa.String(length=100), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("community_release_id", sa.String(length=64), nullable=True),
        sa.Column("community_url", sa.String(length=500), nullable=True),
        sa.Column("stage", sa.String(length=32), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_level", sa.Integer(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("issues", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("assessed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assessed_by_model", sa.String(length=255), nullable=True),
        sa.Column("assessment_queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("assessment_error", sa.Text(), nullable=True),
        sa.Column("manual_risk_level", sa.Integer(), nullable=True),
        sa.Column("manual_note", sa.Text(), nullable=True),
        sa.Column("manual_by", sa.String(length=255), nullable=True),
        sa.Column("manual_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allowed_by", sa.String(length=255), nullable=True),
        sa.Column("allowed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("allowed_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "product", "version"),
    )
    op.create_table(
        "latest_releases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("product", sa.String(length=100), nullable=False),
        sa.Column("current_version", sa.String(length=64), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column("url", sa.String(length=500), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "product", "current_version"),
    )
    op.add_column(
        "app_settings",
        sa.Column("max_acceptable_risk_level", sa.Integer(), server_default="2", nullable=False),
    )
    op.add_column(
        "app_settings", sa.Column("release_assessment_model_id", sa.Uuid(), nullable=True)
    )
    op.create_foreign_key(
        "app_settings_release_assessment_model_id_fkey",
        "app_settings",
        "ai_models",
        ["release_assessment_model_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "app_settings", sa.Column("reddit_client_id", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "app_settings",
        sa.Column("reddit_client_secret_encrypted", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "app_settings",
        sa.Column("web_search_kind", sa.String(length=16), server_default="none", nullable=False),
    )
    op.add_column("app_settings", sa.Column("web_search_url", sa.String(length=500), nullable=True))
    op.add_column(
        "app_settings",
        sa.Column("web_search_api_key_encrypted", sa.LargeBinary(), nullable=True),
    )
    op.add_column("fabrics", sa.Column("max_acceptable_risk_level", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("fabrics", "max_acceptable_risk_level")
    op.drop_column("app_settings", "web_search_api_key_encrypted")
    op.drop_column("app_settings", "web_search_url")
    op.drop_column("app_settings", "web_search_kind")
    op.drop_column("app_settings", "reddit_client_secret_encrypted")
    op.drop_column("app_settings", "reddit_client_id")
    op.drop_constraint(
        "app_settings_release_assessment_model_id_fkey", "app_settings", type_="foreignkey"
    )
    op.drop_column("app_settings", "release_assessment_model_id")
    op.drop_column("app_settings", "max_acceptable_risk_level")
    op.drop_table("latest_releases")
    op.drop_table("releases")
