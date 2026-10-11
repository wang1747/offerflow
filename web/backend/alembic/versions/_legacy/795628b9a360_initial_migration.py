"""Initial migration

Revision ID: 795628b9a360
Revises: 
Create Date: 2026-08-01 11:09:32.196674

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '795628b9a360'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_exists(name: str) -> bool:
    return op.get_bind().dialect.has_table(op.get_bind(), name)


def upgrade() -> None:
    """Upgrade schema."""
    if not _table_exists("users"):
        op.create_table("users",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("interview_articles"):
        op.create_table("interview_articles",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("company", sa.String(), nullable=True),
            sa.Column("position", sa.String(), nullable=True),
            sa.Column("raw_content", sa.Text(), nullable=True),
            sa.Column("questions", sa.JSON(), nullable=True),
            sa.Column("tags", sa.JSON(), nullable=True),
            sa.Column("difficulty", sa.String(), nullable=True),
            sa.Column("embedding_id", sa.String(), nullable=True),
            sa.Column("source", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("interview_sessions"):
        op.create_table("interview_sessions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("resume_id", sa.String(), nullable=False),
            sa.Column("jd_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=True),
            sa.Column("questions", sa.JSON(), nullable=True),
            sa.Column("answers", sa.JSON(), nullable=True),
            sa.Column("feedbacks", sa.JSON(), nullable=True),
            sa.Column("current_question", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("job_descriptions"):
        op.create_table("job_descriptions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("company", sa.String(), nullable=True),
            sa.Column("position", sa.String(), nullable=True),
            sa.Column("must_have", sa.JSON(), nullable=True),
            sa.Column("nice_to_have", sa.JSON(), nullable=True),
            sa.Column("tech_stack", sa.JSON(), nullable=True),
            sa.Column("hidden_signals", sa.JSON(), nullable=True),
            sa.Column("embedding_id", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("resumes"):
        op.create_table("resumes",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("parsed_json", sa.JSON(), nullable=True),
            sa.Column("source_file", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("applications"):
        op.create_table("applications",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("company", sa.String(), nullable=False),
            sa.Column("position", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("jd_id", sa.String(), nullable=True),
            sa.Column("resume_id", sa.String(), nullable=True),
            sa.Column("applied_date", sa.Date(), nullable=True),
            sa.Column("next_action", sa.Text(), nullable=True),
            sa.Column("next_action_date", sa.Date(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["jd_id"], ["job_descriptions.id"]),
            sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("interview_questions"):
        op.create_table("interview_questions",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("article_id", sa.String(), nullable=True),
            sa.Column("user_id", sa.String(), nullable=False),
            sa.Column("question", sa.Text(), nullable=False),
            sa.Column("answer", sa.Text(), nullable=True),
            sa.Column("category", sa.String(), nullable=True),
            sa.Column("difficulty", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["article_id"], ["interview_articles.id"]),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("match_results"):
        op.create_table("match_results",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("jd_id", sa.String(), nullable=False),
            sa.Column("resume_id", sa.String(), nullable=False),
            sa.Column("score", sa.Float(), nullable=False),
            sa.Column("skill_match_detail", sa.JSON(), nullable=True),
            sa.Column("gap_analysis", sa.JSON(), nullable=True),
            sa.Column("suggestion", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["jd_id"], ["job_descriptions.id"]),
            sa.ForeignKeyConstraint(["resume_id"], ["resumes.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _table_exists("application_events"):
        op.create_table("application_events",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("application_id", sa.String(), nullable=False),
            sa.Column("event_type", sa.String(), nullable=False),
            sa.Column("from_status", sa.String(), nullable=True),
            sa.Column("to_status", sa.String(), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("event_date", sa.DateTime(), nullable=False),
            sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=True),
            sa.ForeignKeyConstraint(["application_id"], ["applications.id"]),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    """Downgrade schema."""
    for table in (
        "application_events",
        "match_results",
        "interview_questions",
        "applications",
        "resumes",
        "job_descriptions",
        "interview_sessions",
        "interview_articles",
        "users",
    ):
        if _table_exists(table):
            op.drop_table(table)
