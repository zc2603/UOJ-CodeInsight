"""Durable per-submission question preparation, additive to existing quizzes."""
from alembic import op
import sqlalchemy as sa

revision = "0003_pre_generation"
down_revision = "0002_multi_problem_bilingual"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table("generation_jobs"):
        # A fresh database may have been created by 0001's current metadata.
        op.execute("INSERT INTO generation_control (id) VALUES (1) ON CONFLICT (id) DO NOTHING")
        return
    if "pre_generate" not in {column["name"] for column in inspector.get_columns("quizzes")}:
        op.add_column("quizzes", sa.Column("pre_generate", sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table("generation_control", sa.Column("id", sa.Integer(), primary_key=True))
    op.execute("INSERT INTO generation_control (id) VALUES (1)")
    op.create_table("generation_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("quiz_id", sa.Uuid(), sa.ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("participant_id", sa.Uuid(), sa.ForeignKey("quiz_participants.id", ondelete="CASCADE"), nullable=False),
        sa.Column("submission_snapshot_id", sa.Uuid(), sa.ForeignKey("submission_snapshots.id", ondelete="CASCADE"), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result_json", sa.JSON()),
        sa.Column("raw_response", sa.Text()),
        sa.Column("model", sa.String(100)),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("error", sa.Text()),
        sa.UniqueConstraint("submission_snapshot_id", "round_no"),
    )
    op.create_index("ix_generation_claim", "generation_jobs", ["state", "available_at"])
    op.create_index("ix_generation_jobs_quiz_id", "generation_jobs", ["quiz_id"])
    op.create_index("ix_generation_jobs_participant_id", "generation_jobs", ["participant_id"])
    op.create_table("generation_runs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("prompt_version", sa.String(50), nullable=False),
        sa.Column("raw_response", sa.Text()),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_generation_runs_job_id", "generation_runs", ["job_id"])


def downgrade():
    op.drop_table("generation_runs")
    op.drop_table("generation_jobs")
    op.drop_table("generation_control")
    op.drop_column("quizzes", "pre_generate")
