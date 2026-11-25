"""Store encrypted personal Skill secrets."""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "skill_secrets",
        sa.Column("owner_id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), primary_key=True),
        sa.Column("skill_name", sa.String(), primary_key=True),
        sa.Column("secret_name", sa.String(), primary_key=True),
        sa.Column("ciphertext", sa.String(), nullable=False),
    )


def downgrade():
    op.drop_table("skill_secrets")
