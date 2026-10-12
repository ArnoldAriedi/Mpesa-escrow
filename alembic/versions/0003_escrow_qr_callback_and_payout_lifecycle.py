"""Add buyer QR access, verified STK metadata, payout attempts and dispute states."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0003_escrow_qr_callback_and_payout_lifecycle"
down_revision = "0002_financial_integrity_constraints"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # PostgreSQL uses a native enum for EscrowStatus. SQLite's VARCHAR-backed
    # enum does not need an ALTER TYPE step.
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE escrowstatus ADD VALUE IF NOT EXISTS 'DISPUTED'")
        op.execute("ALTER TYPE escrowstatus ADD VALUE IF NOT EXISTS 'PARTIALLY_SETTLED'")

    escrow_columns = {c["name"] for c in inspector.get_columns("escrow_transactions")}
    with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
        if "qr_view_token" not in escrow_columns:
            batch.add_column(sa.Column("qr_view_token", sa.String(length=64), nullable=True))
        if "stk_amount" not in escrow_columns:
            batch.add_column(sa.Column("stk_amount", sa.Numeric(18, 2), nullable=True))
        if "stk_phone_number" not in escrow_columns:
            batch.add_column(sa.Column("stk_phone_number", sa.String(length=20), nullable=True))
        if "stk_callback_received_at" not in escrow_columns:
            batch.add_column(sa.Column("stk_callback_received_at", sa.DateTime(timezone=True), nullable=True))
        uniques = inspector.get_unique_constraints("escrow_transactions")
        if not any(set(u.get("column_names") or []) == {"mpesa_receipt_number"} for u in uniques):
            batch.create_unique_constraint("uq_escrow_mpesa_receipt_number", ["mpesa_receipt_number"])

    # Indexes are created outside the batch block so this works for both fresh
    # and upgraded SQLite databases.
    inspector = inspect(bind)
    indexes = {i["name"] for i in inspector.get_indexes("escrow_transactions")}
    if "ix_escrow_transactions_qr_view_token" not in indexes:
        op.create_index("ix_escrow_transactions_qr_view_token", "escrow_transactions", ["qr_view_token"], unique=False)

    payout_columns = {c["name"] for c in inspect(bind).get_columns("payouts")}
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        if "attempt_count" not in payout_columns:
            batch.add_column(sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
        if "last_attempt_at" not in payout_columns:
            batch.add_column(sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True))
        if "timed_out_at" not in payout_columns:
            batch.add_column(sa.Column("timed_out_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    # Native PostgreSQL enum values cannot be safely removed in-place. The new
    # enum values are therefore intentionally left in place on downgrade.
    bind = op.get_bind()
    inspector = inspect(bind)
    if "escrow_transactions" in inspector.get_table_names():
        indexes = {i["name"] for i in inspector.get_indexes("escrow_transactions")}
        if "ix_escrow_transactions_qr_view_token" in indexes:
            op.drop_index("ix_escrow_transactions_qr_view_token", table_name="escrow_transactions")
        with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
            try:
                batch.drop_constraint("uq_escrow_mpesa_receipt_number", type_="unique")
            except Exception:
                pass
            for name in ("stk_callback_received_at", "stk_phone_number", "stk_amount", "qr_view_token"):
                try:
                    batch.drop_column(name)
                except Exception:
                    pass
    if "payouts" in inspector.get_table_names():
        with op.batch_alter_table("payouts", recreate="auto") as batch:
            for name in ("timed_out_at", "last_attempt_at", "attempt_count"):
                try:
                    batch.drop_column(name)
                except Exception:
                    pass
