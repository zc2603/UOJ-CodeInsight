"""Versioned assessment, durable drafts, publication and appeal records."""
from alembic import op
import sqlalchemy as sa

revision = "0007_lightweight_v1"
down_revision = "0006_question_quality"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("answer_drafts") and "assessment_version" in {
        column["name"] for column in inspector.get_columns("quizzes")}:
        # 0001 creates current metadata on a fresh installation.
        op.execute("UPDATE generation_jobs SET quality_state='paused', quality_token=NULL, quality_lease_until=NULL WHERE quality_state IN ('queued', 'running')")
        return
    additions = {
        "quizzes": [
            sa.Column("assessment_version", sa.String(30), nullable=False, server_default="legacy"),
            sa.Column("time_mode", sa.String(20), nullable=False, server_default="per_question"),
            sa.Column("published_at", sa.DateTime(timezone=True)),
            sa.Column("published_by", sa.String(80)),
            sa.Column("publish_answers", sa.Boolean(), nullable=False, server_default=sa.false()),
        ],
        "quiz_problem_snapshots": [
            sa.Column("display_order", sa.Integer()),
            sa.Column("include_choice", sa.Boolean(), nullable=False, server_default=sa.true()),
        ],
        "attempts": [
            sa.Column("assessment_version", sa.String(30), nullable=False, server_default="legacy"),
            sa.Column("submitted_at", sa.DateTime(timezone=True)),
            sa.Column("submission_source", sa.String(20)),
            sa.Column("submit_key", sa.String(64)),
            sa.Column("score_version", sa.Integer(), nullable=False, server_default="0"),
        ],
        "questions": [
            sa.Column("response_format", sa.String(20), nullable=False, server_default="short_answer"),
            sa.Column("choices_json", sa.JSON()),
            sa.Column("correct_choice_id", sa.String(1)),
            sa.Column("core_idea", sa.Text()),
        ],
        "answers": [
            sa.Column("choice_id", sa.String(1)),
            sa.Column("student_dispute", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("dispute_reason", sa.Text()),
            sa.Column("manual_score", sa.Integer()),
            sa.Column("manual_reason", sa.Text()),
            sa.Column("manual_by", sa.String(80)),
            sa.Column("manual_at", sa.DateTime(timezone=True)),
            sa.Column("score_version", sa.Integer(), nullable=False, server_default="0"),
        ],
        "generation_jobs": [sa.Column("second_kind", sa.String(20))],
    }
    for table, columns in additions.items():
        for column in columns:
            op.add_column(table, column)
    op.create_table(
        "answer_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("answer_text", sa.Text(), nullable=False),
        sa.Column("choice_id", sa.String(1)),
        sa.Column("revisit", sa.Boolean(), nullable=False),
        sa.Column("dispute", sa.Boolean(), nullable=False),
        sa.Column("dispute_reason", sa.Text()),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "question_id"),
    )
    op.create_index("ix_answer_drafts_attempt_id", "answer_drafts", ["attempt_id"])
    op.create_index("ix_answer_drafts_question_id", "answer_drafts", ["question_id"])
    op.create_table(
        "review_issues",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("answer_id", sa.Uuid(), sa.ForeignKey("answers.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String(80)),
        sa.Column("resolution", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("answer_id", "source"),
    )
    op.create_index("ix_review_issues_answer_id", "review_issues", ["answer_id"])
    op.create_table(
        "score_audits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("old_score", sa.Integer()),
        sa.Column("new_score", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(80), nullable=False),
        sa.Column("score_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_score_audits_attempt_id", "score_audits", ["attempt_id"])
    op.create_index("ix_score_audits_question_id", "score_audits", ["question_id"])
    op.create_table(
        "appeals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_id", sa.Uuid(), sa.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("question_score_version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("score_snapshot", sa.Integer(), nullable=False),
        sa.Column("feedback_snapshot", sa.Text(), nullable=False),
        sa.Column("answer_snapshot", sa.Text(), nullable=False),
        sa.Column("question_snapshot", sa.Text(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("resolution", sa.Text()),
        sa.Column("resolved_by", sa.String(80)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("question_id", "question_score_version"),
    )
    op.create_index("ix_appeals_attempt_id", "appeals", ["attempt_id"])
    op.create_index("ix_appeals_question_id", "appeals", ["question_id"])
    # Invalidate in-flight review tokens. Historical conclusions remain intact.
    op.execute("UPDATE generation_jobs SET quality_state='paused', quality_token=NULL, quality_lease_until=NULL WHERE quality_state IN ('queued', 'running')")


def downgrade():
    raise RuntimeError("Preserve submitted answers, review and appeal history; use a verified backup")
