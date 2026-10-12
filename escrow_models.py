import enum
import hashlib
import os
import secrets
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint, func, select
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./escrow.db")

if DATABASE_URL.startswith("postgresql+asyncpg"):
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(
        DATABASE_URL,
        echo=False,
        poolclass=NullPool,
        connect_args={"statement_cache_size": 0, "timeout": 30},
    )
else:
    engine = create_async_engine(DATABASE_URL, echo=False)

# SQLite ignores foreign keys unless told otherwise; match PostgreSQL delete behavior in dev.
if DATABASE_URL.startswith("sqlite"):
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class EscrowStatus(str, enum.Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    ESCROW_HELD = "ESCROW_HELD"
    DELIVERY_CONFIRMED = "DELIVERY_CONFIRMED"
    DISPUTED = "DISPUTED"
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    REFUND_INITIATED = "REFUND_INITIATED"
    REFUNDED = "REFUNDED"
    PAYMENT_FAILED = "PAYMENT_FAILED"
    PAYOUT_FAILED = "PAYOUT_FAILED"
    REFUND_FAILED = "REFUND_FAILED"
    # Initiation had an unknown outcome (e.g. timeout after sending), so the PIN prompt may still arrive.
    # Distinct from PAYMENT_FAILED: a retry could double-charge. Needs manual reconciliation.
    STK_INITIATION_UNKNOWN = "STK_INITIATION_UNKNOWN"


class PayeeRole(str, enum.Enum):
    RIDER = "RIDER"
    SELLER = "SELLER"
    BUSINESS = "BUSINESS"


class AdminRole(str, enum.Enum):
    """Flat admin permission tiers. Only SUPER_ADMIN can manage other admins."""
    SUPER_ADMIN = "SUPER_ADMIN"
    FINANCE_ADMIN = "FINANCE_ADMIN"
    KYC_ADMIN = "KYC_ADMIN"
    DISPUTE_ADMIN = "DISPUTE_ADMIN"
    OPERATIONS_ADMIN = "OPERATIONS_ADMIN"


class LedgerAccountType(str, enum.Enum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


class LedgerEntryDirection(str, enum.Enum):
    DEBIT = "DEBIT"
    CREDIT = "CREDIT"


class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    PAYMENT_PENDING = "PAYMENT_PENDING"
    PAID = "PAID"
    ASSIGNED = "ASSIGNED"
    PICKED_UP = "PICKED_UP"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    REFUND_PENDING = "REFUND_PENDING"
    REFUND_FAILED = "REFUND_FAILED"
    DISPUTED = "DISPUTED"


class DeliveryStatus(str, enum.Enum):
    UNASSIGNED = "UNASSIGNED"
    ASSIGNED = "ASSIGNED"
    ACCEPTED = "ACCEPTED"
    PICKED_UP = "PICKED_UP"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class DisputeStatus(str, enum.Enum):
    OPEN = "OPEN"
    UNDER_REVIEW = "UNDER_REVIEW"
    RESOLVED_REFUND = "RESOLVED_REFUND"
    RESOLVED_RELEASE = "RESOLVED_RELEASE"
    RESOLVED_PARTIAL = "RESOLVED_PARTIAL"
    CLOSED = "CLOSED"


class NotificationChannel(str, enum.Enum):
    SMS = "SMS"
    PUSH = "PUSH"
    EMAIL = "EMAIL"
    IN_APP = "IN_APP"


class NotificationStatus(str, enum.Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class SellerKycStatus(str, enum.Enum):
    """Identity-verification state, independent of is_active. Payout eligibility needs both."""
    UNVERIFIED = "UNVERIFIED"
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    REJECTED = "REJECTED"


class SellerPayoutChannel(str, enum.Enum):
    """MPESA_PHONE pays via B2C; TILL and PAYBILL pay the seller's merchant account via B2B."""
    MPESA_PHONE = "MPESA_PHONE"
    TILL = "TILL"
    PAYBILL = "PAYBILL"


class UserRole(str, enum.Enum):
    BUYER = "BUYER"
    SELLER = "SELLER"
    ADMIN = "ADMIN"
    RIDER = "RIDER"


class DeliveryAgent(Base):
    __tablename__ = "delivery_agents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    deliveries: Mapped[list["Delivery"]] = relationship("Delivery", back_populates="agent")
    transactions: Mapped[list["EscrowTransaction"]] = relationship("EscrowTransaction", back_populates="assigned_agent")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), unique=True, nullable=False, index=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.BUYER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class AdminUser(Base):
    """Named admin with an individual API key, so actions are attributable and revocable."""
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, nullable=True, index=True)
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    # Defaults to SUPER_ADMIN so pre-RBAC admins are not locked out on upgrade.
    role: Mapped[str] = mapped_column(String(30), default=AdminRole.SUPER_ADMIN.value, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditLogEntry(Base):
    """Append-only record of a sensitive admin action. actor is a denormalized label so it outlives the AdminUser row."""
    __tablename__ = "audit_log_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    admin_user_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    target_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True)


class Seller(Base):
    __tablename__ = "sellers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    business_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, unique=True, index=True)
    payout_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # KYC gate: a seller needs VERIFIED before any payout. Verification is an admin action, never automatic.
    national_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    kyc_status: Mapped[SellerKycStatus] = mapped_column(Enum(SellerKycStatus), default=SellerKycStatus.UNVERIFIED, nullable=False)
    kyc_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    kyc_notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payout_channel: Mapped[SellerPayoutChannel] = mapped_column(Enum(SellerPayoutChannel), default=SellerPayoutChannel.MPESA_PHONE, nullable=False)
    payout_shortcode: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # till/paybill number; required if payout_channel != MPESA_PHONE
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    orders: Mapped[list["Order"]] = relationship("Order", back_populates="seller")


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    public_ref: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    buyer_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    seller_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("sellers.id"), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18,2), nullable=False)
    delivery_fee: Mapped[Decimal] = mapped_column(Numeric(18,2), default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KES", nullable=False)
    status: Mapped[OrderStatus] = mapped_column(Enum(OrderStatus), default=OrderStatus.CREATED, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivery_address: Mapped[str] = mapped_column(Text, nullable=False)
    flagged_for_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flag_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Client idempotency key, unique per buyer_phone: a retried POST /orders returns the original order.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    # SHA-256 of the normalized request body. Same key with a different fingerprint is a 409, not a silent replay.
    idempotency_fingerprint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    seller: Mapped[Optional[Seller]] = relationship("Seller", back_populates="orders")
    # No cascade deletes: an order with escrow or ledger history must never be hard-deleted.
    items: Mapped[list["OrderItem"]] = relationship("OrderItem", back_populates="order", passive_deletes=True)
    escrow: Mapped[Optional["EscrowTransaction"]] = relationship("EscrowTransaction", back_populates="order", uselist=False, passive_deletes=True, lazy="selectin")
    delivery: Mapped[Optional["Delivery"]] = relationship("Delivery", back_populates="order", uselist=False, passive_deletes=True)
    disputes: Mapped[list["Dispute"]] = relationship("Dispute", back_populates="order", passive_deletes=True)
    __table_args__ = (UniqueConstraint("buyer_phone","idempotency_key",name="uq_order_buyer_idempotency_key"),)


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18,2), nullable=False)
    order: Mapped[Order] = relationship("Order", back_populates="items")
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_order_item_qty"), CheckConstraint("unit_price >= 0", name="ck_order_item_price"))


