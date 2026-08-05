"""Create BFF auth session tables.

Stage 2 — the backend holds the OIDC tokens and the browser only receives an
opaque HttpOnly session cookie. ``auth_login_states`` binds the OAuth ``state``
(plus the PKCE verifier) to a browser; ``auth_sessions`` stores the live
session. Sessions are not tenant-scoped rows: the tenant is whatever the access
token says, re-derived through the authenticator on every request.

Revision ID: 0004_auth_sessions
Revises: 0003_tenants_config_default
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_auth_sessions"
down_revision = "0003_tenants_config_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_login_states",
        sa.Column("state", sa.String(128), primary_key=True),
        sa.Column("code_verifier", sa.String(255), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.String(64), nullable=False),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("session_id", sa.String(128), primary_key=True),
        sa.Column("subject_id", sa.String(255), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("access_token", sa.Text(), nullable=False),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("access_expires_at", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.String(64), nullable=False),
        sa.Column("created_at", sa.String(64), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("auth_sessions")
    op.drop_table("auth_login_states")
