"""add employment_type, location_category, seniority, posted_at to jobs

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("employment_type", sa.String, nullable=False, server_default="unknown")
    )
    op.add_column(
        "jobs",
        sa.Column("location_category", sa.String, nullable=False, server_default="location_unknown"),
    )
    op.add_column("jobs", sa.Column("seniority", sa.String, nullable=False, server_default="unknown"))
    op.add_column("jobs", sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "posted_at")
    op.drop_column("jobs", "seniority")
    op.drop_column("jobs", "location_category")
    op.drop_column("jobs", "employment_type")
