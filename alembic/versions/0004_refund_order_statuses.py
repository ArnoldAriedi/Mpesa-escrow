"""Add explicit order states for pending and failed refunds."""
from alembic import op

revision = "0004_refund_order_statuses"
down_revision = "0003_escrow_qr_callback_and_payout_lifecycle"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'REFUND_PENDING'")
        op.execute("ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS 'REFUND_FAILED'")


def downgrade():
    # PostgreSQL enum values cannot safely be removed in-place; leave them in the type.
    pass
