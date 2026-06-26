"""add registration tokens and workflow dagon ini

Revision ID: 8d4f1a2b6c3e
Revises: e1a2c3d4f5a6
Create Date: 2026-06-26 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "8d4f1a2b6c3e"
down_revision = "e1a2c3d4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.add_column(sa.Column("dagon_ini_json", sa.Text(), nullable=True))

    op.create_table(
        "registration_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    with op.batch_alter_table("registration_tokens", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_registration_tokens_email"), ["email"], unique=False)


def downgrade():
    with op.batch_alter_table("registration_tokens", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_registration_tokens_email"))
    op.drop_table("registration_tokens")
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.drop_column("dagon_ini_json")
