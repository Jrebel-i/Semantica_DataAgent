import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tokens",
        sa.Column("digest", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.String(), nullable=False),
    )


def downgrade():
    op.drop_table("settings")
    op.drop_table("tokens")
