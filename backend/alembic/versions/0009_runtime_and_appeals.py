"""Appeal defaults and durable runtime configuration (no credentials)."""
from alembic import op
import sqlalchemy as sa
from app.models.entities import RuntimeConfiguration, RuntimeConfigurationAudit, RuntimeWorker

revision = "0009_runtime_and_appeals"
down_revision = "0008_teacher_settings"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in sa.inspect(bind).get_columns("quizzes")}
    for column in [sa.Column("appeal_window_days", sa.Integer(), nullable=True),
        sa.Column("appeal_prompt", sa.Text(), nullable=True)]:
        if column.name not in columns:
            op.add_column("quizzes", column)
    for table in (RuntimeConfiguration.__table__, RuntimeConfigurationAudit.__table__, RuntimeWorker.__table__):
        table.create(bind, checkfirst=True)
    if not bind.execute(sa.select(RuntimeConfiguration.id).where(RuntimeConfiguration.id == 1)).first():
        bind.execute(sa.insert(RuntimeConfiguration).values(id=1, revision=0))


def downgrade():
    for table in ("runtime_workers", "runtime_configuration_audits", "runtime_configuration"):
        op.drop_table(table)
    op.drop_column("quizzes", "appeal_prompt")
    op.drop_column("quizzes", "appeal_window_days")
