"""add workflow link kind for DAGonStar explicit dependencies

Revision ID: f3b4c5d6e7a8
Revises: c7d8e9f0a1b2
Create Date: 2026-11-16
"""
from alembic import op
import sqlalchemy as sa


revision = "f3b4c5d6e7a8"
down_revision = "c7d8e9f0a1b2"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflow_links") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.String(length=20), nullable=False, server_default="data"))


def downgrade():
    with op.batch_alter_table("workflow_links") as batch_op:
        batch_op.drop_column("kind")
