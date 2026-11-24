"""Add the seven VPS monitoring tables."""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def profile_key():
    return sa.Column("profile_id", sa.String(36), sa.ForeignKey("vps_profiles.id"), primary_key=True)


def upgrade():
    op.create_table(
        "vps_profiles",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("veid", sa.String(20)),
        sa.Column("ssh_host", sa.String(80)),
        sa.Column("ssh_port", sa.Integer(), nullable=False),
        sa.Column("configuration", sa.Text(), nullable=False),
        sa.Column("key_algorithm", sa.String(80)),
        sa.Column("key_sha256", sa.String(64)),
    )
    op.create_index("ix_vps_active", "vps_profiles", ["active"], unique=True, sqlite_where=sa.text("active = 1"))
    op.create_table(
        "kiwivm_service_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("vps_profiles.id"), nullable=False),
        sa.Column("endpoint", sa.String(32), nullable=False),
        sa.Column("captured", sa.Float(), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_index("ix_kiwivm_snapshots_recent", "kiwivm_service_snapshots", ["profile_id", "endpoint", "id"])
    op.create_table(
        "kiwivm_usage_points", profile_key(),
        sa.Column("timestamp", sa.BigInteger(), primary_key=True),
        sa.Column("cadence", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.Text(), primary_key=True),
    )
    op.create_table(
        "vps_network_samples", profile_key(),
        sa.Column("captured", sa.Float(), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("interface", sa.String(64)),
        sa.Column("rx_bytes", sa.Text()),
        sa.Column("tx_bytes", sa.Text()),
        sa.Column("rx_delta", sa.Text()),
        sa.Column("tx_delta", sa.Text()),
        sa.Column("elapsed", sa.Float()),
        sa.Column("rx_mbps", sa.Float()),
        sa.Column("tx_mbps", sa.Float()),
    )
    op.create_table(
        "vps_network_rollups", profile_key(),
        sa.Column("period", sa.String(8), primary_key=True),
        sa.Column("bucket", sa.String(16), primary_key=True),
        sa.Column("interface", sa.String(64), primary_key=True),
        sa.Column("samples", sa.Integer(), nullable=False),
        sa.Column("gaps", sa.Integer(), nullable=False),
        sa.Column("seconds", sa.Float(), nullable=False),
        sa.Column("rx_bytes", sa.Text(), nullable=False),
        sa.Column("tx_bytes", sa.Text(), nullable=False),
    )
    op.create_table(
        "vps_system_samples", profile_key(),
        sa.Column("captured", sa.Float(), primary_key=True),
        sa.Column("payload", sa.Text(), nullable=False),
    )
    op.create_table(
        "monitoring_sync_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("profile_id", sa.String(36), sa.ForeignKey("vps_profiles.id"), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("finished", sa.Float(), nullable=False),
        sa.Column("outcome", sa.String(48), nullable=False),
    )
    op.create_index("ix_monitoring_runs_profile", "monitoring_sync_runs", ["profile_id", "source", "id"])


def downgrade():
    raise RuntimeError("restore_verified_backup_to_downgrade")