class Delivery(Base):
    __tablename__ = "deliveries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, nullable=False)
    agent_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("delivery_agents.id"), nullable=True, index=True)
    status: Mapped[DeliveryStatus] = mapped_column(Enum(DeliveryStatus), default=DeliveryStatus.UNASSIGNED, nullable=False, index=True)
    picked_up_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    order: Mapped[Order] = relationship("Order", back_populates="delivery")
    agent: Mapped[Optional[DeliveryAgent]] = relationship("DeliveryAgent", back_populates="deliveries")


class LedgerAccount(Base):
    __tablename__ = "ledger_accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    account_type: Mapped[LedgerAccountType] = mapped_column(Enum(LedgerAccountType), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="KES", nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    entries: Mapped[list["LedgerEntry"]] = relationship("LedgerEntry", back_populates="account")


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    reference: Mapped[str] = mapped_column(String(120), unique=True, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    escrow_transaction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("escrow_transactions.id", ondelete="RESTRICT"), nullable=True, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    # Append-only: never cascade-delete entries when a transaction is removed.
    entries: Mapped[list["LedgerEntry"]] = relationship("LedgerEntry", back_populates="ledger_transaction", passive_deletes=True)


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    ledger_transaction_id: Mapped[int] = mapped_column(Integer, ForeignKey("ledger_transactions.id", ondelete="RESTRICT"), nullable=False, index=True)
    account_id: Mapped[int] = mapped_column(Integer, ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False, index=True)
    direction: Mapped[LedgerEntryDirection] = mapped_column(Enum(LedgerEntryDirection), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18,2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    ledger_transaction: Mapped[LedgerTransaction] = relationship("LedgerTransaction", back_populates="entries")
    account: Mapped[LedgerAccount] = relationship("LedgerAccount", back_populates="entries")
    __table_args__ = (CheckConstraint("amount > 0", name="ck_ledger_entry_positive_amount"),)


class EscrowTransaction(Base):
    __tablename__ = "escrow_transactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("orders.id", ondelete="RESTRICT"), unique=True, nullable=True, index=True)
    buyer_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    payee_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    payee_role: Mapped[PayeeRole] = mapped_column(Enum(PayeeRole), default=PayeeRole.RIDER, nullable=False)
    assigned_agent_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("delivery_agents.id"), nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18,2), nullable=False)
    status: Mapped[EscrowStatus] = mapped_column(Enum(EscrowStatus), default=EscrowStatus.PENDING_PAYMENT, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivery_provider: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    external_delivery_ref: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    buyer_access_token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    qr_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    qr_view_token: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, unique=True, index=True)
    qr_token_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    qr_token_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    mpesa_checkout_request_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True, index=True)
    mpesa_merchant_request_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    # "STK" or "C2B". Reversal only applies to the product that created the transaction. NULL on older rows.
    payment_method: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    mpesa_receipt_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, unique=True, index=True)
    stk_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18,2), nullable=True)
    stk_phone_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    stk_callback_received_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    stk_result_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    stk_result_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stk_last_queried_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    b2c_conversation_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    b2c_originator_conversation_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    b2c_transaction_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    b2c_result_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    b2c_result_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    expired_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    assigned_agent: Mapped[Optional[DeliveryAgent]] = relationship("DeliveryAgent", back_populates="transactions", lazy="selectin")
    order: Mapped[Optional[Order]] = relationship("Order", back_populates="escrow", lazy="selectin")
    __table_args__ = (CheckConstraint("amount > 0", name="ck_escrow_positive_amount"),)


