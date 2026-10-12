"""Add EscrowStatus.STK_INITIATION_UNKNOWN and Order.idempotency_key."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0007_stk_unknown_state_and_order_idempotency"
down_revision = "0006_c2b_reversal_b2b_balance_and_velocity_flags"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE escrowstatus ADD VALUE IF NOT EXISTS 'STK_INITIATION_UNKNOWN'")

    order_columns = {c["name"] for c in inspector.get_columns("orders")}
    with op.batch_alter_table("orders", recreate="auto") as batch:
        if "idempotency_key" not in order_columns:
            batch.add_column(sa.Column("idempotency_key", sa.String(length=150), nullable=True))

    existing_constraints = {c["name"] for c in inspector.get_unique_constraints("orders")}
    if "uq_order_buyer_idempotency_key" not in existing_constraints:
        op.create_unique_constraint("uq_order_buyer_idempotency_key", "orders", ["buyer_phone", "idempotency_key"])


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    existing_constraints = {c["name"] for c in inspector.get_unique_constraints("orders")}
    if "uq_order_buyer_idempotency_key" in existing_constraints:
        op.drop_constraint("uq_order_buyer_idempotency_key", "orders", type_="unique")

    order_columns = {c["name"] for c in inspector.get_columns("orders")}
    if "idempotency_key" in order_columns:
        with op.batch_alter_table("orders", recreate="auto") as batch:
            try:
                batch.drop_column("idempotency_key")
            except Exception:
                pass

    # PostgreSQL enum values cannot safely be removed in-place; leave them in the type.
