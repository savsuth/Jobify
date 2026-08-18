"""add jobs.canonical_key (source-independent identity for cross-source
deduplication - see src/discovery/normalize.canonical_job_identity)

Root cause (investigation, 2026-08-17): (source, source_id) dedup is scoped
to one source - Greenhouse/Lever use each platform's own native posting ID,
while web search uses the discovered URL itself as source_id - so the same
still-open posting, found once via web search and later via the native
connector, produces two different (source, source_id) pairs and gets
persisted twice. 7 real duplicate pairs were found in the jobs table at the
time of this migration (14 rows total), each with independent, sometimes
DIVERGING downstream job_analysis/resume_selections data on both sides (e.g.
one side scored 68% and selected "master", the other scored 58% and
tailored) - so this migration deliberately does NOT merge or delete any
existing row. It only adds the new identity column, backfills it for every
existing row, and adds a non-unique index so discover_jobs() can start
checking new candidates against it going forward. Resolving the 7 existing
duplicate pairs is a separate, human-reviewed decision - see the
investigation report for the full list of affected job IDs.

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("canonical_key", sa.String(), nullable=True))

    # Backfill using the exact same function the application uses going
    # forward, imported here rather than reimplemented, so the migration can
    # never silently drift from the real normalization logic.
    from src.discovery.normalize import canonical_job_identity

    bind = op.get_bind()
    jobs_table = sa.table("jobs", sa.column("id", sa.Integer), sa.column("url", sa.String),
                           sa.column("canonical_key", sa.String))
    rows = bind.execute(sa.select(jobs_table.c.id, jobs_table.c.url)).fetchall()
    for job_id, url in rows:
        bind.execute(
            jobs_table.update()
            .where(jobs_table.c.id == job_id)
            .values(canonical_key=canonical_job_identity(url))
        )

    op.alter_column("jobs", "canonical_key", nullable=False)
    op.create_index("ix_jobs_canonical_key", "jobs", ["canonical_key"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_jobs_canonical_key", table_name="jobs")
    op.drop_column("jobs", "canonical_key")
