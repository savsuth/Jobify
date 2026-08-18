"""lower search_config.ats_threshold default from 60 to 40

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-13

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("search_config", "ats_threshold", server_default="40")


def downgrade() -> None:
    op.alter_column("search_config", "ats_threshold", server_default="60")
