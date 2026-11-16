"""add role permissions, SSO identity fields and the audit log

Revision ID: a9b8c7d6e5f4
Revises: f3b4c5d6e7a8
Create Date: 2026-11-16
"""
import json

from alembic import op
import sqlalchemy as sa


revision = "a9b8c7d6e5f4"
down_revision = "f3b4c5d6e7a8"
branch_labels = None
depends_on = None

USER_PERMISSIONS = ["assistant.use", "workflows.create", "workflows.publish", "workflows.run"]


def upgrade():
    with op.batch_alter_table("roles") as batch_op:
        batch_op.add_column(sa.Column("permissions_json", sa.Text(), nullable=False, server_default="[]"))
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("auth_provider", sa.String(length=40), nullable=False, server_default="local"))
        batch_op.add_column(sa.Column("external_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_users_external_id", ["external_id"])
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("ip_address", sa.String(length=64), nullable=False, server_default=""),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", ["created_at"])
    op.create_index("ix_audit_logs_user_id", "audit_logs", ["user_id"])
    op.create_index("ix_audit_logs_action", "audit_logs", ["action"])
    # Existing standard users keep everything they could do before.
    roles = sa.table("roles", sa.column("name", sa.String), sa.column("permissions_json", sa.Text))
    op.execute(roles.update().where(roles.c.name == "user").values(permissions_json=json.dumps(USER_PERMISSIONS)))


def downgrade():
    op.drop_index("ix_audit_logs_action", table_name="audit_logs")
    op.drop_index("ix_audit_logs_user_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_external_id")
        batch_op.drop_column("last_login_at")
        batch_op.drop_column("external_id")
        batch_op.drop_column("auth_provider")
    with op.batch_alter_table("roles") as batch_op:
        batch_op.drop_column("permissions_json")
