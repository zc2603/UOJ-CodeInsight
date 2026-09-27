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
    inspector = sa.inspect(op.get_bind())
    for table, columns in {
        "quizzes": [sa.Column("minutes_per_question", sa.Integer(), nullable=False, server_default="3"),
                    sa.Column("question_mode", sa.String(length=30), nullable=False, server_default="all_positive_2")],
        "questions": [sa.Column("submission_snapshot_id", sa.Uuid(), nullable=False),
                      sa.Column("question_text_en", sa.Text(), nullable=False)],
    }.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    inspector = sa.inspect(op.get_bind())
    if "ix_questions_submission_snapshot_id" not in {index["name"] for index in inspector.get_indexes("questions")}:
        op.create_index("ix_questions_submission_snapshot_id", "questions", ["submission_snapshot_id"])
    if not any(fk["constrained_columns"] == ["submission_snapshot_id"]
        for fk in inspector.get_foreign_keys("questions")):
        op.create_foreign_key("fk_questions_submission_snapshot_id", "questions", "submission_snapshots",
            ["submission_snapshot_id"], ["id"])


def downgrade() -> None:
    op.drop_constraint("fk_questions_submission_snapshot_id", "questions", type_="foreignkey")
    op.drop_index("ix_questions_submission_snapshot_id", table_name="questions")
    op.drop_column("questions", "question_text_en")
    op.drop_column("questions", "submission_snapshot_id")
    op.drop_column("quizzes", "question_mode")
    op.drop_column("quizzes", "minutes_per_question")
