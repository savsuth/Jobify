"""add hard_requirements_met/hard_requirements_missing to job_analysis

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job_analysis",
        sa.Column("hard_requirements_met", sa.JSON, nullable=False, server_default="[]"),
    )
    op.add_column(
        "job_analysis",
        sa.Column("hard_requirements_missing", sa.JSON, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("job_analysis", "hard_requirements_missing")
    op.drop_column("job_analysis", "hard_requirements_met")
