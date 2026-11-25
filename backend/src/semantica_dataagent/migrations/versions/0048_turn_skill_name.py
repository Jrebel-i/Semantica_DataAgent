"""Record an explicitly selected Skill name on conversation turns."""

import sqlalchemy as sa
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("turns") as batch:
        batch.add_column(sa.Column("skill_name", sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table("turns") as batch:
        batch.drop_column("skill_name")
