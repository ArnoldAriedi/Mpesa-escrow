"""Add EscrowTransaction.stk_last_queried_at.

N6: the lost-callback sweep asks Safaricom about stale PENDING_PAYMENT
escrows; this records when each was last asked so it is not queried on every
sweep.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0014_stk_last_queried_at"
down_revision = "0013_unapplied_payments"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("escrow_transactions")}
    if "stk_last_queried_at" not in columns:
        with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
            batch.add_column(sa.Column("stk_last_queried_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    bind = op.get_bind()
    columns = {c["name"] for c in inspect(bind).get_columns("escrow_transactions")}
    if "stk_last_queried_at" in columns:
        with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
            batch.drop_column("stk_last_queried_at")