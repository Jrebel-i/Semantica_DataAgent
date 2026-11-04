import uuid

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tokens", sa.Column("id", sa.String(), nullable=True))
    op.add_column("tokens", sa.Column("created_at", sa.DateTime(), nullable=True))
    op.add_column("tokens", sa.Column("last_used_at", sa.DateTime(), nullable=True))
    op.add_column(
        "tokens",
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT digest FROM tokens")).fetchall()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE tokens SET id = :id, created_at = CURRENT_TIMESTAMP WHERE digest = :digest"
            ),
            {"id": uuid.uuid4().hex, "digest": row[0]},
        )
    with op.batch_alter_table("tokens") as batch:
        batch.alter_column("id", nullable=False)
        batch.alter_column("created_at", nullable=False)
        batch.create_unique_constraint("uq_tokens_id", ["id"])


def downgrade():
    with op.batch_alter_table("tokens") as batch:
        batch.drop_constraint("uq_tokens_id", type_="unique")
        batch.drop_column("revoked")
        batch.drop_column("last_used_at")
        batch.drop_column("created_at")
        batch.drop_column("id")
