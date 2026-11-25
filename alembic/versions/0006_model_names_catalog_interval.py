"""consoles/devices.model_name and app_settings.release_catalog_interval_hours

`model_name` is display only ("UCG-Ultra" next to the "UDRULT" code) and
nullable: existing rows show the code until the next inventory sync fills
it. The catalogue interval gets a server default of 24 hours, so the
existing settings row picks it up.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.add_column("consoles", sa.Column("model_name", sa.String(length=100), nullable=True))
    op.add_column("devices", sa.Column("model_name", sa.String(length=100), nullable=True))
    op.add_column(
        "app_settings",
        sa.Column(
            "release_catalog_interval_hours", sa.Integer(), server_default="24", nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "release_catalog_interval_hours")
    op.drop_column("devices", "model_name")
    op.drop_column("consoles", "model_name")
