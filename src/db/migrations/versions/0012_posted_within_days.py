"""add search_config.posted_within_days (Discovery freshness gate)

2026-08-17 Discovery enhancement: jobs' actual posting dates (posted_at,
already stored since migration 0003 from Greenhouse's first_published /
Lever's createdAt) were never consumed as a filter - Discovery repeatedly
re-surfaces genuinely old postings. This adds the single configurable
setting the new freshness gate reads (see
src.discovery.normalize.is_fresh_enough/classify_freshness and
src.graph.pipeline.discover_jobs). Default 7 (days); NULL disables the gate
entirely, restoring prior behavior - nothing is rejected on posting date in
that state. Backfills the existing single search_config row to 7 so the
gate is active by default without a separate seed step.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-17

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "search_config", sa.Column("posted_within_days", sa.Integer(), nullable=True, server_default="7")
    )


def downgrade() -> None:
    op.drop_column("search_config", "posted_within_days")
