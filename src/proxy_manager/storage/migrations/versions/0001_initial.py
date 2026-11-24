"""Create proxy snapshots, sync history and operation history."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "proxies",
        sa.Column("profile_id", sa.String(), primary_key=True),
        sa.Column("provider_id", sa.String(), primary_key=True),
        sa.Column("address", sa.String(), nullable=False),
        sa.Column("port_http", sa.Integer()),
        sa.Column("port_socks", sa.Integer()),
        sa.Column("username", sa.String()),
        sa.Column("password_encrypted", sa.LargeBinary()),
        sa.Column("country", sa.String()),
        sa.Column("status", sa.String()),
        sa.Column("order_id", sa.String()),
        sa.Column("expires_on", sa.Date()),
        sa.Column("checked_at", sa.DateTime()),
        sa.Column("check_result", sa.String()),
        sa.Column("latency_ms", sa.Integer()),
    )
    op.create_index("ix_proxies_profile_country", "proxies", ["profile_id", "country"])
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("count", sa.Integer()),
        sa.Column("reason", sa.String()),
    )
    op.create_index("ix_sync_runs_profile_started", "sync_runs", ["profile_id", "started_at"])
    op.create_table(
        "operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("happened_at", sa.DateTime(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
    )


def downgrade():
    op.drop_table("operations")
    op.drop_index("ix_sync_runs_profile_started", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_index("ix_proxies_profile_country", table_name="proxies")
    op.drop_table("proxies")