class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    order_id: Mapped[int] = mapped_column(Integer, ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False, index=True)
    opened_by_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[DisputeStatus] = mapped_column(Enum(DisputeStatus), default=DisputeStatus.OPEN, nullable=False, index=True)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refund_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18,2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    order: Mapped[Order] = relationship("Order", back_populates="disputes")


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    channel: Mapped[NotificationChannel] = mapped_column(Enum(NotificationChannel), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[NotificationStatus] = mapped_column(Enum(NotificationStatus), default=NotificationStatus.PENDING, nullable=False)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Payout(Base):
    __tablename__ = "payouts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    escrow_transaction_id: Mapped[int] = mapped_column(Integer, ForeignKey("escrow_transactions.id", ondelete="RESTRICT"), nullable=False, index=True)
    purpose: Mapped[str] = mapped_column(String(30), nullable=False)  # RELEASE or REFUND
    recipient_phone: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18,2), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False, index=True)
    method: Mapped[str] = mapped_column(String(20), default="B2C", nullable=False)  # B2C, REVERSAL, or B2B
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    dead_lettered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    timed_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_stale_alert_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(100), unique=True, nullable=True, index=True)
    originator_conversation_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    transaction_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    result_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payout_positive_amount"),
        CheckConstraint("status IN ('PENDING','SUBMITTING','INITIATED','COMPLETED','FAILED','TIMEOUT')", name="ck_payout_status"),
        CheckConstraint("purpose IN ('RELEASE','REFUND')", name="ck_payout_purpose"),
        CheckConstraint("method IN ('B2C','REVERSAL','B2B')", name="ck_payout_method"),
        UniqueConstraint("escrow_transaction_id", "purpose", name="uq_payout_escrow_purpose"),
        # One receipt can settle only one payout. NULLs (not yet completed) do not collide.
        UniqueConstraint("transaction_id", name="uq_payout_transaction_id"),
    )


