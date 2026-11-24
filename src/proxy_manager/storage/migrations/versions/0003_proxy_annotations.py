"""Store local proxy regions and tags independently of sync snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "proxy_annotations",
        sa.Column("profile_id", sa.String(), primary_key=True),
        sa.Column("provider_id", sa.String(), primary_key=True),
        sa.Column("region", sa.String()),
        sa.Column("tag", sa.String()),
    )


def downgrade():
    op.drop_table("proxy_annotations")
