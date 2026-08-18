"""add resume_selections.github_context_snapshot (raw fetched GitHub context,
for auditability independent of the model's self-reported github_facts_used)

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "resume_selections",
        sa.Column("github_context_snapshot", sa.JSON, nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("resume_selections", "github_context_snapshot")
