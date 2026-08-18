"""resume agent routing: lower ats_threshold default to 30, add
resume_no_tailor_threshold, add resume_drafts + resume_selections tables

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Schema-level default only, same as 0004's ats_threshold change - the actual
    # singleton search_config row is updated by re-running scripts/seed_config.py,
    # not by the migration itself. No existing job/job_analysis rows are touched.
    op.alter_column("search_config", "ats_threshold", server_default="30")
    op.add_column(
        "search_config",
        sa.Column("resume_no_tailor_threshold", sa.Integer, nullable=False, server_default="65"),
    )

    op.create_table(
        "resume_drafts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("tailored_latex", sa.Text, nullable=False),
        sa.Column("summary_of_changes", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "version", name="uq_resume_drafts_job_version"),
    )

    op.create_table(
        "resume_selections",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.Integer, sa.ForeignKey("jobs.id"), nullable=False, unique=True),
        sa.Column("resume_source", sa.String, nullable=False),
        sa.Column("tailoring_needed", sa.Boolean, nullable=False),
        sa.Column("tailoring_zone", sa.String, nullable=False),
        sa.Column("reasoning", sa.Text, nullable=False),
        sa.Column("selected_version_id", sa.Integer, sa.ForeignKey("resume_drafts.id"), nullable=True),
        sa.Column("github_context_used", sa.JSON, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("resume_selections")
    op.drop_table("resume_drafts")
    op.drop_column("search_config", "resume_no_tailor_threshold")
    op.alter_column("search_config", "ats_threshold", server_default="40")
