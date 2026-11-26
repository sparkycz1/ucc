"""Per-Fabric/console/device policy, fallback AI model, AI call accounting

- Hidden release stages (`hidden_release_stages`): global list on
  `app_settings` (default `[]`, nothing hidden — the behaviour before
  0.4.0), and a nullable override on fabrics, consoles and devices
  (NULL = inherit).
- Acceptable risk level overrides on consoles and devices (NULL = inherit;
  the Fabric one exists since 0001).
- Per-Fabric inventory sync interval (NULL = the global one) and the time
  the last sync was enqueued, which Beat's every-minute tick compares it to.
- A fallback AI model next to the primary one (`release_assessment_model_id`,
  unchanged), and a request timeout per provider (default 90 s, what was
  hard-coded before).
- Per-call AI accounting: what the call was for, how long it took, whether
  it was the fallback; on releases the last assessment's tokens and time;
  on assistant messages the provider whose format `provider_native` is in.

All additive and nullable or with a server default, so an instance
upgrading keeps behaving exactly as before until someone changes a setting.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None

_PROVIDER_KIND = postgresql.ENUM(name="ai_provider_kind", create_type=False)


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column(
            "hidden_release_stages", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
    )
    op.add_column("app_settings", sa.Column("ai_fallback_model_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_app_settings_ai_fallback_model_id",
        "app_settings",
        "ai_models",
        ["ai_fallback_model_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column("fabrics", sa.Column("hidden_release_stages", sa.JSON(), nullable=True))
    op.add_column(
        "fabrics", sa.Column("inventory_sync_interval_seconds", sa.Integer(), nullable=True)
    )
    op.add_column(
        "fabrics", sa.Column("last_sync_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    for table in ("consoles", "devices"):
        op.add_column(table, sa.Column("max_acceptable_risk_level", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("hidden_release_stages", sa.JSON(), nullable=True))
    op.add_column(
        "ai_provider_configs",
        sa.Column("timeout_seconds", sa.Integer(), server_default="90", nullable=False),
    )
    op.add_column("ai_usage_records", sa.Column("purpose", sa.String(length=32), nullable=True))
    op.add_column("ai_usage_records", sa.Column("label", sa.String(length=255), nullable=True))
    op.add_column("ai_usage_records", sa.Column("duration_ms", sa.Integer(), nullable=True))
    op.add_column(
        "ai_usage_records",
        sa.Column("fallback", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("ai_messages", sa.Column("provider_kind", _PROVIDER_KIND, nullable=True))
    op.add_column("releases", sa.Column("assessment_tokens", sa.Integer(), nullable=True))
    op.add_column("releases", sa.Column("assessment_duration_ms", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("releases", "assessment_duration_ms")
    op.drop_column("releases", "assessment_tokens")
    op.drop_column("ai_messages", "provider_kind")
    for column in ("fallback", "duration_ms", "label", "purpose"):
        op.drop_column("ai_usage_records", column)
    op.drop_column("ai_provider_configs", "timeout_seconds")
    for table in ("devices", "consoles"):
        op.drop_column(table, "hidden_release_stages")
        op.drop_column(table, "max_acceptable_risk_level")
    op.drop_column("fabrics", "last_sync_attempt_at")
    op.drop_column("fabrics", "inventory_sync_interval_seconds")
    op.drop_column("fabrics", "hidden_release_stages")
    op.drop_constraint(
        "fk_app_settings_ai_fallback_model_id", "app_settings", type_="foreignkey"
    )
    op.drop_column("app_settings", "ai_fallback_model_id")
    op.drop_column("app_settings", "hidden_release_stages")
