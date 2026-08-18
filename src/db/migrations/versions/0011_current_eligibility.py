"""add jobs.current_eligibility/eligibility_reasons/eligibility_evaluated_at
(re-evaluates every historically-persisted job against CURRENT Discovery
rules only - see src/discovery/normalize.compute_current_eligibility)

Root cause (historical-cleanup investigation, 2026-08-17): the 304-row jobs
table spans multiple generations of Discovery filter code - 60 senior-titled
and 76 wrong-role-family jobs predate the filters that would reject them
today, and 42 non-US jobs (41 stale + 2 confirmed via live Greenhouse
re-fetch to be a still-active classify_location() offices[]-tiering gap, see
jobs 188/232) currently sit in the table despite failing CURRENT rules.
Nothing in the existing schema distinguished "persisted" from "currently
valid" - this migration adds that distinction as a new, additive gate,
without touching Discovery/ATS/Resume Agent code or deleting any row.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("current_eligibility", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("eligibility_reasons", sa.JSON(), nullable=True))
    op.add_column("jobs", sa.Column("eligibility_evaluated_at", sa.DateTime(timezone=True), nullable=True))

    from datetime import datetime, timezone

    from src.discovery.normalize import compute_current_eligibility

    bind = op.get_bind()
    jobs_table = sa.table(
        "jobs",
        sa.column("id", sa.Integer),
        sa.column("title", sa.String),
        sa.column("location", sa.String),
        sa.column("description_raw", sa.Text),
        sa.column("current_eligibility", sa.String),
        sa.column("eligibility_reasons", sa.JSON),
        sa.column("eligibility_evaluated_at", sa.DateTime(timezone=True)),
    )
    rows = bind.execute(
        sa.select(jobs_table.c.id, jobs_table.c.title, jobs_table.c.location, jobs_table.c.description_raw)
    ).fetchall()
    now = datetime.now(timezone.utc)
    for job_id, title, location, description_raw in rows:
        status, reasons = compute_current_eligibility(title, location, description_raw)
        bind.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job_id)
            .values(current_eligibility=status, eligibility_reasons=reasons, eligibility_evaluated_at=now)
        )

    op.alter_column("jobs", "current_eligibility", nullable=False)
    op.alter_column("jobs", "eligibility_reasons", nullable=False)
    op.alter_column("jobs", "eligibility_evaluated_at", nullable=False)
    op.create_index("ix_jobs_current_eligibility", "jobs", ["current_eligibility"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_current_eligibility", table_name="jobs")
    op.drop_column("jobs", "eligibility_evaluated_at")
    op.drop_column("jobs", "eligibility_reasons")
    op.drop_column("jobs", "current_eligibility")
