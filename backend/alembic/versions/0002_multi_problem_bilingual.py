"""Support multi-problem bilingual quizzes.

Revision ID: 0002_multi_problem_bilingual
Revises: 0001_initial
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002_multi_problem_bilingual"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quizzes", sa.Column("minutes_per_question", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("quizzes", sa.Column("question_mode", sa.String(length=30), nullable=False, server_default="all_positive_2"))
    op.add_column("questions", sa.Column("submission_snapshot_id", sa.Uuid(), nullable=False))
    op.add_column("questions", sa.Column("question_text_en", sa.Text(), nullable=False))
    op.create_index("ix_questions_submission_snapshot_id", "questions", ["submission_snapshot_id"])
    op.create_foreign_key(
        "fk_questions_submission_snapshot_id",
        "questions",
        "submission_snapshots",
        ["submission_snapshot_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_questions_submission_snapshot_id", "questions", type_="foreignkey")
    op.drop_index("ix_questions_submission_snapshot_id", table_name="questions")
    op.drop_column("questions", "question_text_en")
    op.drop_column("questions", "submission_snapshot_id")
    op.drop_column("quizzes", "question_mode")
    op.drop_column("quizzes", "minutes_per_question")
