"""Persistent scheduled quiz opening."""
from alembic import op
import sqlalchemy as sa

revision = "0011_scheduled_open"
down_revision = "0010_model_switch_tests"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 creates current metadata when initializing an empty database.
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("quizzes")}
    if "scheduled_open_at" not in columns:
        op.add_column("quizzes", sa.Column("scheduled_open_at", sa.DateTime(timezone=True), nullable=True))
    if "scheduled_open_error" not in columns:
        op.add_column("quizzes", sa.Column("scheduled_open_error", sa.Text(), nullable=True))
    if "ix_quizzes_scheduled_open_at" not in {i["name"] for i in inspector.get_indexes("quizzes")}:
        op.create_index("ix_quizzes_scheduled_open_at", "quizzes", ["scheduled_open_at"])


def downgrade():
    op.drop_index("ix_quizzes_scheduled_open_at", table_name="quizzes")
    op.drop_column("quizzes", "scheduled_open_error")
    op.drop_column("quizzes", "scheduled_open_at")
