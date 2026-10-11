from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "795628b9a360"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("users")}
    for column in (
        "hashed_password",
        "llm_provider",
        "llm_base_url",
        "llm_model",
        "encrypted_api_key",
        "role",
        "is_active",
    ):
        if column not in columns:
            op.add_column("users", sa.Column(column, sa.String(), nullable=True))


def downgrade() -> None:
    for column in (
        "hashed_password",
        "llm_provider",
        "llm_base_url",
        "llm_model",
        "encrypted_api_key",
        "role",
        "is_active",
    ):
        op.drop_column("users", column)
