"""Store provisional grades and teacher review flags without rewriting history."""
from alembic import op
import sqlalchemy as sa

revision = "0004_grading_review"
down_revision = "0003_pre_generation"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # 0001 uses current metadata on a fresh install; make additions idempotent.
    for table, columns in {
        "attempts": [sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.false())],
        "answers": [sa.Column("review_required", sa.Boolean(), nullable=False, server_default=sa.false()),
                    sa.Column("review_reason", sa.Text()), sa.Column("question_validity", sa.String(20))],
    }.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
    # Existing non-native enum uses VARCHAR(24), long enough for both new values.
    # Older SQLAlchemy installations may have emitted a named CHECK constraint.
    for constraint in inspector.get_check_constraints("questions"):
        if "question_type" in constraint.get("sqltext", ""):
            if not constraint.get("name"):
                raise RuntimeError("Unnamed question_type constraint requires manual inspection")
            with op.batch_alter_table("questions") as batch:
                batch.drop_constraint(constraint["name"], type_="check")
                batch.create_check_constraint(constraint["name"],
                    "question_type IN ('explanation','trace','boundary','modification','boundary_or_modification')")


def downgrade():
    raise RuntimeError("Review evidence must be retained; restore a verified backup for rollback")
