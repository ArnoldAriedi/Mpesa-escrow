"""Add seller KYC fields, admin users + audit log, USSD session created_at, notification attempts."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "0005_kyc_admin_audit_and_notification_attempts"
down_revision = "0004_refund_order_statuses"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # --- sellers: KYC fields ---------------------------------------------
    seller_columns = {c["name"] for c in inspector.get_columns("sellers")}
    kyc_enum = sa.Enum("UNVERIFIED", "PENDING", "VERIFIED", "REJECTED", name="sellerkycstatus")
    kyc_enum.create(bind, checkfirst=True)
    with op.batch_alter_table("sellers", recreate="auto") as batch:
        if "national_id" not in seller_columns:
            batch.add_column(sa.Column("national_id", sa.String(length=50), nullable=True))
        if "kyc_status" not in seller_columns:
            # New sellers created after this migration default to UNVERIFIED in
            # the application layer. Existing sellers are backfilled to
            # UNVERIFIED too, on the theory that "we don't actually know" is
            # the honest default - deployers with sellers they already trust
            # should explicitly mark them VERIFIED via
            # POST /admin/sellers/{id}/kyc (or a one-off data migration)
            # before relying on this gate, rather than being silently blocked
            # or silently grandfathered in.
            batch.add_column(sa.Column("kyc_status", kyc_enum, nullable=False, server_default="UNVERIFIED"))
        if "kyc_verified_at" not in seller_columns:
            batch.add_column(sa.Column("kyc_verified_at", sa.DateTime(timezone=True), nullable=True))
        if "kyc_notes" not in seller_columns:
            batch.add_column(sa.Column("kyc_notes", sa.Text(), nullable=True))

    # --- ussd_sessions: created_at ----------------------------------------
    ussd_columns = {c["name"] for c in inspect(bind).get_columns("ussd_sessions")}
    with op.batch_alter_table("ussd_sessions", recreate="auto") as batch:
        if "created_at" not in ussd_columns:
            batch.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()))

    # --- notifications: attempts -------------------------------------------
    notification_columns = {c["name"] for c in inspect(bind).get_columns("notifications")}
    with op.batch_alter_table("notifications", recreate="auto") as batch:
        if "attempts" not in notification_columns:
            batch.add_column(sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"))

    # --- admin_users --------------------------------------------------------
    if "admin_users" not in inspector.get_table_names():
        op.create_table(
            "admin_users",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("email", sa.String(length=255), nullable=True),
            sa.Column("api_key_hash", sa.String(length=64), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_unique_constraint("uq_admin_users_email", "admin_users", ["email"])
        op.create_unique_constraint("uq_admin_users_api_key_hash", "admin_users", ["api_key_hash"])
        op.create_index("ix_admin_users_email", "admin_users", ["email"])
        op.create_index("ix_admin_users_api_key_hash", "admin_users", ["api_key_hash"])

    # --- audit_log_entries ---------------------------------------------------
    if "audit_log_entries" not in inspector.get_table_names():
        op.create_table(
            "audit_log_entries",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("actor", sa.String(length=255), nullable=False),
            sa.Column("admin_user_id", sa.Integer(), sa.ForeignKey("admin_users.id"), nullable=True),
            sa.Column("action", sa.String(length=100), nullable=False),
            sa.Column("target_type", sa.String(length=50), nullable=True),
            sa.Column("target_id", sa.String(length=100), nullable=True),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        )
        op.create_index("ix_audit_log_entries_admin_user_id", "audit_log_entries", ["admin_user_id"])
        op.create_index("ix_audit_log_entries_action", "audit_log_entries", ["action"])
        op.create_index("ix_audit_log_entries_created_at", "audit_log_entries", ["created_at"])


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    if "audit_log_entries" in inspector.get_table_names():
        op.drop_table("audit_log_entries")
    if "admin_users" in inspector.get_table_names():
        op.drop_table("admin_users")

    if "notifications" in inspector.get_table_names():
        with op.batch_alter_table("notifications", recreate="auto") as batch:
            try:
                batch.drop_column("attempts")
            except Exception:
                pass

    if "ussd_sessions" in inspector.get_table_names():
        with op.batch_alter_table("ussd_sessions", recreate="auto") as batch:
            try:
                batch.drop_column("created_at")
            except Exception:
                pass

    if "sellers" in inspector.get_table_names():
        with op.batch_alter_table("sellers", recreate="auto") as batch:
            for name in ("kyc_notes", "kyc_verified_at", "kyc_status", "national_id"):
                try:
                    batch.drop_column(name)
                except Exception:
                    pass

    # Native PostgreSQL enum values/types cannot always be dropped cleanly if
    # still referenced elsewhere; only drop when we know nothing else uses it.
    if bind.dialect.name == "postgresql":
        sa.Enum(name="sellerkycstatus").drop(bind, checkfirst=True)
