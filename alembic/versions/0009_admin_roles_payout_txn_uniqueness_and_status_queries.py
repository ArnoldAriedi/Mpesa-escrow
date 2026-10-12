"""Add admin_users.role (RBAC), a unique constraint on payouts.transaction_id,
and the transaction_status_queries table.

Corresponds to CORRECTIONS.md items #7, #11, #14, #15, #16.

- admin_users.role: existing rows are backfilled to SUPER_ADMIN so admins
  created before this migration are not silently locked out of newly
  role-gated endpoints (see require_admin_role in escrow_main.py). New
  registrations must explicitly choose a (non-SUPER_ADMIN, by default) role.
- payouts.transaction_id: a Safaricom transaction receipt/ID should never
  settle two different internal payouts. NULLs (payouts that never
  completed) are unaffected by a unique constraint.
- transaction_status_queries: durable, structured record of each
  TransactionStatusQuery request/response, so "what happened to payout
  #123" has an auditable answer instead of only a log line.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0009_admin_roles_payout_txn_uniqueness_and_status_queries"
down_revision = "0008_checkout_request_id_unique_constraint"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # --- admin_users: role ------------------------------------------------
    admin_columns = {c["name"] for c in inspector.get_columns("admin_users")}
    with op.batch_alter_table("admin_users", recreate="auto") as batch:
        if "role" not in admin_columns:
            batch.add_column(sa.Column("role", sa.String(length=30), nullable=False, server_default="SUPER_ADMIN"))

    # --- payouts: unique transaction_id ------------------------------------
    existing_payout_constraints = {c["name"] for c in inspector.get_unique_constraints("payouts")}
    if "uq_payout_transaction_id" not in existing_payout_constraints:
        op.create_unique_constraint(
            "uq_payout_transaction_id",
            "payouts",
            ["transaction_id"],
        )

    # --- transaction_status_queries -----------------------------------------
    if "transaction_status_queries" not in inspector.get_table_names():
        op.create_table(
            "transaction_status_queries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("payout_id", sa.Integer(), sa.ForeignKey("payouts.id", ondelete="RESTRICT"), nullable=True, index=True),
            sa.Column("query_conversation_id", sa.String(length=150), nullable=False, unique=True, index=True),
            sa.Column("queried_transaction_id", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="PENDING"),
            sa.Column("result_code", sa.Integer(), nullable=True),
            sa.Column("result_desc", sa.Text(), nullable=True),
            sa.Column("transaction_status", sa.String(length=50), nullable=True),
            sa.Column("receipt_number", sa.String(length=100), nullable=True),
            sa.Column("amount", sa.Numeric(18, 2), nullable=True),
            sa.Column("raw_response", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if "transaction_status_queries" in inspector.get_table_names():
        op.drop_table("transaction_status_queries")

    existing_payout_constraints = {c["name"] for c in inspector.get_unique_constraints("payouts")}
    if "uq_payout_transaction_id" in existing_payout_constraints:
        op.drop_constraint("uq_payout_transaction_id", "payouts", type_="unique")

    admin_columns = {c["name"] for c in inspector.get_columns("admin_users")}
    with op.batch_alter_table("admin_users", recreate="auto") as batch:
        if "role" in admin_columns:
            batch.drop_column("role")
