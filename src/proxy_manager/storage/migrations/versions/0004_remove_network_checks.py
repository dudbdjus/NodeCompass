"""Remove network check fields and operation entries from the active database."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

REMOVED_COLUMNS = ("checked_at", "check_result", "check_protocol", "latency_ms")


def upgrade():
    for column in REMOVED_COLUMNS:
        op.drop_column("proxies", column)
    op.execute("DELETE FROM operations WHERE action = 'check'")


def downgrade():
    raise RuntimeError("Restore the verified pre-upgrade SQLite backup to recover the original data.")
