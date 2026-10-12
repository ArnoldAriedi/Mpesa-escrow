"""Add Payout.last_stale_alert_at."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0011_payout_stale_alert_tracking"
down_revision = "0010_payment_method_fingerprint_and_status_fields"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    payout_columns = {c["name"] for c in inspector.get_columns("payouts")}
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        if "last_stale_alert_at" not in payout_columns:
            batch.add_column(sa.Column("last_stale_alert_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    payout_columns = {c["name"] for c in inspector.get_columns("payouts")}
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        if "last_stale_alert_at" in payout_columns:
            batch.drop_column("last_stale_alert_at")
