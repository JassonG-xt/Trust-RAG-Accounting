"""Create production persistence schema.

Revision ID: 0001_production_persistence
Revises: None
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_production_persistence"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("document_id", sa.String(255), primary_key=True),
        sa.Column("current_version_id", sa.String(255)),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("document_type", sa.String(128), nullable=False),
        sa.Column("client", sa.String(255)),
        sa.Column("tombstoned", sa.Boolean(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(64), nullable=False),
    )
    op.create_table(
        "document_versions",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("version_id", sa.String(255), primary_key=True),
        sa.Column("document_id", sa.String(255), nullable=False),
        sa.Column("version_label", sa.String(128), nullable=False),
        sa.Column("checksum", sa.String(128), nullable=False),
        sa.Column("source_uri", sa.Text(), nullable=False),
        sa.Column("parse_status", sa.String(32), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.document_id"],
        ),
        sa.UniqueConstraint("tenant_id", "document_id", "checksum"),
    )
    op.create_table(
        "index_generations",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("generation_id", sa.String(255), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("activated_at", sa.String(64)),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index(
        "uq_index_generations_tenant_active",
        "index_generations",
        ["tenant_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "document_chunks",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("generation_id", sa.String(255), primary_key=True),
        sa.Column("chunk_id", sa.String(255), primary_key=True),
        sa.Column("document_id", sa.String(255), nullable=False),
        sa.Column("version_id", sa.String(255), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("checksum", sa.String(128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "document_id"],
            ["documents.tenant_id", "documents.document_id"],
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["document_versions.tenant_id", "document_versions.version_id"],
        ),
    )
    op.create_table(
        "review_checkpoints",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("review_queue_id", sa.String(255), primary_key=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
    )
    op.create_table(
        "review_actions",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("action_id", sa.String(255), primary_key=True),
        sa.Column("review_queue_id", sa.String(255), nullable=False),
        sa.Column("previous_status", sa.String(64), nullable=False),
        sa.Column("new_status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "review_queue_id"],
            ["review_checkpoints.tenant_id", "review_checkpoints.review_queue_id"],
        ),
    )
    op.create_index(
        "ix_review_actions_tenant_queue_created",
        "review_actions",
        ["tenant_id", "review_queue_id", "created_at", "action_id"],
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("run_id", sa.String(255), primary_key=True),
        sa.Column("run_type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("artifact_uri", sa.Text()),
        sa.Column("summary", sa.JSON(), nullable=False),
    )
    op.create_table(
        "index_jobs",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("job_id", sa.String(255), primary_key=True),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source_uri", sa.Text()),
        sa.Column("document_id", sa.String(255)),
        sa.Column("generation_id", sa.String(255)),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.String(64)),
        sa.Column("lease_owner", sa.String(255)),
        sa.Column("lease_expires_at", sa.String(64)),
        sa.Column("error_code", sa.String(128)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(64), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key"),
    )


def downgrade() -> None:
    op.drop_table("index_jobs")
    op.drop_table("evaluation_runs")
    op.drop_index(
        "ix_review_actions_tenant_queue_created",
        table_name="review_actions",
    )
    op.drop_table("review_actions")
    op.drop_table("review_checkpoints")
    op.drop_table("document_chunks")
    op.drop_index(
        "uq_index_generations_tenant_active",
        table_name="index_generations",
    )
    op.drop_table("index_generations")
    op.drop_table("document_versions")
    op.drop_table("documents")
