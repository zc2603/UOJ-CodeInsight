"""Persist drafts and lease timeout grading without changing submitted answers."""
from alembic import op
import sqlalchemy as sa
revision = "0005_timeout_submission"
down_revision = "0004_grading_review"
branch_labels = None
depends_on = None


def upgrade():
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("attempts")}
    for column in [sa.Column("timed_out", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("draft_text", sa.Text()), sa.Column("draft_question_index", sa.Integer()),
        sa.Column("draft_revision", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("grading_token", sa.String(36)), sa.Column("grading_lease_until", sa.DateTime(timezone=True))]:
        if column.name not in existing:
            op.add_column("attempts", column)


def downgrade():
    raise RuntimeError("Preserve saved drafts; restore a verified backup for rollback")
