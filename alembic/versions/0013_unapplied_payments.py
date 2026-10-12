"""Add the unapplied_payments table.

C5: money Safaricom confirmed as received that cannot be applied to an escrow
(no matching escrow, wrong state, amount/phone mismatch, missing metadata,
duplicate receipt) is recorded here for an admin to decide. It is never
refunded automatically. The UNAPPLIED_PAYMENTS ledger account needs no
migration: ensure_system_accounts creates it on first use.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0013_unapplied_payments"
down_revision = "0012_payout_originator_index_and_checks"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if "unapplied_payments" in inspect(bind).get_table_names():
        return
    op.create_table(
        "unapplied_payments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source", sa.String(length=10), nullable=False),
        sa.Column("provider_reference", sa.String(length=150), nullable=False),
        sa.Column("mpesa_receipt_number", sa.String(length=100), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("phone_number", sa.String(length=20), nullable=True),
        sa.Column("escrow_transaction_id", sa.Integer(), sa.ForeignKey("escrow_transactions.id", ondelete="RESTRICT"), nullable=True),
        sa.Column("reason", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="OPEN"),
        sa.Column("raw_payload", sa.Text(), nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("source", "provider_reference", name="uq_unapplied_source_ref"),
        sa.CheckConstraint("source IN ('STK','C2B')", name="ck_unapplied_source"),
        sa.CheckConstraint("status IN ('OPEN','APPLIED','REFUNDED_EXTERNALLY','WRITTEN_OFF')", name="ck_unapplied_status"),
    )
    op.create_index("ix_unapplied_payments_id", "unapplied_payments", ["id"])
    op.create_index("ix_unapplied_payments_mpesa_receipt_number", "unapplied_payments", ["mpesa_receipt_number"])
    op.create_index("ix_unapplied_payments_escrow_transaction_id", "unapplied_payments", ["escrow_transaction_id"])
    op.create_index("ix_unapplied_payments_status", "unapplied_payments", ["status"])


def downgrade():
    bind = op.get_bind()
    if "unapplied_payments" in inspect(bind).get_table_names():
        op.drop_table("unapplied_payments")