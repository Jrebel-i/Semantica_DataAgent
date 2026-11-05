import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "knowledge_generations",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("build_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=True),
    )
    op.create_table(
        "generation_pointer",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.String(), nullable=False),
        sa.CheckConstraint("id = 1"),
    )
    op.execute(
        "INSERT INTO knowledge_generations(name) SELECT value FROM settings WHERE key = 'knowledge_generation' AND value <> ''"
    )
    op.execute(
        "INSERT INTO generation_pointer(id, generation) SELECT 1, name FROM knowledge_generations"
    )


def downgrade():
    op.drop_table("generation_pointer")
    op.drop_table("knowledge_generations")
