"""Initial AI Quiz schema.

Revision ID: 0001_initial
Revises: None
"""
from __future__ import annotations

from alembic import op

from app.models.base import Base
import app.models  # noqa: F401


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind(), checkfirst=True)


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=True)
