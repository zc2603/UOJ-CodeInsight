"""Persist bounded model availability checks for each saved configuration."""
from alembic import op
import sqlalchemy as sa

revision = "0010_model_switch_tests"
down_revision = "0009_runtime_and_appeals"
branch_labels = None
depends_on = None


def upgrade():
    names = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("runtime_configuration_audits")}
    if "model_tests_json" not in names:
        op.add_column("runtime_configuration_audits", sa.Column("model_tests_json", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("runtime_configuration_audits", "model_tests_json")
