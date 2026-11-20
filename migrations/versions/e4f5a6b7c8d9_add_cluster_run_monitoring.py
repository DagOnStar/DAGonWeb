"""add cluster run monitoring state

Revision ID: e4f5a6b7c8d9
Revises: d2e3f4a5b6c7
Create Date: 2026-11-19
"""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "d2e3f4a5b6c7"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.add_column(sa.Column("remote_meta_json", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("monitor_heartbeat", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("workflow_runs") as batch_op:
        batch_op.drop_column("monitor_heartbeat")
        batch_op.drop_column("remote_meta_json")
