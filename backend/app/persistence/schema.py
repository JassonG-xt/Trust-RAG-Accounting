"""Relational schema for production persistence."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

metadata = MetaData()

documents = Table(
    "documents",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("document_id", String(255), primary_key=True),
    Column("current_version_id", String(255)),
    Column("staging_generation_id", String(255)),
    Column("title", Text, nullable=False),
    Column("document_type", String(128), nullable=False),
    Column("client", String(255)),
    Column("tombstoned", Boolean, nullable=False, default=False),
    Column("metadata_json", JSON, nullable=False, default=dict),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
)

document_versions = Table(
    "document_versions",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("version_id", String(255), primary_key=True),
    Column("document_id", String(255), nullable=False),
    Column("version_label", String(128), nullable=False),
    Column("checksum", String(128), nullable=False),
    Column("source_uri", Text, nullable=False),
    Column("parse_status", String(32), nullable=False),
    Column("staging_generation_id", String(255)),
    Column("metadata_json", JSON, nullable=False, default=dict),
    Column("created_at", String(64), nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "document_id"],
        ["documents.tenant_id", "documents.document_id"],
    ),
    UniqueConstraint("tenant_id", "document_id", "checksum"),
)

document_chunks = Table(
    "document_chunks",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("generation_id", String(255), primary_key=True),
    Column("chunk_id", String(255), primary_key=True),
    Column("document_id", String(255), nullable=False),
    Column("version_id", String(255), nullable=False),
    Column("position", Integer, nullable=False),
    Column("checksum", String(128), nullable=False),
    Column("content", Text, nullable=False),
    Column("metadata_json", JSON, nullable=False, default=dict),
    ForeignKeyConstraint(
        ["tenant_id", "document_id"],
        ["documents.tenant_id", "documents.document_id"],
    ),
    ForeignKeyConstraint(
        ["tenant_id", "version_id"],
        ["document_versions.tenant_id", "document_versions.version_id"],
    ),
)

review_checkpoints = Table(
    "review_checkpoints",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("review_queue_id", String(255), primary_key=True),
    Column("status", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
)

review_actions = Table(
    "review_actions",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("action_id", String(255), primary_key=True),
    Column("review_queue_id", String(255), nullable=False),
    Column("previous_status", String(64), nullable=False),
    Column("new_status", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    ForeignKeyConstraint(
        ["tenant_id", "review_queue_id"],
        ["review_checkpoints.tenant_id", "review_checkpoints.review_queue_id"],
    ),
)
Index(
    "ix_review_actions_tenant_queue_created",
    review_actions.c.tenant_id,
    review_actions.c.review_queue_id,
    review_actions.c.created_at,
    review_actions.c.action_id,
)

evaluation_runs = Table(
    "evaluation_runs",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("run_id", String(255), primary_key=True),
    Column("run_type", String(64), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("artifact_uri", Text),
    Column("summary", JSON, nullable=False),
)

index_generations = Table(
    "index_generations",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("generation_id", String(255), primary_key=True),
    Column("status", String(32), nullable=False),
    Column("created_at", String(64), nullable=False),
    Column("activated_at", String(64)),
    Column("metadata_json", JSON, nullable=False, default=dict),
)
Index(
    "uq_index_generations_tenant_active",
    index_generations.c.tenant_id,
    unique=True,
    postgresql_where=index_generations.c.status == "active",
    sqlite_where=index_generations.c.status == "active",
)

index_jobs = Table(
    "index_jobs",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("job_id", String(255), primary_key=True),
    Column("operation", String(32), nullable=False),
    Column("status", String(32), nullable=False),
    Column("source_uri", Text),
    Column("document_id", String(255)),
    Column("generation_id", String(255)),
    Column("idempotency_key", String(255), nullable=False),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("next_attempt_at", String(64)),
    Column("lease_owner", String(255)),
    Column("lease_expires_at", String(64)),
    Column("error_code", String(128)),
    Column("error_summary", Text),
    Column("payload", JSON, nullable=False, default=dict),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    UniqueConstraint("tenant_id", "idempotency_key"),
)

tenants = Table(
    "tenants",
    metadata,
    Column("tenant_id", String(128), primary_key=True),
    Column("name", Text, nullable=False),
    Column("status", String(32), nullable=False, default="active"),
    Column("created_at", String(64), nullable=False),
    Column("updated_at", String(64), nullable=False),
    Column("config_json", JSON, nullable=False, default=dict),
)
