"""Record which protocol was checked."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("proxies", sa.Column("check_protocol", sa.String()))


def downgrade():
    with op.batch_alter_table("proxies") as batch:
        batch.drop_column("check_protocol")