class UnmatchedC2BPayment(Base):
    """C2B payment Safaricom already settled that matched no pending order. A C2B confirmation cannot be rejected, so it is kept for manual reconciliation."""
    __tablename__ = "unmatched_c2b_payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    mpesa_receipt_number: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18,2), nullable=False)
    msisdn: Mapped[str] = mapped_column(String(20), nullable=False)
    bill_ref_number: Mapped[str] = mapped_column(String(50), nullable=False)
    transaction_time: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    matched_order_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("orders.id"), nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Finer-grained than `resolved`, which is kept in sync for older readers. UNMATCHED is the initial state.
    status: Mapped[str] = mapped_column(String(30), default="UNMATCHED", nullable=False)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class UnappliedPayment(Base):
    """Money Safaricom confirmed that could not be applied to an escrow. Held for an admin, never refunded automatically, booked to UNAPPLIED_PAYMENTS."""
    __tablename__ = "unapplied_payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source: Mapped[str] = mapped_column(String(10), nullable=False)  # STK or C2B
    provider_reference: Mapped[str] = mapped_column(String(150), nullable=False)  # CheckoutRequestID (STK) or receipt (C2B)
    mpesa_receipt_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True, index=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18,2), nullable=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    escrow_transaction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("escrow_transactions.id", ondelete="RESTRICT"), nullable=True, index=True)
    reason: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False, index=True)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("source", "provider_reference", name="uq_unapplied_source_ref"),
        CheckConstraint("source IN ('STK','C2B')", name="ck_unapplied_source"),
        CheckConstraint("status IN ('OPEN','APPLIED','REFUNDED_EXTERNALLY','WRITTEN_OFF')", name="ck_unapplied_status"),
    )


class AccountBalanceCheck(Base):
    """One Account Balance request. The balance arrives later on the result callback and is matched by conversation_id."""
    __tablename__ = "account_balance_checks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    conversation_id: Mapped[Optional[str]] = mapped_column(String(100), unique=True, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)  # PENDING, COMPLETED, FAILED, TIMEOUT
    result_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    working_account_balance: Mapped[Optional[Decimal]] = mapped_column(Numeric(18,2), nullable=True)
    raw_result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class TransactionStatusQuery(Base):
    """One TransactionStatusQuery round trip for a payout. Records the answer; never changes the payout's status itself."""
    __tablename__ = "transaction_status_queries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    payout_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("payouts.id", ondelete="RESTRICT"), nullable=True, index=True)
    # Denormalized from Payout.method so the query record stands alone for audit.
    payout_method: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # B2C, B2B, REVERSAL
    query_conversation_id: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    queried_transaction_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING", nullable=False)  # PENDING, ANSWERED, TIMEOUT
    result_code: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    result_desc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transaction_status: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    receipt_number: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18,2), nullable=True)
    raw_response: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class USSDSession(Base):
    __tablename__ = "ussd_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[str] = mapped_column(String(150), unique=True, nullable=False, index=True)
    phone_number: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    state: Mapped[str] = mapped_column(String(50), default="MAIN", nullable=False)
    order_ref: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)


