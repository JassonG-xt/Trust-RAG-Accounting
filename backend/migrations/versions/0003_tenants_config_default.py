"""Give tenants.config_json a server default.

``0002`` created the column ``NOT NULL`` with no server default while the model
carries a Python-side ``default=dict``. Any raw ``INSERT`` that omits
``config_json`` — a plausible bootstrap path when the first tenant has to be
seeded out of band — therefore fails. ``status`` already had a server default;
this makes ``config_json`` symmetric.

Revision ID: 0003_tenants_config_default
Revises: 0002_tenant_registry
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_tenants_config_default"
down_revision = "0002_tenant_registry"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # batch_alter_table so the migration also applies on SQLite, which has no
    # ALTER COLUMN; on Postgres it degrades to a plain ALTER.
    with op.batch_alter_table("tenants") as batch_op:
        batch_op.alter_column(
            "config_json",
            existing_type=sa.JSON(),
            existing_nullable=False,
            server_default=sa.text("'{}'"),
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch_op:
        batch_op.alter_column(
            "config_json",
            existing_type=sa.JSON(),
            existing_nullable=False,
            server_default=None,
        )
