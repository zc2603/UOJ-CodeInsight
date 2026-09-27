"""Independent quality audit metadata; existing questions remain unaudited."""
from alembic import op
import sqlalchemy as sa
revision = "0006_question_quality"
down_revision = "0005_timeout_submission"
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("generation_jobs")}
    for column in [
        sa.Column("quality_state", sa.String(20), nullable=False, server_default="not_requested"),
        sa.Column("quality_token", sa.String(36)),
        sa.Column("quality_lease_until", sa.DateTime(timezone=True)),
        sa.Column("quality_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_result", sa.JSON()), sa.Column("quality_raw", sa.Text()),
        sa.Column("quality_error", sa.Text()), sa.Column("quality_model", sa.String(100)),
        sa.Column("quality_version", sa.String(50)), sa.Column("quality_acknowledged", sa.JSON()),
        sa.Column("quality_finished_at", sa.DateTime(timezone=True)),
    ]:
        if column.name not in existing:
            op.add_column("generation_jobs", column)
    if "ix_quality_claim" not in {index["name"] for index in inspector.get_indexes("generation_jobs")}:
        op.create_index("ix_quality_claim", "generation_jobs", ["quality_state", "quality_lease_until"])


def downgrade():
    raise RuntimeError("Preserve quality review records; use a verified backup")
