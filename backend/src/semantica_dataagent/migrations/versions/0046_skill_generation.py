"""Give each uploaded Skill a distinct immutable generation."""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("catalog_skills", sa.Column("generation", sa.String(), nullable=True))
    op.execute("UPDATE catalog_skills SET generation = lower(hex(randomblob(16)))")
    with op.batch_alter_table("catalog_skills") as batch:
        batch.alter_column("generation", existing_type=sa.String(), nullable=False)


def downgrade():
    with op.batch_alter_table("catalog_skills") as batch:
        batch.drop_column("generation")
