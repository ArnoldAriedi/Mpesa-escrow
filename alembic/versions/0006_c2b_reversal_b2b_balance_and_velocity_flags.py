"""Add payout.method, seller payout channel, order review flags, unmatched C2B payments, and account balance checks."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0006_c2b_reversal_b2b_balance_and_velocity_flags"
down_revision = "0005_kyc_admin_audit_and_notification_attempts"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # --- payouts: method (B2C / REVERSAL / B2B) -----------------------------
    payout_columns = {c["name"] for c in inspector.get_columns("payouts")}
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        if "method" not in payout_columns:
            batch.add_column(sa.Column("method", sa.String(length=20), nullable=False, server_default="B2C"))

    # --- sellers: payout channel + shortcode --------------------------------
    seller_columns = {c["name"] for c in inspector.get_columns("sellers")}
    channel_enum = sa.Enum("MPESA_PHONE", "TILL", "PAYBILL", name="sellerpayoutchannel")
    channel_enum.create(bind, checkfirst=True)
    with op.batch_alter_table("sellers", recreate="auto") as batch:
        if "payout_channel" not in seller_columns:
            batch.add_column(sa.Column("payout_channel", channel_enum, nullable=False, server_default="MPESA_PHONE"))
        if "payout_shortcode" not in seller_columns:
            batch.add_column(sa.Column("payout_shortcode", sa.String(length=20), nullable=True))

    # --- orders: review flags ------------------------------------------------
    order_columns = {c["name"] for c in inspector.get_columns("orders")}
    with op.batch_alter_table("orders", recreate="auto") as batch:
        if "flagged_for_review" not in order_columns:
            batch.add_column(sa.Column("flagged_for_review", sa.Boolean(), nullable=False, server_default=sa.false()))
        if "flag_reason" not in order_columns:
            batch.add_column(sa.Column("flag_reason", sa.Text(), nullable=True))

    # --- unmatched_c2b_payments ------------------------------------------------
    if "unmatched_c2b_payments" not in inspector.get_table_names():
        op.create_table(
            "unmatched_c2b_payments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("mpesa_receipt_number", sa.String(length=50), nullable=False),
            sa.Column("amount", sa.Numeric(18, 2), nullable=False),
            sa.Column("msisdn", sa.String(length=20), nullable=False),
            sa.Column("bill_ref_number", sa.String(length=50), nullable=False),
            sa.Column("transaction_time", sa.String(length=20), nullable=True),
            sa.Column("matched_order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=True),
            sa.Column("resolved", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("resolution_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_unique_constraint("uq_unmatched_c2b_receipt", "unmatched_c2b_payments", ["mpesa_receipt_number"])
        op.create_index("ix_unmatched_c2b_receipt", "unmatched_c2b_payments", ["mpesa_receipt_number"])

    # --- account_balance_checks ------------------------------------------------
    if "account_balance_checks" not in inspector.get_table_names():
        op.create_table(
            "account_balance_checks",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("conversation_id", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
            sa.Column("result_desc", sa.Text(), nullable=True),
            sa.Column("working_account_balance", sa.Numeric(18, 2), nullable=True),
            sa.Column("raw_result", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_unique_constraint("uq_account_balance_checks_conversation_id", "account_balance_checks", ["conversation_id"])
        op.create_index("ix_account_balance_checks_conversation_id", "account_balance_checks", ["conversation_id"])


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if "account_balance_checks" in inspector.get_table_names():
        op.drop_table("account_balance_checks")
    if "unmatched_c2b_payments" in inspector.get_table_names():
        op.drop_table("unmatched_c2b_payments")

    if "orders" in inspector.get_table_names():
        with op.batch_alter_table("orders", recreate="auto") as batch:
            for name in ("flag_reason", "flagged_for_review"):
                try:
                    batch.drop_column(name)
                except Exception:
                    pass

    if "sellers" in inspector.get_table_names():
        with op.batch_alter_table("sellers", recreate="auto") as batch:
            for name in ("payout_shortcode", "payout_channel"):
                try:
                    batch.drop_column(name)
                except Exception:
                    pass

    if "payouts" in inspector.get_table_names():
        with op.batch_alter_table("payouts", recreate="auto") as batch:
            try:
                batch.drop_column("method")
            except Exception:
                pass

    if bind.dialect.name == "postgresql":
        sa.Enum(name="sellerpayoutchannel").drop(bind, checkfirst=True)
