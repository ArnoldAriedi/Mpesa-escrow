"""Add the payout uniqueness guard used for refund/release idempotency."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0002_financial_integrity_constraints"
down_revision = "0001_initial_escrow_schema"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "payouts" not in inspector.get_table_names():
        return
    uniques = inspector.get_unique_constraints("payouts")
    if any(set(u.get("column_names") or []) == {"escrow_transaction_id", "purpose"} for u in uniques):
        return
    # batch_alter_table works on SQLite as well as PostgreSQL/MySQL.
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        batch.create_unique_constraint(
            "uq_payout_escrow_purpose",
            ["escrow_transaction_id", "purpose"],
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    if "payouts" not in inspector.get_table_names():
        return
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        try:
            batch.drop_constraint("uq_payout_escrow_purpose", type_="unique")
        except Exception:
            pass
