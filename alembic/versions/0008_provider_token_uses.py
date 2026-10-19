"""Persist one-time provider capability claims across MCP process restarts.

Revision ID: 0008_provider_token_uses
Revises: 0007_backend_attribution
Create Date: 2026-10-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008_provider_token_uses"
down_revision: str | None = "0007_backend_attribution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_token_uses",
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("heal_job_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("nonce", name="pk_provider_token_uses"),
    )
    op.create_index("ix_provider_token_uses_expires_at", "provider_token_uses", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_provider_token_uses_expires_at", table_name="provider_token_uses")
    op.drop_table("provider_token_uses")
