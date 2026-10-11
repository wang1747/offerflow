"""add corpus_items table

Revision ID: c1d2e3f4a5b6
Revises: a1b2c3d4e5f6
Create Date: 2026-09-19

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """新增共享语料库表 corpus_items + users 表加 allow_corpus 开关列。"""
    # users 表加 allow_corpus（布尔，默认 0）
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_cols = {c["name"] for c in inspector.get_columns("users")}
    if "allow_corpus" not in user_cols:
        op.add_column("users", sa.Column("allow_corpus", sa.Boolean(), nullable=False, server_default=sa.text("0")))

    op.create_table(
        "corpus_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("item_type", sa.String(), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("structured", sa.JSON(), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=True),
        sa.Column("direction", sa.String(), nullable=True),
        sa.Column("industry", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("user_id", sa.String(), nullable=True),
        sa.Column("is_public", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("embedding_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_corpus_items_type", "corpus_items", ["item_type"])
    op.create_index("ix_corpus_items_direction", "corpus_items", ["direction"])


def downgrade() -> None:
    op.drop_index("ix_corpus_items_direction", table_name="corpus_items")
    op.drop_index("ix_corpus_items_type", table_name="corpus_items")
    op.drop_table("corpus_items")
