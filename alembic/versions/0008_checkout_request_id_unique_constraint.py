"""Add a DB-level unique constraint on escrow_transactions.mpesa_checkout_request_id.

mpesa_receipt_number already had one; checkout_request_id (the field
stk_callback actually looks transactions up by) only had an index. NULLs
are unaffected by a unique constraint (multiple NULLs are allowed by
standard SQL semantics), so pre-payment rows are unaffected.

Uses batch mode so the migration also runs on SQLite, which cannot ALTER
constraints directly. On PostgreSQL batch mode emits plain ALTER TABLE
statements, so behaviour there is unchanged.
"""
from alembic import op
from sqlalchemy import inspect

revision = "0008_checkout_request_id_unique_constraint"
down_revision = "0007_stk_unknown_state_and_order_idempotency"
branch_labels = None
depends_on = None

CONSTRAINT = "uq_escrow_transactions_checkout_request_id"


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {c["name"] for c in inspector.get_unique_constraints("escrow_transactions")}
    if CONSTRAINT not in existing:
        with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
            batch.create_unique_constraint(CONSTRAINT, ["mpesa_checkout_request_id"])


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing = {c["name"] for c in inspector.get_unique_constraints("escrow_transactions")}
    if CONSTRAINT in existing:
        with op.batch_alter_table("escrow_transactions", recreate="auto") as batch:
            batch.drop_constraint(CONSTRAINT, type_="unique")