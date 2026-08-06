"""Create the wiki proposal review queue tables (defect A).

The wiki proposal store migrates from a single JSON file to Postgres so the
REST review queue and the ``trustrag-wiki`` CLI share one durable source.
``wiki_proposals`` holds the current review state; ``wiki_proposal_actions``
is the append-only audit log. ``act()`` locks the proposal row
(``FOR UPDATE``), validates the state machine, writes the action and updates
the status in a single transaction so concurrent reviewers cannot silently
drop a decision.

Revision ID: 0005_wiki_proposal_queue
Revises: 0004_auth_sessions
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_wiki_proposal_queue"
down_revision = "0004_auth_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "wiki_proposals",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("proposal_id", sa.String(255), primary_key=True),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("risk", sa.String(32), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("proposal", sa.JSON(), nullable=False),
    )
    op.create_table(
        "wiki_proposal_actions",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("action_id", sa.String(255), primary_key=True),
        sa.Column("proposal_id", sa.String(255), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("previous_status", sa.String(64), nullable=False),
        sa.Column("new_status", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "proposal_id"],
            ["wiki_proposals.tenant_id", "wiki_proposals.proposal_id"],
        ),
    )
    op.create_index(
        "ix_wiki_proposal_actions_tenant_proposal_created",
        "wiki_proposal_actions",
        ["tenant_id", "proposal_id", "created_at", "action_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_wiki_proposal_actions_tenant_proposal_created",
        table_name="wiki_proposal_actions",
    )
    op.drop_table("wiki_proposal_actions")
    op.drop_table("wiki_proposals")
