"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-08-12

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "candidate_profile",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("raw_latex", sa.Text, nullable=False),
        sa.Column("raw_latex_hash", sa.String, nullable=False),
        sa.Column("structured_json", sa.JSON, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "search_config",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("title_keywords", sa.JSON, nullable=False),
        sa.Column("locations", sa.JSON, nullable=False),
        sa.Column("remote_pref", sa.String, nullable=False),
        sa.Column("companies", sa.JSON, nullable=False),
        sa.Column("experience_level", sa.String, nullable=False),
        sa.Column("technologies", sa.JSON, nullable=False),
        sa.Column("ats_threshold", sa.Integer, nullable=False, server_default="60"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("source_id", sa.String, nullable=False),
        sa.Column("company", sa.String, nullable=False),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("location", sa.String, nullable=True),
        sa.Column("remote_type", sa.String, nullable=True),
        sa.Column("url", sa.String, nullable=False),
        sa.Column("description_raw", sa.Text, nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("source", "source_id", name="uq_jobs_source_source_id"),
    )

    op.create_table(
        "job_analysis",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("match_score", sa.Integer, nullable=False),
        sa.Column("matched_skills", sa.JSON, nullable=False),
        sa.Column("missing_skills", sa.JSON, nullable=False),
        sa.Column("reasoning", sa.Text, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("job_analysis")
    op.drop_table("jobs")
    op.drop_table("search_config")
    op.drop_table("candidate_profile")
