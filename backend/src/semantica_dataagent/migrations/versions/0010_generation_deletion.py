import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "UPDATE knowledge_generations SET build_id = (SELECT id FROM builds WHERE builds.generation = knowledge_generations.name AND builds.status = 'succeeded' ORDER BY id DESC LIMIT 1)"
    )
    op.execute(
        "UPDATE knowledge_generations SET created_at = (SELECT finished_at FROM builds WHERE builds.id = knowledge_generations.build_id)"
    )
    op.add_column(
        "knowledge_generations",
        sa.Column("deleting", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade():
    op.drop_column("knowledge_generations", "deleting")
