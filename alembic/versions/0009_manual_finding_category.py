"""Add the manual finding category.

Revision ID: 0009_manual_finding_category
Revises: 0008_provider_token_uses
Create Date: 2026-11-07
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_manual_finding_category"
down_revision: str | None = "0008_provider_token_uses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE finding_category ADD VALUE IF NOT EXISTS 'manual'")


def downgrade() -> None:
    # PostgreSQL does not support removing an enum value safely. Keeping the
    # unused value is backward-compatible with code that no longer writes it.
    pass
