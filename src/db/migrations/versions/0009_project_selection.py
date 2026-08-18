"""add resume_selections.project_selection (structured project-selection audit
trail: selected/removed pool projects, reordered skill categories)

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "resume_selections",
        sa.Column("project_selection", sa.JSON, nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("resume_selections", "project_selection")
