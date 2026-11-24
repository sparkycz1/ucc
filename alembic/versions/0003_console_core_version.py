"""rename consoles.firmware_version to core_version

0.1.2 stored `hardware.firmwareVersion` there and showed `reportedState.version`
as UniFi OS. It's the other way round: `hardware.firmwareVersion` is the UniFi
OS version updates compare to (now `os_version`), `reportedState.version` is a
separate platform build (now `core_version`). The values themselves are
mirrored from Site Manager and rewritten by the next inventory sync.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: Sequence[str] | str | None = None
depends_on: Sequence[str] | str | None = None


def upgrade() -> None:
    op.alter_column("consoles", "firmware_version", new_column_name="core_version")


def downgrade() -> None:
    op.alter_column("consoles", "core_version", new_column_name="firmware_version")