class ReconciliationRecord(Base):
    __tablename__ = "reconciliation_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_reference: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    escrow_transaction_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("escrow_transactions.id", ondelete="RESTRICT"), nullable=True)
    amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(18,2), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    raw_payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    __table_args__ = (UniqueConstraint("provider", "provider_reference", name="uq_recon_provider_ref"),)


Index("ix_escrow_expiry_sweep", EscrowTransaction.status, EscrowTransaction.qr_token_expires_at)


SYSTEM_ACCOUNTS = {
    "MPESA_CLEARING": ("M-Pesa Clearing", LedgerAccountType.ASSET),
    "ESCROW_HOLDING": ("Customer Escrow Holding", LedgerAccountType.LIABILITY),
    "RIDER_PAYABLE": ("Rider Payable", LedgerAccountType.LIABILITY),
    "SELLER_PAYABLE": ("Seller Payable", LedgerAccountType.LIABILITY),
    "BUYER_REFUND_PAYABLE": ("Buyer Refund Payable", LedgerAccountType.LIABILITY),
    "UNAPPLIED_PAYMENTS": ("Unapplied Customer Payments", LedgerAccountType.LIABILITY),
}


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode()).hexdigest()


def generate_agent_api_key() -> str:
    return f"agnt_{secrets.token_urlsafe(32)}"


def generate_admin_api_key() -> str:
    return f"admn_{secrets.token_urlsafe(32)}"


def generate_public_ref(prefix="ORD"):
    return f"{prefix}-{secrets.token_hex(6).upper()}"


async def ensure_system_accounts(db: AsyncSession) -> None:
    for code, (name, account_type) in SYSTEM_ACCOUNTS.items():
        existing = await db.scalar(select(LedgerAccount).where(LedgerAccount.code == code))
        if not existing:
            db.add(LedgerAccount(code=code, name=name, account_type=account_type))
    await db.flush()


class LedgerOverdraft(RuntimeError):
    """A posting would take a guarded liability account below zero."""


# On by default. Tests that hand-build escrows without the payment hold entry switch it off in tests/conftest.py.
LEDGER_OVERDRAFT_GUARD = os.getenv("LEDGER_OVERDRAFT_GUARD", "true").lower() in {"1", "true", "yes"}

# Per-escrow balance (credits minus debits) of these accounts must never go negative.
_GUARDED_PER_ESCROW = ("ESCROW_HOLDING", "SELLER_PAYABLE", "RIDER_PAYABLE", "BUYER_REFUND_PAYABLE")


async def _account_balance(db: AsyncSession, account_code: str, escrow_transaction_id: Optional[int] = None) -> Decimal:
    """Credits minus debits on one account, optionally limited to one escrow."""
    account = await db.scalar(select(LedgerAccount).where(LedgerAccount.code == account_code))
    if account is None:
        return Decimal("0")

    def total(direction):
        query = select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.account_id == account.id, LedgerEntry.direction == direction
        )
        if escrow_transaction_id is None:
            return query
        return query.join(LedgerTransaction, LedgerTransaction.id == LedgerEntry.ledger_transaction_id).where(
            LedgerTransaction.escrow_transaction_id == escrow_transaction_id
        )

    credit = await db.scalar(total(LedgerEntryDirection.CREDIT))
    debit = await db.scalar(total(LedgerEntryDirection.DEBIT))
    return Decimal(str(credit)) - Decimal(str(debit))


async def _check_no_overdraft(db: AsyncSession, debit_account_code: str, escrow_transaction_id: Optional[int]) -> None:
    """Run after the new entries are flushed. Only the debited account can have gone negative."""
    if not LEDGER_OVERDRAFT_GUARD:
        return
    if debit_account_code in _GUARDED_PER_ESCROW and escrow_transaction_id is not None:
        balance = await _account_balance(db, debit_account_code, escrow_transaction_id)
        if balance < 0:
            raise LedgerOverdraft(
                f"{debit_account_code} would go negative for escrow #{escrow_transaction_id} "
                f"(balance {balance}); refusing the posting"
            )
        return
    if debit_account_code == "UNAPPLIED_PAYMENTS":
        balance = await _account_balance(db, "UNAPPLIED_PAYMENTS")
        if balance < 0:
            raise LedgerOverdraft(f"UNAPPLIED_PAYMENTS would go negative (balance {balance}); refusing the posting")


