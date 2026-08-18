"""add work_auth_status/work_auth_reason to jobs; make job_analysis.match_score nullable

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs", sa.Column("work_auth_status", sa.String, nullable=False, server_default="unknown")
    )
    op.add_column("jobs", sa.Column("work_auth_reason", sa.Text, nullable=True))
    op.alter_column("job_analysis", "match_score", existing_type=sa.Integer, nullable=True)


def downgrade() -> None:
    op.alter_column("job_analysis", "match_score", existing_type=sa.Integer, nullable=False)
    op.drop_column("jobs", "work_auth_reason")
    op.drop_column("jobs", "work_auth_status")
