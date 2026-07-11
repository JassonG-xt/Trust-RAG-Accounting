"""Create production persistence schema.

Revision ID: 0001_production_persistence
Revises: None
"""

from __future__ import annotations

from alembic import op

from backend.app.persistence.schema import metadata

revision = "0001_production_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
