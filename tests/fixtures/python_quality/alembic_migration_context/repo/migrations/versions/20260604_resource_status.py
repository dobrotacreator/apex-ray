import sqlalchemy as sa
from alembic import op

revision = "20260604_resource_status"
down_revision = "20260603_resource"


def upgrade() -> None:
    op.add_column(
        "resource",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.alter_column("resource", "status", server_default=None)
    op.create_index("ix_resource_status", "resource", ["status"])


def downgrade() -> None:
    op.drop_index("ix_resource_status", table_name="resource")
    op.drop_column("resource", "status")
