"""add workflow run label

Revision ID: e1a2c3d4f5a6
Revises: bca19f23b1f8
Create Date: 2026-06-24
"""
from alembic import op
import sqlalchemy as sa


revision = "e1a2c3d4f5a6"
down_revision = "bca19f23b1f8"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.add_column(sa.Column("label", sa.String(length=255), nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.drop_column("label")
