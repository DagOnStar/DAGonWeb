"""add HPC cluster (Unix) accounts to users and runs

Revision ID: d2e3f4a5b6c7
Revises: b1c2d3e4f5a6
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

revision = "d2e3f4a5b6c7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("unix_username", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("unix_public_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("unix_private_key", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("unix_linked_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_users_unix_username", ["unix_username"], unique=False)
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.add_column(sa.Column("unix_username", sa.String(length=64), nullable=True))


def downgrade():
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.drop_column("unix_username")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_unix_username")
        batch_op.drop_column("unix_linked_at")
        batch_op.drop_column("unix_private_key")
        batch_op.drop_column("unix_public_key")
        batch_op.drop_column("unix_username")
