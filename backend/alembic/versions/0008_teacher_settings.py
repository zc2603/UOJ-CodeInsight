"""Teacher defaults and quiz policy snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "0008_teacher_settings"
down_revision = "0007_lightweight_v1"
branch_labels = None
depends_on = None


def upgrade():
    # 0001 creates current metadata on empty databases; upgrade existing 0007
    # databases as well without adding the same columns twice.
    columns = {
        "admin_users": [sa.Column("settings_json", sa.JSON(), nullable=True),
            sa.Column("settings_revision", sa.Integer(), nullable=False, server_default="0")],
        "quizzes": [sa.Column("entry_minutes", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("reopen_minutes", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("grade_bands", sa.JSON(), nullable=True)],
    }
    inspector = sa.inspect(op.get_bind())
    for table, additions in columns.items():
        existing = {column["name"] for column in inspector.get_columns(table)}
        for column in additions:
            if column.name not in existing:
                op.add_column(table, column)


def downgrade():
    for name in ("grade_bands", "reopen_minutes", "entry_minutes"):
        op.drop_column("quizzes", name)
    op.drop_column("admin_users", "settings_revision")
    op.drop_column("admin_users", "settings_json")