async def post_ledger_transaction(
    db: AsyncSession,
    *,
    reference: str,
    idempotency_key: str,
    description: str,
    debit_account_code: str,
    credit_account_code: str,
    amount: Decimal,
    escrow_transaction_id: Optional[int] = None,
) -> LedgerTransaction:
    amount = Decimal(str(amount))
    if amount <= 0:
        raise ValueError("Ledger amount must be positive")
    existing = await db.scalar(select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key))
    if existing:
        return existing

    await ensure_system_accounts(db)
    debit_account = await db.scalar(select(LedgerAccount).where(LedgerAccount.code == debit_account_code))
    credit_account = await db.scalar(select(LedgerAccount).where(LedgerAccount.code == credit_account_code))
    if not debit_account or not credit_account:
        raise RuntimeError("Ledger system account missing")

    posting = LedgerTransaction(
        reference=reference,
        description=description,
        idempotency_key=idempotency_key,
        escrow_transaction_id=escrow_transaction_id,
    )
    posting.entries = [
        LedgerEntry(account=debit_account, direction=LedgerEntryDirection.DEBIT, amount=amount),
        LedgerEntry(account=credit_account, direction=LedgerEntryDirection.CREDIT, amount=amount),
    ]
    db.add(posting)
    await db.flush()
    await _check_no_overdraft(db, debit_account_code, escrow_transaction_id)
    return posting


def _clean_amount(amount) -> Optional[Decimal]:
    """Positive two-decimal amount, or None if missing or unreadable."""
    if amount is None:
        return None
    try:
        cleaned = Decimal(str(amount)).quantize(Decimal("0.01"))
    except Exception:
        return None
    if not cleaned.is_finite() or cleaned <= 0:
        return None
    return cleaned


async def record_unapplied_payment(
    db: AsyncSession,
    *,
    source: str,
    provider_reference: str,
    reason: str,
    raw_payload: str,
    amount=None,
    phone_number: Optional[str] = None,
    mpesa_receipt_number: Optional[str] = None,
    escrow_transaction_id: Optional[int] = None,
) -> tuple["UnappliedPayment", bool]:
    """Record money Safaricom confirmed that cannot be applied to an escrow, and book it to UNAPPLIED_PAYMENTS.

    Idempotent per (source, provider_reference): a redelivery returns (existing, False). An unreadable amount
    still records the row, with the raw payload, but posts nothing. Flushes; the caller commits.
    """
    existing = await db.scalar(
        select(UnappliedPayment).where(
            UnappliedPayment.source == source, UnappliedPayment.provider_reference == provider_reference
        )
    )
    if existing:
        return existing, False

    clean_amount = _clean_amount(amount)
    row = UnappliedPayment(
        source=source,
        provider_reference=provider_reference,
        reason=reason,
        raw_payload=raw_payload,
        amount=clean_amount,
        phone_number=phone_number,
        mpesa_receipt_number=mpesa_receipt_number,
        escrow_transaction_id=escrow_transaction_id,
    )
    db.add(row)
    await db.flush()
    if clean_amount is not None:
        await post_ledger_transaction(
            db,
            reference=f"UNAPPLIED-{source}-{provider_reference}"[:120],
            idempotency_key=f"unapplied-{source.lower()}-{provider_reference}"[:150],
            description=f"Unapplied {source} payment ({reason})",
            debit_account_code="MPESA_CLEARING",
            credit_account_code="UNAPPLIED_PAYMENTS",
            amount=clean_amount,
            escrow_transaction_id=escrow_transaction_id,
        )
    return row, True


async def init_db() -> None:
    if os.getenv("DB_AUTO_CREATE", "true").lower() in {"1", "true", "yes"}:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as db:
        await ensure_system_accounts(db)
        await db.commit()
