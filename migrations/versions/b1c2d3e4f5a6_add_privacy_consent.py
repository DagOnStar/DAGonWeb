"""add privacy notice consent to users

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-11-18
"""
from alembic import op
import sqlalchemy as sa

revision = "b1c2d3e4f5a6"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("privacy_version", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("privacy_accepted_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("privacy_accepted_at")
        batch_op.drop_column("privacy_version")
