"""Index Payout.originator_conversation_id and constrain Payout status/purpose/method.

C4: late callbacks are matched by the OriginatorConversationID we generate and
persist before sending, so that lookup needs an index.
N19: payout status/purpose/method were free-form strings; a typo silently
created an invalid state.
"""
from alembic import op
from sqlalchemy import inspect

revision = "0012_payout_originator_index_and_checks"
down_revision = "0011_payout_stale_alert_tracking"
branch_labels = None
depends_on = None

INDEX_NAME = "ix_payouts_originator_conversation_id"
CHECKS = {
    "ck_payout_status": "status IN ('PENDING','SUBMITTING','INITIATED','COMPLETED','FAILED','TIMEOUT')",
    "ck_payout_purpose": "purpose IN ('RELEASE','REFUND')",
    "ck_payout_method": "method IN ('B2C','REVERSAL','B2B')",
}


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    indexes = {i["name"] for i in inspector.get_indexes("payouts")}
    checks = {c["name"] for c in inspector.get_check_constraints("payouts") if c.get("name")}
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        if INDEX_NAME not in indexes:
            batch.create_index(INDEX_NAME, ["originator_conversation_id"])
        for name, expression in CHECKS.items():
            if name not in checks:
                batch.create_check_constraint(name, expression)


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    indexes = {i["name"] for i in inspector.get_indexes("payouts")}
    checks = {c["name"] for c in inspector.get_check_constraints("payouts") if c.get("name")}
    with op.batch_alter_table("payouts", recreate="auto") as batch:
        for name in CHECKS:
            if name in checks:
                batch.drop_constraint(name, type_="check")
        if INDEX_NAME in indexes:
            batch.drop_index(INDEX_NAME)