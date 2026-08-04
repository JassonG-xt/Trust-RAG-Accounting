"""Create tenants registry table.

Revision ID: 0002_tenant_registry
Revises: 0001_production_persistence
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_tenant_registry"
down_revision = "0001_production_persistence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("tenants")
