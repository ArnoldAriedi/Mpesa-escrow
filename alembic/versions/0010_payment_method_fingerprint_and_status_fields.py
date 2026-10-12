"""Add EscrowTransaction.payment_method, Order.idempotency_fingerprint,
TransactionStatusQuery.payout_method, and UnmatchedC2BPayment.status.

Corresponds to the second round of v20 corrections: #8, #12, #14, #18.

- escrow_transactions.payment_method: NULL for existing rows (funded before
  this field existed); _select_refund_method() already treats NULL as "not
  provably STK" and falls back to B2C, so no backfill is attempted — we
  can't reliably tell STK from C2B for historical rows from the columns
  alone without risking a wrong guess.
- orders.idempotency_fingerprint: NULL for existing rows. The idempotency
  check only enforces a fingerprint match when one is stored, so existing
  orders keep behaving exactly as before until they're naturally superseded.
- transaction_status_queries.payout_method: NULL for existing rows;
  denormalized convenience field only, still derivable via payout_id join.
- unmatched_c2b_payments.status: backfilled from the existing `resolved`
  boolean (and matched_order_id, to distinguish an application from a
  generic manual resolution) so the new column isn't just NULL/UNMATCHED
  for rows that were already actually resolved.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0010_payment_method_fingerprint_and_status_fields"
down_revision = "0009_admin_roles_payout_txn_uniqueness_and_status_queries"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    escrow_columns = {c["name"] for c in inspector.get_columns("escrow_transactions")}
    with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
        if "payment_method" not in escrow_columns:
            batch.add_column(sa.Column("payment_method", sa.String(length=10), nullable=True))

    order_columns = {c["name"] for c in inspector.get_columns("orders")}
    with op.batch_alter_table("orders", recreate="auto") as batch:
        if "idempotency_fingerprint" not in order_columns:
            batch.add_column(sa.Column("idempotency_fingerprint", sa.String(length=64), nullable=True))

    tsq_columns = {c["name"] for c in inspector.get_columns("transaction_status_queries")}
    with op.batch_alter_table("transaction_status_queries", recreate="auto") as batch:
        if "payout_method" not in tsq_columns:
            batch.add_column(sa.Column("payout_method", sa.String(length=20), nullable=True))

    unmatched_columns = {c["name"] for c in inspector.get_columns("unmatched_c2b_payments")}
    with op.batch_alter_table("unmatched_c2b_payments", recreate="auto") as batch:
        if "status" not in unmatched_columns:
            batch.add_column(sa.Column("status", sa.String(length=30), nullable=False, server_default="UNMATCHED"))

    if "status" not in unmatched_columns:
        unmatched = sa.table(
            "unmatched_c2b_payments",
            sa.column("resolved", sa.Boolean),
            sa.column("matched_order_id", sa.Integer),
            sa.column("status", sa.String),
        )
        # A resolved row with a matched_order_id was applied to an order;
        # a resolved row without one was some other manual resolution —
        # CLOSED is the safest generic label since we can't recover whether
        # it was originally REFUNDED_EXTERNALLY or WRITTEN_OFF.
        op.execute(unmatched.update().where(unmatched.c.resolved == True, unmatched.c.matched_order_id.isnot(None)).values(status="APPLIED_TO_ORDER"))  # noqa: E712
        op.execute(unmatched.update().where(unmatched.c.resolved == True, unmatched.c.matched_order_id.is_(None)).values(status="CLOSED"))  # noqa: E712


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    unmatched_columns = {c["name"] for c in inspector.get_columns("unmatched_c2b_payments")}
    with op.batch_alter_table("unmatched_c2b_payments", recreate="auto") as batch:
        if "status" in unmatched_columns:
            batch.drop_column("status")

    tsq_columns = {c["name"] for c in inspector.get_columns("transaction_status_queries")}
    with op.batch_alter_table("transaction_status_queries", recreate="auto") as batch:
        if "payout_method" in tsq_columns:
            batch.drop_column("payout_method")

    order_columns = {c["name"] for c in inspector.get_columns("orders")}
    with op.batch_alter_table("orders", recreate="auto") as batch:
        if "idempotency_fingerprint" in order_columns:
            batch.drop_column("idempotency_fingerprint")

    escrow_columns = {c["name"] for c in inspector.get_columns("escrow_transactions")}
    with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
        if "payment_method" in escrow_columns:
            batch.drop_column("payment_method")
