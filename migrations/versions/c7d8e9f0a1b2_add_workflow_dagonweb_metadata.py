"""add workflow dagonweb metadata

Revision ID: c7d8e9f0a1b2
Revises: 8d4f1a2b6c3e
Create Date: 2026-06-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "c7d8e9f0a1b2"
down_revision = "8d4f1a2b6c3e"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dagonweb_json", sa.Text(), nullable=True, server_default="{}"))


def downgrade():
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.drop_column("dagonweb_json")
