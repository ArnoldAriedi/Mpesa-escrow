from dotenv import load_dotenv

load_dotenv()
import logging as _lg

_lg.getLogger("httpx").setLevel(_lg.WARNING)

import asyncio, hashlib, json, logging, os, secrets, time as _time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

import escrow_core as mpesa
from ops_alerts import alert_payout_issue
from notification_service import send_sms, retry_failed_notification, NOTIFICATION_MAX_TOTAL_ATTEMPTS
from escrow_models import (
    AccountBalanceCheck,
    AdminRole,
    AdminUser,
    AsyncSessionLocal,
    AuditLogEntry,
    DeliveryAgent,
    Delivery,
    DeliveryStatus,
    Dispute,
    DisputeStatus,
    EscrowStatus,
    EscrowTransaction,
    LedgerAccount,
    LedgerEntry,
    LedgerEntryDirection,
    LedgerTransaction,
    Notification,
    NotificationStatus,
    Order,
    OrderItem,
    OrderStatus,
    PayeeRole,
    Payout,
    ReconciliationRecord,
    Seller,
    SellerKycStatus,
    SellerPayoutChannel,
    TransactionStatusQuery,
    UnmatchedC2BPayment,
    USSDSession,
    User,
    UserRole,
    generate_admin_api_key,
    generate_agent_api_key,
    generate_public_ref,
    hash_api_key,
    init_db,
    post_ledger_transaction,
    record_unapplied_payment,
    UnappliedPayment,
)

_required_for_local_dev = ["ADMIN_API_KEY", "ESCROW_API_KEY"]
_missing = [k for k in _required_for_local_dev if not os.getenv(k)]
if _missing:
    import warnings
    warnings.warn(
        f".env values not loaded (or not set): {', '.join(_missing)}. Admin endpoints and payment auth will reject all requests."
    )

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
ESCROW_API_KEY = os.getenv("ESCROW_API_KEY")
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY")
QR_EXPIRY_CHECK_SECONDS = int(os.getenv("QR_EXPIRY_CHECK_SECONDS", "60"))
# most USSD gateways time out around this anyway
USSD_SESSION_TTL_SECONDS = int(os.getenv("USSD_SESSION_TTL_SECONDS", "180"))
# refunds inside this window use Reversal instead of a fresh B2C payment
MPESA_REVERSAL_WINDOW_HOURS = int(os.getenv("MPESA_REVERSAL_WINDOW_HOURS", "24"))
# KES; alert when the B2C working account balance drops below this
FLOAT_LOW_BALANCE_THRESHOLD = Decimal(os.getenv("FLOAT_LOW_BALANCE_THRESHOLD", "10000"))
# 6 hours; scheduled account-balance + stuck-payout reconciliation
RECONCILIATION_INTERVAL_SECONDS = int(os.getenv("RECONCILIATION_INTERVAL_SECONDS", "21600"))
# query Transaction Status for a payout INITIATED longer than this with no callback
PAYOUT_STALE_INITIATED_MINUTES = int(os.getenv("PAYOUT_STALE_INITIATED_MINUTES", "30"))
PAYOUT_MAX_ATTEMPTS = int(os.getenv("PAYOUT_MAX_ATTEMPTS", "5"))
ADMIN_RATE_LIMIT_MAX_REQUESTS = int(os.getenv("ADMIN_RATE_LIMIT_MAX_REQUESTS", "120"))
ADMIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("ADMIN_RATE_LIMIT_WINDOW_SECONDS", "60"))
ACCOUNT_BALANCE_STALE_MINUTES = int(os.getenv("ACCOUNT_BALANCE_STALE_MINUTES", "60"))
TRANSACTION_STATUS_QUERY_STALE_MINUTES = int(os.getenv("TRANSACTION_STATUS_QUERY_STALE_MINUTES", "60"))
STALE_TIMEOUT_REALERT_MINUTES = int(os.getenv("STALE_TIMEOUT_REALERT_MINUTES", "60"))
MPESA_CALLBACK_SECRET = os.getenv("MPESA_CALLBACK_SECRET")
DB_AUTO_CREATE = os.getenv("DB_AUTO_CREATE", "true").lower() in {"1", "true", "yes"}
BUYER_PORTAL_BASE_URL = os.getenv("BUYER_PORTAL_BASE_URL", "").rstrip("/")

_REQUIRED_MPESA_VARS = [
    "MPESA_CONSUMER_KEY",
    "MPESA_CONSUMER_SECRET",
    "MPESA_SHORTCODE",
    "MPESA_PASSKEY",
    "MPESA_CALLBACK_BASE_URL",
    "MPESA_INITIATOR_NAME",
    "MPESA_INITIATOR_PASSWORD",
    "MPESA_CALLBACK_SECRET"
]


def _now():
    return datetime.now(timezone.utc)


def _as_aware(dt):
    """Treat a naive datetime as UTC. SQLite drops tzinfo on round-trip, and comparing naive with aware raises
    TypeError.
    """

    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_whole_kes(amount: Decimal) -> int:
    amount = Decimal(str(amount))
    if amount != amount.to_integral_value():
        raise HTTPException(422, "Amount must be a whole number of KES.")
    return int(amount)


def _parse_c2b_amount(raw):
    """Decimal amount, or None if missing, unparseable, or not a finite number."""

    if raw is None:
        return None
    try:
        amt = Decimal(str(raw))
    except InvalidOperation:
        return None
    return amt if amt.is_finite() else None


def _require_callback_secret(header_secret: Optional[str]):
    """Authenticate callbacks by the X-Mpesa-Secret header only; a trusted proxy injects it. The URL never
    carries the secret.
    """

    if not MPESA_CALLBACK_SECRET:
        raise HTTPException(503, "M-Pesa callback authentication is not configured.")
    if not header_secret or not secrets.compare_digest(header_secret, MPESA_CALLBACK_SECRET):
        raise HTTPException(401, "Invalid callback secret.")


def _buyer_qr_url(tx: EscrowTransaction) -> Optional[str]:
    if not BUYER_PORTAL_BASE_URL or not tx.qr_view_token:
        return None
    return f"{BUYER_PORTAL_BASE_URL}/buyer/escrow/{tx.id}/qr?token={tx.qr_view_token}"


class RateLimiter:
    def __init__(self, max_requests, window):
        self.max_requests = max_requests
        self.window = window
        self.hits = defaultdict(deque)

    def check(self, key):
        now = _time.monotonic()
        h = self.hits[key]
        while h and now - h[0] > self.window:
            h.popleft()
        if len(h) >= self.max_requests:
            return False
        h.append(now)
        return True


limiter = RateLimiter(30, 60)


class VelocityGuard:
    """Flags, never blocks, a buyer with unusual recent order activity. In-memory and single-process, like
    RateLimiter.
    """

    def __init__(self, max_orders, max_total_amount, window):
        self.max_orders = max_orders
        self.max_total_amount = max_total_amount
        self.window = window
        self.hits = defaultdict(deque)  # key -> deque[(monotonic_time, amount)]

    def check(self, key, amount):
        now = _time.monotonic()
        h = self.hits[key]
        while h and now - h[0][0] > self.window:
            h.popleft()
        h.append((now, amount))
        count = len(h)
        total = sum((a for _, a in h))
        if count > self.max_orders or total > self.max_total_amount:
            return (False, count, total)
        return (True, count, total)


velocity_guard = VelocityGuard(max_orders=5, max_total_amount=Decimal("200000"), window=3600)

_api = APIKeyHeader(name="X-API-Key", auto_error=False)

_admin = APIKeyHeader(name="X-API-Key", auto_error=False)
_agent = APIKeyHeader(name="X-Agent-Api-Key", auto_error=False)
_buyer = APIKeyHeader(name="X-Buyer-Access-Token", auto_error=False)


async def verify_platform_key(k: Optional[str]=Depends(_api)):
    if not ESCROW_API_KEY or not k or not secrets.compare_digest(k, ESCROW_API_KEY):
        raise HTTPException(401, "Missing or invalid API key.")


async def _resolve_admin(k: Optional[str]) -> tuple[str, str]:
    """Return (actor_label, role): the one place that decides whose key this is."""

    if not k:
        raise HTTPException(401, "Missing admin API key.")
    if ADMIN_API_KEY and secrets.compare_digest(k, ADMIN_API_KEY):
        # Legacy shared key predates per-admin accounts; treat it as SUPER_ADMIN so it isn't locked out of
        # role-gated endpoints.
        return ("legacy-shared-admin-key", AdminRole.SUPER_ADMIN.value)
    async with AsyncSessionLocal() as db:
        admin = await db.scalar(select(AdminUser).where(AdminUser.api_key_hash == hash_api_key(k)))
        if admin and admin.is_active:
            admin.last_used_at = _now()
            await db.commit()
            return (f"{admin.name} (admin_user_id={admin.id})", admin.role)
    raise HTTPException(401, "Missing or invalid admin API key.")


async def verify_admin_key(k: Optional[str]=Depends(_admin)) -> str:
    """Return an actor label for audit logging. Accepts the legacy shared ADMIN_API_KEY or a per-admin key from
    /admin/admins/register.
    """

    actor, _role = await _resolve_admin(k)
    return actor


def require_admin_role(*roles: str):
    """Dependency factory: SUPER_ADMIN always passes, plus any of `roles`. With no roles, SUPER_ADMIN only."""

    async def _dep(k: Optional[str]=Depends(_admin)) -> str:
        actor, role = await _resolve_admin(k)
        if role != AdminRole.SUPER_ADMIN.value and role not in roles:
            allowed = ", ".join([AdminRole.SUPER_ADMIN.value, *roles])
            raise HTTPException(403, f"This action requires one of: {allowed}.")
        return actor

    return _dep


async def record_audit(
    db,
    actor: str,
    action: str,
    target_type: Optional[str]=None,
    target_id=None,
    detail: Optional[str]=None
):
    """Append an AuditLogEntry to the caller's transaction, so a rolled-back action leaves no audit entry."""

    admin_user_id = None
    if "admin_user_id=" in actor:
        try:
            admin_user_id = int(actor.rsplit("admin_user_id=", 1)[1].rstrip(")"))
        except ValueError:
            pass
    db.add(
        AuditLogEntry(
            actor=actor,
            admin_user_id=admin_user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail
        )
    )


async def get_authenticated_agent(k: Optional[str]=Depends(_agent)):
    if not k:
        raise HTTPException(401, "Missing agent API key.")
    async with AsyncSessionLocal() as db:
        agent = await db.scalar(select(DeliveryAgent).where(DeliveryAgent.api_key_hash == hash_api_key(k)))
        if not agent or not agent.is_active:
            raise HTTPException(401, "Invalid or inactive agent credentials.")
        return agent


async def rate_limit(request: Request):
    key = request.client.host if request.client else "unknown"
    if not limiter.check(key):
        raise HTTPException(429, "Too many requests.")


class RegisterAgentRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    phone_number: str


class RegisterSellerRequest(BaseModel):
    business_name: str = Field(min_length=1, max_length=255)
    phone_number: str
    payout_phone: str
    national_id: Optional[str] = Field(default=None, max_length=50)


class OrderItemSchema(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    quantity: int = Field(gt=0, le=100000)
    unit_price: Decimal = Field(ge=0, decimal_places=2, max_digits=18)


class CreateOrderRequest(BaseModel):
    buyer_phone: str
    seller_id: Optional[int] = None
    amount: Decimal = Field(gt=0, decimal_places=2, max_digits=18)
    delivery_fee: Decimal = Field(default=0, ge=0, decimal_places=2, max_digits=18)
    delivery_address: str = Field(min_length=3, max_length=1000)
    description: Optional[str] = Field(default=None, max_length=500)
    assigned_agent_id: Optional[int] = None
    items: list[OrderItemSchema] = Field(default_factory=list)


class ConfirmDeliveryRequest(BaseModel):
    qr_token: str


class DeliveryStateRequest(BaseModel):
    note: Optional[str] = None


class DisputeRequest(BaseModel):
    order_id: int
    reason: str = Field(min_length=3, max_length=2000)
    evidence: Optional[str] = Field(default=None, max_length=10000)


class ResolveDisputeRequest(BaseModel):
    decision: str
    refund_amount: Optional[Decimal] = Field(default=None, gt=0)
    note: Optional[str] = None


class USSDRequest(BaseModel):
    sessionId: str
    phoneNumber: str
    serviceCode: str = "*334#"
    text: str = ""


async def create_payout(
    db,
    tx,
    *,
    purpose,
    recipient_phone,
    amount,
    debit_account,
    credit_account,
    method="B2C"
):
    amount = Decimal(str(amount))
    _to_whole_kes(amount)
    if purpose not in {"RELEASE", "REFUND"}:
        raise ValueError("Unsupported payout purpose")
    if method not in {"B2C", "REVERSAL", "B2B"}:
        raise ValueError("Unsupported payout method")
    payout = Payout(
        escrow_transaction_id=tx.id,
        purpose=purpose,
        recipient_phone=recipient_phone,
        amount=amount,
        status="PENDING",
        method=method
    )
    db.add(payout)
    await db.flush()
    await post_ledger_transaction(
        db,
        reference=f"{purpose}-{tx.id}-{payout.id}",
        idempotency_key=f"{purpose.lower()}-reserve-{tx.id}-{payout.id}",
        description=f"Reserve {purpose.lower()} for escrow #{tx.id}",
        escrow_transaction_id=tx.id,
        debit_account_code=debit_account,
        credit_account_code=credit_account,
        amount=amount
    )
    return payout


def _select_refund_method(tx) -> str:
    """REVERSAL while the original STK receipt is inside Safaricom's reversal window, otherwise B2C.

    Gated on payment_method == "STK": STK and C2B fill the receipt fields identically, and a reversal against
    a C2B payment isn't appropriate. NULL (older rows) falls back to B2C.
    """

    if tx.payment_method == "STK" and tx.mpesa_receipt_number and tx.stk_callback_received_at:
        if _now() - _as_aware(tx.stk_callback_received_at) <= timedelta(hours=MPESA_REVERSAL_WINDOW_HOURS):
            return "REVERSAL"
    return "B2C"


async def _select_release_method_and_recipient(db, tx):
    """Return (method, recipient) for a RELEASE payout. B2B only for a verified seller with a registered
    till/paybill; everything else is B2C.
    """

    if tx.payee_role == PayeeRole.SELLER and tx.order and tx.order.seller_id:
        seller = await db.get(Seller, tx.order.seller_id)
        if seller and seller.payout_channel != SellerPayoutChannel.MPESA_PHONE and seller.payout_shortcode:
            return ("B2B", seller.payout_shortcode)
    return ("B2C", tx.payee_phone)


async def _park_payout_as_timeout(
    db,
    payout_id: int,
    reason: str,
    conversation_id: Optional[str]=None
) -> bool:
    """Park a payout with an unknown outcome as TIMEOUT.

    For any failure after the reservation committed that isn't a confirmed rejection: Safaricom may already
    have the request, so it must never become auto-retryable. Never raises; if this write fails the payout
    stays SUBMITTING and the reconciliation sweep converts it.
    """

    try:
        await db.rollback()
        payout = await db.scalar(
            select(Payout)
            .where(Payout.id == payout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if payout is None or payout.status != "SUBMITTING":
            return False
        payout.status = "TIMEOUT"
        payout.timed_out_at = _now()
        payout.result_desc = reason
        if conversation_id:
            payout.conversation_id = conversation_id
        await db.commit()
        return True
    except Exception:
        logging.exception(
            "Could not park payout %s as TIMEOUT; it stays SUBMITTING for the reconciliation sweep",
            payout_id
        )
        return False


async def initiate_payout_for_record(db, payout, http_client):
    tx = await db.get(EscrowTransaction, payout.escrow_transaction_id)
    if not tx:
        raise RuntimeError("Escrow transaction not found")
    if payout.status in {"COMPLETED", "INITIATED", "TIMEOUT"}:
        return payout.status == "INITIATED"
    if payout.status not in {"PENDING", "FAILED"}:
        return False
    _to_whole_kes(payout.amount)
    payable = (
        "BUYER_REFUND_PAYABLE"
        if payout.purpose == "REFUND"
        else "RIDER_PAYABLE"
        if tx.payee_role == PayeeRole.RIDER
        else "SELLER_PAYABLE"
    )
    payout_id = payout.id
    tx_id = tx.id
    amount = payout.amount
    purpose = payout.purpose.lower()
    payout.attempt_count += 1
    payout.last_attempt_at = _now()
    attempt = payout.attempt_count
    # Fresh OriginatorConversationID per attempt, persisted before the call.
    outbound_originator_id = f"escrow-payout-{payout_id}-{attempt}"
    payout.originator_conversation_id = outbound_originator_id
    # Claim as SUBMITTING in the same commit as the reservation so no other worker picks it up.
    payout.status = "SUBMITTING"

    # Phase 1: reserve. Nothing is sent yet, so FAILED is safe on error.
    try:
        await post_ledger_transaction(
            db,
            reference=f"MPESA-SETTLE-{payout_id}-{attempt}",
            idempotency_key=f"mpesa-settle-{payout_id}-{attempt}",
            description=f"Reserve {purpose} for external M-Pesa settlement attempt {attempt}",
            escrow_transaction_id=tx_id,
            debit_account_code=payable,
            credit_account_code="MPESA_CLEARING",
            amount=amount
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        failed = await db.scalar(
            select(Payout)
            .where(Payout.id == payout_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if failed is None:
            raise
        failed.status = "FAILED"
        failed.result_desc = f"Internal error before the request reached Safaricom: {exc}"
        await db.commit()
        await alert_payout_issue(
            "PAYOUT_FAILED",
            payout_id,
            tx_id,
            f"Internal error before the request reached Safaricom: {exc}"
        )
        return False

    # Phase 2: the request may be on its way. FAILED only after a confirmed rejection whose reversal also
    # posted; anything else is TIMEOUT.
    conversation_id = None
    rejection = None
    try:
        try:
            if payout.method == "REVERSAL":
                reply = await mpesa.initiate_reversal(
                    http_client,
                    transaction_id=tx.mpesa_receipt_number,
                    amount=_to_whole_kes(amount),
                    remarks=f"Escrow {purpose} #{tx_id}",
                    originator_conversation_id=outbound_originator_id,
                    occasion="Escrow"
                )
            elif payout.method == "B2B":
                # Revalidate the seller right before sending.
                b2b_seller = (
                    await db.get(Seller, tx.order.seller_id) if tx.order
                    and tx.order.seller_id else None
                )
                if (
                    not b2b_seller
                    or not b2b_seller.is_active
                    or b2b_seller.kyc_status != SellerKycStatus.VERIFIED
                    or (b2b_seller.payout_channel == SellerPayoutChannel.MPESA_PHONE)
                    or not b2b_seller.payout_shortcode
                ):
                    raise mpesa.MpesaRejected(
                        f"Seller #{(tx.order.seller_id if tx.order else '?')} is no longer eligible for B2B payout (inactive/unverified/channel or shortcode changed); needs admin review before retrying."
                    )
                reply = await mpesa.initiate_b2b_payout(
                    http_client,
                    receiver_shortcode=b2b_seller.payout_shortcode,
                    receiver_channel=b2b_seller.payout_channel.value,
                    amount=_to_whole_kes(amount),
                    remarks=f"Escrow {purpose} #{tx_id}",
                    originator_conversation_id=outbound_originator_id,
                    account_reference=tx.order.public_ref if tx.order else str(tx_id)
                )
            else:
                reply = await mpesa.initiate_b2c_payout(
                    http_client,
                    phone_number=payout.recipient_phone,
                    amount=_to_whole_kes(amount),
                    remarks=f"Escrow {purpose} #{tx_id}",
                    originator_conversation_id=outbound_originator_id,
                    occasion="Escrow"
                )
        except (httpx.HTTPStatusError, mpesa.MpesaRejected) as exc:
            # Confirmed rejection (4xx, or 200 with a non-zero ResponseCode). HTTP 5xx is MpesaOutcomeUnknown
            # and is not caught here.
            rejection = exc
        if rejection is not None:
            await post_ledger_transaction(
                db,
                reference=f"MPESA-SETTLE-REVERSE-{payout_id}-{attempt}",
                idempotency_key=f"mpesa-settle-reverse-{payout_id}-{attempt}",
                description="Reverse provider-rejected external settlement reservation",
                escrow_transaction_id=tx_id,
                debit_account_code="MPESA_CLEARING",
                credit_account_code=payable,
                amount=amount
            )
            payout.status = "FAILED"
            payout.result_desc = str(rejection)
            await db.commit()
        else:
            conversation_id = reply.get("ConversationID")
            if not conversation_id:
                raise RuntimeError(f"M-Pesa {payout.method} response did not return ConversationID")
            payout.conversation_id = conversation_id
            payout.status = "INITIATED"
            await db.commit()
    except Exception as exc:
        # Unknown outcome: MpesaOutcomeUnknown, transport errors, a failed reversal posting, or a failed
        # commit after sending.
        await _park_payout_as_timeout(db, payout_id, f"Outcome unknown: {exc}", conversation_id)
        await alert_payout_issue(
            "PAYOUT_TIMEOUT",
            payout_id,
            tx_id,
            f"Unknown outcome for the {purpose} request (needs manual reconciliation): {exc}"
        )
        return False
    if rejection is not None:
        await alert_payout_issue(
            "PAYOUT_FAILED",
            payout_id,
            tx_id,
            f"Provider rejected the {purpose}: {rejection}"
        )
        return False
    return True


async def notify_order_events(db, order, agent=None):
    """Queue order notifications in the caller's session. After a financial commit use
    notify_order_events_after_commit().
    """

    await send_sms(
        db,
        order.buyer_phone,
        f"Order {order.public_ref}: payment and escrow processing started. Delivery address: {order.delivery_address}",
        f"order-created-{order.id}"
    )
    if agent:
        await send_sms(
            db,
            agent.phone_number,
            f"New escrow delivery {order.public_ref}. Order value KES {order.amount + order.delivery_fee} (held in escrow for the seller). Deliver the order and scan the buyer QR after delivery.",
            f"order-agent-{order.id}"
        )


async def notify_order_events_after_commit(order_id: int, agent_id: Optional[int]=None):
    """Run notification persistence in a fresh session after financial commit."""

    async with AsyncSessionLocal() as notification_db:
        order = await notification_db.get(Order, order_id)
        if not order:
            return
        agent = await notification_db.get(DeliveryAgent, agent_id) if agent_id else None
        await notify_order_events(notification_db, order, agent)
        await notification_db.commit()


async def notify_seller_escrow_held_after_commit(escrow_id: int):
    """Tell the seller the order is paid and funds are held. Never includes the QR token or link."""

    async with AsyncSessionLocal() as notification_db:
        tx = await notification_db.get(
            EscrowTransaction,
            escrow_id,
            options=[selectinload(EscrowTransaction.order)]
        )
        if not tx or not tx.order or not tx.order.seller_id:
            return
        seller = await notification_db.get(Seller, tx.order.seller_id)
        if not seller:
            return
        ref = tx.order.public_ref
        await send_sms(
            notification_db,
            seller.phone_number,
            f"Order {ref}: buyer payment of KES {tx.amount} received and held in escrow. Funds will be released to you once delivery is confirmed.",
            f"escrow-held-seller-{tx.id}"
        )
        await notification_db.commit()


async def notify_delivery_confirmed_hold_after_commit(escrow_id: int):
    """Tell the buyer delivery was confirmed but payout is on hold for admin review."""

    async with AsyncSessionLocal() as notification_db:
        tx = await notification_db.get(
            EscrowTransaction,
            escrow_id,
            options=[selectinload(EscrowTransaction.order)]
        )
        if not tx:
            return
        ref = tx.order.public_ref if tx.order else tx.id
        await send_sms(
            notification_db,
            tx.buyer_phone,
            f"Order {ref}: delivery confirmed. Your payment release is under admin review and you'll be updated shortly.",
            f"delivery-confirmed-hold-buyer-{tx.id}"
        )
        await notification_db.commit()


async def notify_delivery_confirmed_after_commit(escrow_id: int):
    """Tell the buyer their delivery was confirmed and payout is in progress."""

    async with AsyncSessionLocal() as notification_db:
        tx = await notification_db.get(
            EscrowTransaction,
            escrow_id,
            options=[selectinload(EscrowTransaction.order), selectinload(EscrowTransaction.assigned_agent)]
        )
        if not tx:
            return
        ref = tx.order.public_ref if tx.order else tx.id
        await send_sms(
            notification_db,
            tx.buyer_phone,
            f"Order {ref}: delivery confirmed. Your funds are being released from escrow.",
            f"delivery-confirmed-buyer-{tx.id}"
        )
        if tx.assigned_agent:
            await send_sms(
                notification_db,
                tx.assigned_agent.phone_number,
                f"Order {ref}: QR scan confirmed. The buyer's payment has been approved and is being released to the seller.",
                f"delivery-confirmed-rider-{tx.id}"
            )
        await notification_db.commit()


async def notify_rider_escrow_finalized_after_commit(escrow_id: int):
    """Tell the rider once the seller has actually received the funds. Distinct from the scan-time message, which
    only means the payout was approved.
    """

    async with AsyncSessionLocal() as notification_db:
        tx = await notification_db.get(
            EscrowTransaction,
            escrow_id,
            options=[selectinload(EscrowTransaction.order)]
        )
        if not tx or not tx.assigned_agent_id or tx.status != EscrowStatus.RELEASED:
            return
        agent = await notification_db.get(DeliveryAgent, tx.assigned_agent_id)
        if not agent:
            return
        ref = tx.order.public_ref if tx.order else tx.id
        await send_sms(
            notification_db,
            agent.phone_number,
            f"Order {ref}: the seller has received their payment. Delivery complete.",
            f"escrow-finalized-rider-{tx.id}"
        )
        await notification_db.commit()


async def notify_dispute_resolved_after_commit(dispute_id: int, escrow_id: int):
    """Tell the buyer and payee how an admin resolved their dispute."""

    async with AsyncSessionLocal() as notification_db:
        dispute = await notification_db.get(Dispute, dispute_id)
        tx = await notification_db.get(
            EscrowTransaction,
            escrow_id,
            options=[selectinload(EscrowTransaction.order)]
        )
        if not dispute or not tx:
            return
        ref = tx.order.public_ref if tx.order else tx.id
        if dispute.status == DisputeStatus.RESOLVED_REFUND:
            summary = f"Order {ref}: your dispute was resolved with a full refund."
        elif dispute.status == DisputeStatus.RESOLVED_RELEASE:
            summary = f"Order {ref}: your dispute was resolved and the delivery payment was released."
        elif dispute.status == DisputeStatus.RESOLVED_PARTIAL:
            summary = f"Order {ref}: your dispute was resolved with a partial refund of KES {dispute.refund_amount}."
        else:
            summary = f"Order {ref}: your dispute has been reviewed."
        await send_sms(notification_db, tx.buyer_phone, summary, f"dispute-resolved-buyer-{dispute.id}")
        if tx.payee_phone and tx.payee_phone != tx.buyer_phone:
            await send_sms(
                notification_db,
                tx.payee_phone,
                f"Order {ref}: dispute resolved by admin. Check your payout status.",
                f"dispute-resolved-payee-{dispute.id}"
            )
        await notification_db.commit()


async def notify_seller_escrow_finalized_after_commit(escrow_id: int):
    """Tell the seller how the transaction ended: paid out, refunded or split. Never includes the QR token or
    link.
    """

    async with AsyncSessionLocal() as notification_db:
        tx = await notification_db.get(
            EscrowTransaction,
            escrow_id,
            options=[selectinload(EscrowTransaction.order)]
        )
        if not tx or not tx.order or not tx.order.seller_id:
            return
        if tx.status not in {EscrowStatus.RELEASED, EscrowStatus.REFUNDED, EscrowStatus.PARTIALLY_SETTLED}:
            return
        seller = await notification_db.get(Seller, tx.order.seller_id)
        if not seller:
            return
        ref = tx.order.public_ref
        is_seller_payee = tx.payee_role == PayeeRole.SELLER
        if tx.status == EscrowStatus.RELEASED:
            if is_seller_payee:
                message = f"Order {ref}: delivery confirmed. KES {tx.amount} has been released to your payout account."
            else:
                message = f"Order {ref}: delivery confirmed and the transaction is complete."
            key = f"escrow-finalized-seller-released-{tx.id}"
        elif tx.status == EscrowStatus.REFUNDED:
            message = f"Order {ref}: the buyer's payment of KES {tx.amount} was refunded and the order was cancelled. No payment is due to you."
            key = f"escrow-finalized-seller-refunded-{tx.id}"
        else:  # PARTIALLY_SETTLED
            payouts = (
                (
                    await notification_db.execute(
                        select(Payout).where(
                            Payout.escrow_transaction_id == tx.id,
                            Payout.status == "COMPLETED"
                        )
                    )
                )
                .scalars()
                .all()
            )
            released = sum((p.amount for p in payouts if p.purpose == "RELEASE"), Decimal("0.00"))
            refunded = sum((p.amount for p in payouts if p.purpose == "REFUND"), Decimal("0.00"))
            if is_seller_payee:
                message = f"Order {ref}: dispute settled. KES {released} released to your payout account, KES {refunded} refunded to the buyer."
            else:
                message = f"Order {ref}: dispute settled with a partial refund of KES {refunded} to the buyer."
            key = f"escrow-finalized-seller-partial-{tx.id}"
        await send_sms(notification_db, seller.phone_number, message, key)
        await notification_db.commit()


async def notify_escrow_finalized_after_commit(escrow_id: int):
    """Tell the buyer once an escrow reaches a terminal REFUNDED/RELEASED/PARTIALLY_SETTLED state."""

    async with AsyncSessionLocal() as notification_db:
        tx = await notification_db.get(
            EscrowTransaction,
            escrow_id,
            options=[selectinload(EscrowTransaction.order)]
        )
        if not tx:
            return
        ref = tx.order.public_ref if tx.order else tx.id
        if tx.status == EscrowStatus.REFUNDED:
            message = f"Order {ref}: your refund of KES {tx.amount} has been completed."
            key = f"refund-completed-buyer-{tx.id}"
        elif tx.status == EscrowStatus.RELEASED:
            message = f"Order {ref}: payment has been released. Thank you for using our escrow service."
            key = f"release-completed-buyer-{tx.id}"
        elif tx.status == EscrowStatus.PARTIALLY_SETTLED:
            message = f"Order {ref}: your dispute settlement has been completed."
            key = f"partial-settled-buyer-{tx.id}"
        else:
            return
        await send_sms(notification_db, tx.buyer_phone, message, key)
        await notification_db.commit()


async def claim_expired_transaction(db, tx_id: int, now: Optional[datetime]=None) -> bool:
    """Atomically claim an expired escrow for refund. The conditional UPDATE is the final idempotency guard, safe
    with several workers or a database that ignores row locks (SQLite).
    """

    now = now or _now()
    result = await db.execute(
        update(EscrowTransaction)
        .where(
            EscrowTransaction.id == tx_id,
            EscrowTransaction.status == EscrowStatus.ESCROW_HELD,
            EscrowTransaction.qr_token_expires_at.is_not(None),
            EscrowTransaction.qr_token_expires_at <= now
        )
        .values(status=EscrowStatus.EXPIRED, expired_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


# Once out for delivery the goods may already be with the buyer. Add PICKED_UP for a stricter rule.
_HANDOVER_STARTED = {DeliveryStatus.OUT_FOR_DELIVERY, DeliveryStatus.DELIVERED}


async def _delivery_may_have_been_handed_over(db, tx) -> bool:
    if tx.order_id is None:
        return False
    delivery = await db.scalar(select(Delivery).where(Delivery.order_id == tx.order_id))
    return delivery is not None and delivery.status in _HANDOVER_STARTED


async def expire_one(db, tx, http_client):
    if tx.status != EscrowStatus.EXPIRED:
        return
    existing = await db.scalar(
        select(Payout).where(Payout.escrow_transaction_id == tx.id, Payout.purpose == "REFUND")
    )
    if not existing and await _delivery_may_have_been_handed_over(db, tx):
        # No automatic refund: a buyer must not keep the goods and withhold the QR. An admin settles it
        # through the dispute flow.
        tx.status = EscrowStatus.DISPUTED
        if tx.order:
            tx.order.status = OrderStatus.DISPUTED
        db.add(
            Dispute(
                order_id=tx.order_id,
                opened_by_phone=tx.buyer_phone,
                reason="Automatically opened: the delivery QR expired while the order was already out for delivery. The refund was NOT sent automatically; an admin must confirm with the rider and the buyer whether the goods were delivered."
            )
        )
        return
    if not existing:
        tx.status = EscrowStatus.REFUND_INITIATED
        payout = await create_payout(
            db,
            tx,
            purpose="REFUND",
            recipient_phone=tx.buyer_phone,
            amount=tx.amount,
            debit_account="ESCROW_HOLDING",
            credit_account="BUYER_REFUND_PAYABLE",
            method=_select_refund_method(tx)
        )
        await initiate_payout_for_record(db, payout, http_client)
        if payout.status == "FAILED":
            tx.status = EscrowStatus.REFUND_FAILED
    elif existing.status in {"FAILED"}:
        tx.status = EscrowStatus.REFUND_FAILED
    elif existing.status in {"PENDING", "SUBMITTING", "INITIATED"}:
        # SUBMITTING is in-flight like PENDING/INITIATED; don't leave tx.status stuck on EXPIRED while a
        # refund is running.
        tx.status = EscrowStatus.REFUND_INITIATED
    if tx.order:
        if tx.status == EscrowStatus.REFUNDED:
            tx.order.status = OrderStatus.REFUNDED
        elif tx.status == EscrowStatus.REFUND_FAILED:
            tx.order.status = OrderStatus.REFUND_FAILED
        else:
            tx.order.status = OrderStatus.REFUND_PENDING


# minimum gap between automatic retries of one payout
PAYOUT_RETRY_BACKOFF_SECONDS = int(os.getenv("PAYOUT_RETRY_BACKOFF_SECONDS", "300"))


async def retry_failed_payouts(db, client):
    """Automatic retry of FAILED payouts.

    initiate_payout_for_record commits on entry, releasing any row lock, so each payout is re-selected, locked
    and re-checked individually. The lock is released by the move to SUBMITTING; a payout claimed elsewhere is
    skipped.
    """

    from sqlalchemy import or_
    # End the caller's transaction so no earlier work or lock is left dangling.
    await db.commit()
    cutoff = _now() - timedelta(seconds=PAYOUT_RETRY_BACKOFF_SECONDS)
    candidate_ids = (
        (
            await db.execute(
                select(Payout.id)
                .where(
                    Payout.status == "FAILED",
                    Payout.attempt_count < PAYOUT_MAX_ATTEMPTS,
                    or_(Payout.last_attempt_at.is_(None), Payout.last_attempt_at <= cutoff)
                )
                .order_by(Payout.id)
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    await db.commit()
    for payout_id in candidate_ids:
        payout = await db.scalar(
            select(Payout)
            .where(
                Payout.id == payout_id,
                Payout.status == "FAILED",
                Payout.attempt_count < PAYOUT_MAX_ATTEMPTS
            )
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if payout is None:
            # ends the empty transaction; commit (unlike rollback) does not expire the caller's objects
            await db.commit()
            continue
        try:
            await initiate_payout_for_record(db, payout, client)
        except Exception:
            logging.exception("Automatic retry of payout %s failed unexpectedly", payout_id)
            await db.rollback()
    dead = (
        (
            await db.execute(
                select(Payout)
                .where(
                    Payout.status == "FAILED",
                    Payout.attempt_count >= PAYOUT_MAX_ATTEMPTS,
                    Payout.dead_lettered_at.is_(None)
                )
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for payout in dead:
        payout.dead_lettered_at = _now()
        await alert_payout_issue(
            "PAYOUT_DEAD_LETTER",
            payout.id,
            payout.escrow_transaction_id,
            f"payout permanently FAILED after {payout.attempt_count} attempts; needs manual reconciliation"
        )
    if dead:
        await db.commit()
    else:
        await db.commit()


async def retry_failed_notifications(db):
    """Dead-letter retry for FAILED notifications, the same treatment payouts get."""

    stmt = (
        select(Notification)
        .where(
            Notification.status == NotificationStatus.FAILED,
            Notification.attempts < NOTIFICATION_MAX_TOTAL_ATTEMPTS
        )
        .order_by(Notification.id)
        .limit(20)
        .with_for_update(skip_locked=True)
    )
    rows = (await db.execute(stmt)).scalars().all()
    for notification in rows:
        succeeded = await retry_failed_notification(db, notification)
        if not succeeded and notification.attempts >= NOTIFICATION_MAX_TOTAL_ATTEMPTS:
            # Dead-letter point: further automatic retries are pointless and an admin must look.
            await alert_payout_issue(
                "NOTIFICATION_DEAD_LETTER",
                0,
                0,
                f"notification_id={notification.id} to {mpesa.mask_phone(notification.recipient_phone)} permanently failed after {notification.attempts} attempts: {notification.error}"
            )


EXPIRY_BATCH_SIZE = int(os.getenv("EXPIRY_BATCH_SIZE", "50"))
# minutes between scheduled status queries per payout
PAYOUT_STATUS_QUERY_COOLDOWN_MINUTES = int(os.getenv("PAYOUT_STATUS_QUERY_COOLDOWN_MINUTES", "1440"))
_expiry_error_alerted: set = set()


async def _expire_one_escrow_by_id(tx_id: int, client):
    """Expire and refund one escrow in its own transaction, so one failure can't block the rest."""

    async with AsyncSessionLocal() as db:
        tx = await db.scalar(
            select(EscrowTransaction).where(EscrowTransaction.id == tx_id).with_for_update(skip_locked=True)
        )
        if tx is None:
            return
        if not await claim_expired_transaction(db, tx.id):
            await db.commit()
            return
        await db.flush()
        await db.refresh(tx)
        await expire_one(db, tx, client)
        held_for_review = tx.status == EscrowStatus.DISPUTED
        await db.commit()
    if held_for_review:
        await alert_payout_issue(
            "EXPIRY_HELD_FOR_REVIEW",
            0,
            tx_id,
            f"The delivery QR for escrow #{tx_id} expired while the order was already out for delivery. The refund was NOT sent automatically: a dispute was opened for an admin to resolve."
        )


async def expiry_loop(client):
    while True:
        try:
            async with AsyncSessionLocal() as db:
                expired_ids = (
                    (
                        await db.execute(
                            select(EscrowTransaction.id)
                            .where(
                                EscrowTransaction.status == EscrowStatus.ESCROW_HELD,
                                EscrowTransaction.qr_token_expires_at.is_not(None),
                                EscrowTransaction.qr_token_expires_at < _now()
                            )
                            .order_by(EscrowTransaction.id)
                            .limit(EXPIRY_BATCH_SIZE)
                        )
                    )
                    .scalars()
                    .all()
                )
            for tx_id in expired_ids:
                try:
                    await _expire_one_escrow_by_id(tx_id, client)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logging.exception(
                        "Expiry of escrow %s failed; continuing with the others: %s",
                        tx_id,
                        exc
                    )
                    if tx_id not in _expiry_error_alerted:
                        _expiry_error_alerted.add(tx_id)
                        await alert_payout_issue(
                            "EXPIRY_REFUND_ERROR",
                            0,
                            tx_id,
                            f"Automatic expiry/refund failed and is retried on every sweep until fixed: {exc}"
                        )
            try:
                async with AsyncSessionLocal() as db:
                    await retry_failed_payouts(db, client)
                    await retry_failed_notifications(db)
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.exception("Automatic payout/notification retries failed: %s", exc)
            # Sleep inside the cancellation boundary so task.cancel() on shutdown is caught and re-raised
            # cleanly.
            await asyncio.sleep(QR_EXPIRY_CHECK_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception("expiry sweep failed: %s", e)
            await asyncio.sleep(QR_EXPIRY_CHECK_SECONDS)


# younger than this: the callback may still be on its way
STK_RECOVERY_MIN_AGE_SECONDS = int(os.getenv("STK_RECOVERY_MIN_AGE_SECONDS", "180"))
# older than this: stop asking, leave it for an admin
STK_RECOVERY_MAX_AGE_HOURS = int(os.getenv("STK_RECOVERY_MAX_AGE_HOURS", "24"))
# minimum gap between queries about one escrow
STK_RECOVERY_COOLDOWN_SECONDS = int(os.getenv("STK_RECOVERY_COOLDOWN_SECONDS", "600"))
STK_RECOVERY_INTERVAL_SECONDS = int(os.getenv("STK_RECOVERY_INTERVAL_SECONDS", "60"))
STK_RECOVERY_BATCH_SIZE = int(os.getenv("STK_RECOVERY_BATCH_SIZE", "20"))


async def _recover_one_stk(tx_id: int, client):
    """Ask Safaricom about one escrow whose STK callback never arrived: claim (lock, re-check, stamp), ask (no
    locks held), apply (lock, re-check). A failed or unreadable answer changes nothing.
    """

    from sqlalchemy import or_
    now = _now()
    async with AsyncSessionLocal() as db:
        tx = await db.scalar(
            select(EscrowTransaction).where(EscrowTransaction.id == tx_id).with_for_update(skip_locked=True)
        )
        if tx is None or tx.status != EscrowStatus.PENDING_PAYMENT or not tx.mpesa_checkout_request_id:
            return
        if (
            tx.stk_last_queried_at is not None
            and (now - _as_aware(tx.stk_last_queried_at)).total_seconds() < STK_RECOVERY_COOLDOWN_SECONDS
        ):
            return
        checkout_id = tx.mpesa_checkout_request_id
        tx.stk_last_queried_at = now
        await db.commit()
    try:
        answer = await mpesa.query_stk_status(client, checkout_id)
    except Exception as exc:
        logging.warning("STK status query for escrow %s failed (treated as unknown): %s", tx_id, exc)
        return
    outcome = answer.get("outcome")
    if outcome not in {"PAID", "FAILED"}:
        return
    async with AsyncSessionLocal() as db:
        tx = await db.scalar(select(EscrowTransaction).where(EscrowTransaction.id == tx_id).with_for_update())
        if tx is None or tx.status != EscrowStatus.PENDING_PAYMENT:
            return  # a real callback landed meanwhile
        if outcome == "PAID":
            # The query answer has no receipt, amount or phone, so park it for an admin (resolve-stk-unknown)
            # instead of applying it.
            tx.status = EscrowStatus.STK_INITIATION_UNKNOWN
            tx.stk_result_code = answer.get("result_code")
            tx.stk_result_desc = f"Recovered by status query: {answer.get('result_desc')}"
            await db.commit()
            await alert_payout_issue(
                "STK_PAID_NO_CALLBACK",
                0,
                tx_id,
                f"Safaricom says STK checkout {checkout_id} was PAID but its callback never arrived. Escrow parked as STK_INITIATION_UNKNOWN: finish it with /admin/escrow/{tx_id}/resolve-stk-unknown using the receipt from the Safaricom portal."
            )
        else:
            tx.status = EscrowStatus.PAYMENT_FAILED
            tx.stk_result_code = answer.get("result_code")
            tx.stk_result_desc = f"Recovered by status query: {answer.get('result_desc')}"
            if tx.order:
                tx.order.status = OrderStatus.CANCELLED
            await db.commit()


async def stk_recovery_sweep(client):
    """One pass over stale PENDING_PAYMENT escrows."""

    now = _now()
    async with AsyncSessionLocal() as db:
        ids = (
            (
                await db.execute(
                    select(EscrowTransaction.id)
                    .where(
                        EscrowTransaction.status == EscrowStatus.PENDING_PAYMENT,
                        EscrowTransaction.mpesa_checkout_request_id.is_not(None),
                        EscrowTransaction.created_at < now - timedelta(seconds=STK_RECOVERY_MIN_AGE_SECONDS),
                        EscrowTransaction.created_at > now - timedelta(hours=STK_RECOVERY_MAX_AGE_HOURS)
                    )
                    .order_by(EscrowTransaction.id)
                    .limit(STK_RECOVERY_BATCH_SIZE)
                )
            )
            .scalars()
            .all()
        )
    for tx_id in ids:
        try:
            await _recover_one_stk(tx_id, client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.exception("STK recovery for escrow %s failed; continuing with the others: %s", tx_id, exc)


async def stk_recovery_loop(client):
    while True:
        try:
            await stk_recovery_sweep(client)
            await asyncio.sleep(STK_RECOVERY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception("stk recovery sweep failed: %s", e)
            await asyncio.sleep(STK_RECOVERY_INTERVAL_SECONDS)


async def reconciliation_loop(client):
    """Scheduled reconciliation: check the B2C float and query Safaricom about payouts INITIATED too long without
    a callback.

    Runs far less often than expiry_loop because it calls metered Safaricom APIs. Admin endpoints cover the
    on-demand case.
    """

    while True:
        try:
            originator_conversation_id = f"scheduled-balance-{secrets.token_hex(8)}"
            async with AsyncSessionLocal() as db:
                check = AccountBalanceCheck(conversation_id=None, status="PENDING")
                db.add(check)
                await db.flush()
                try:
                    balance_reply = await mpesa.query_account_balance(client, originator_conversation_id)
                    check.conversation_id = balance_reply.get("ConversationID")
                except Exception as exc:
                    check.status = "FAILED"
                    check.result_desc = str(exc)
                await db.commit()

            stale_before = _now() - timedelta(minutes=PAYOUT_STALE_INITIATED_MINUTES)
            async with AsyncSessionLocal() as db:
                # SKIP LOCKED so two instances don't both query the same stale payout. Includes SUBMITTING:
                # the process crashed after the request may have reached Safaricom.
                recent_query = (
                    select(TransactionStatusQuery.id)
                    .where(
                        TransactionStatusQuery.payout_id == Payout.id,
                        TransactionStatusQuery.created_at > _now() - timedelta(
                            minutes=PAYOUT_STATUS_QUERY_COOLDOWN_MINUTES
                        )
                    )
                    .exists()
                )
                stale = (
                    (
                        await db.execute(
                            select(Payout)
                            .where(
                                Payout.status.in_(["INITIATED", "SUBMITTING"]),
                                Payout.last_attempt_at < stale_before,
                                ~recent_query
                            )
                            .order_by(Payout.id)
                            .limit(10)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                # One transaction for the whole batch: committing per row would release the locks on the other
                # rows.
                alerts = []
                for payout in stale:
                    if not payout.transaction_id and not payout.conversation_id:
                        # SUBMITTING with no IDs: nothing to query with. Treat it like a queue-timeout
                        # (TIMEOUT, admin reconciles); never auto-retry, Safaricom may have received it.
                        payout.status = "TIMEOUT"
                        payout.timed_out_at = _now()
                        payout.result_desc = (
                            payout.result_desc or ""
                        ) + " | Stuck in SUBMITTING with no provider reference ever recorded (process likely crashed before the request completed or before Safaricom responded); needs manual reconciliation."
                        tx = await db.get(EscrowTransaction, payout.escrow_transaction_id)
                        if tx:
                            tx.status = (
                                EscrowStatus.REFUND_INITIATED
                                if payout.purpose == "REFUND"
                                else EscrowStatus.PAYOUT_FAILED
                            )
                        alerts.append(payout.id)
                        continue
                    query_conversation_id = f"scheduled-status-{payout.id}-{secrets.token_hex(6)}"
                    db.add(
                        TransactionStatusQuery(
                            payout_id=payout.id,
                            payout_method=payout.method,
                            query_conversation_id=query_conversation_id,
                            queried_transaction_id=payout.transaction_id or payout.conversation_id,
                            status="PENDING"
                        )
                    )
                    try:
                        await mpesa.query_transaction_status(
                            client,
                            transaction_id=payout.transaction_id or payout.conversation_id,
                            originator_conversation_id=query_conversation_id,
                            occasion=f"Scheduled reconciliation for payout #{payout.id}"
                        )
                    except Exception as exc:
                        logging.warning(
                            "Scheduled transaction status query failed for payout %s: %s",
                            payout.id,
                            exc
                        )
                await db.commit()
            for payout_id in alerts:
                await alert_payout_issue(
                    "PAYOUT_TIMEOUT",
                    payout_id,
                    0,
                    "Payout was stuck in SUBMITTING with no transaction/conversation ID; auto-marked TIMEOUT by the scheduled reconciliation sweep and needs manual reconciliation."
                )

            # A balance check or status query whose callback never arrives would sit at PENDING forever.
            # TIMEOUT means we never heard back; FAILED means Safaricom said no.
            async with AsyncSessionLocal() as db:
                balance_stale_before = _now() - timedelta(minutes=ACCOUNT_BALANCE_STALE_MINUTES)
                stale_balance_checks = (
                    (
                        await db.execute(
                            select(AccountBalanceCheck)
                            .where(
                                AccountBalanceCheck.status == "PENDING",
                                AccountBalanceCheck.created_at < balance_stale_before
                            )
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for bc in stale_balance_checks:
                    bc.status = "TIMEOUT"
                    bc.result_desc = "No callback (result or queue-timeout) ever received"
                    bc.completed_at = _now()
                query_stale_before = _now() - timedelta(minutes=TRANSACTION_STATUS_QUERY_STALE_MINUTES)
                stale_status_queries = (
                    (
                        await db.execute(
                            select(TransactionStatusQuery)
                            .where(
                                TransactionStatusQuery.status == "PENDING",
                                TransactionStatusQuery.created_at < query_stale_before
                            )
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                for sq in stale_status_queries:
                    sq.status = "TIMEOUT"
                    sq.result_desc = "No callback (result or queue-timeout) ever received"
                    sq.answered_at = _now()
                if stale_balance_checks or stale_status_queries:
                    await db.commit()

            realert_before = _now() - timedelta(minutes=STALE_TIMEOUT_REALERT_MINUTES)
            async with AsyncSessionLocal() as db:
                stale_timeouts = (
                    (
                        await db.execute(
                            select(Payout)
                            .where(
                                Payout.status == "TIMEOUT",
                                func.coalesce(
                                    Payout.last_stale_alert_at,
                                    Payout.timed_out_at
                                ) < realert_before
                            )
                            .order_by(Payout.id)
                            .limit(20)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .all()
                )
                realerted = [payout.id for payout in stale_timeouts]
                for payout in stale_timeouts:
                    payout.last_stale_alert_at = _now()
                if stale_timeouts:
                    await db.commit()
            for payout_id in realerted:
                await alert_payout_issue(
                    "PAYOUT_TIMEOUT_STILL_UNRESOLVED",
                    payout_id,
                    0,
                    f"Payout has been stuck in TIMEOUT for over {STALE_TIMEOUT_REALERT_MINUTES} minutes since the last alert and still needs manual reconciliation."
                )

            await asyncio.sleep(RECONCILIATION_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.exception("reconciliation sweep failed: %s", e)
            await asyncio.sleep(RECONCILIATION_INTERVAL_SECONDS)


def _validate_production_config(db_auto_create: bool, database_url: str, cors_origins: str) -> None:
    """Raise RuntimeError if the app is misconfigured for production (DB_AUTO_CREATE=false): requires Postgres
    and an explicit CORS allowlist instead of '*'.
    """

    if db_auto_create:
        return
    if not database_url.startswith("postgresql"):
        raise RuntimeError("Production mode requires PostgreSQL and Alembic migrations.")
    if cors_origins == "*":
        raise RuntimeError(
            "Production mode (DB_AUTO_CREATE=false) requires CORS_ALLOWED_ORIGINS to be set to an explicit comma-separated allowlist of origins; wildcard '*' is not permitted."
        )


_MIN_SECRET_LENGTH = 32


def _validate_production_secrets(db_auto_create: bool, secrets_by_name: dict) -> None:
    """In production mode, refuse to start with missing, short or reused secrets. ESCROW_API_KEY and
    ADMIN_API_KEY share the X-API-Key header, so a shared value would let the platform key act as admin.
    """

    if db_auto_create:
        return
    problems = []
    for name, value in secrets_by_name.items():
        if not value:
            problems.append(f"{name} is not set")
        elif len(value) < _MIN_SECRET_LENGTH:
            problems.append(f"{name} is shorter than {_MIN_SECRET_LENGTH} characters")
    seen = {}
    for name, value in secrets_by_name.items():
        if value:
            if value in seen:
                problems.append(f"{name} must differ from {seen[value]}")
            else:
                seen[value] = name
    if problems:
        raise RuntimeError(
            "Production mode (DB_AUTO_CREATE=false) refuses to start: " + "; ".join(problems) + "."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    missing = [v for v in _REQUIRED_MPESA_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(f"Missing required M-Pesa environment variable(s): {', '.join(missing)}")
    if not ESCROW_API_KEY or not ADMIN_API_KEY:
        raise RuntimeError("ESCROW_API_KEY and ADMIN_API_KEY must be set.")
    _validate_production_config(
        DB_AUTO_CREATE,
        os.getenv("DATABASE_URL", ""),
        os.getenv("CORS_ALLOWED_ORIGINS", "*")
    )
    _validate_production_secrets(
        DB_AUTO_CREATE,
        {
            "ESCROW_API_KEY": ESCROW_API_KEY,
            "ADMIN_API_KEY": ADMIN_API_KEY,
            "MPESA_CALLBACK_SECRET": MPESA_CALLBACK_SECRET,
            "USSD_CALLBACK_SECRET": mpesa.USSD_CALLBACK_SECRET
        }
    )
    await init_db()
    app.state.http_client = httpx.AsyncClient(timeout=float(os.getenv("MPESA_HTTP_TIMEOUT_SECONDS", "30")))
    task = asyncio.create_task(expiry_loop(app.state.http_client))
    reconciliation_task = asyncio.create_task(reconciliation_loop(app.state.http_client))
    stk_recovery_task = asyncio.create_task(stk_recovery_loop(app.state.http_client))
    yield
    task.cancel()
    reconciliation_task.cancel()
    stk_recovery_task.cancel()
    for t in (task, reconciliation_task, stk_recovery_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    await app.state.http_client.aclose()


# API docs hand unauthenticated callers a map of every endpoint. They follow DB_AUTO_CREATE=false (production
# mode); EXPOSE_API_DOCS=true opts back in.
_EXPOSE_DOCS = DB_AUTO_CREATE or os.getenv("EXPOSE_API_DOCS", "false").lower() in {"1", "true", "yes"}
app = FastAPI(
    title="Escrow Delivery Platform",
    version="2.0",
    lifespan=lifespan,
    docs_url="/docs" if _EXPOSE_DOCS else None,
    redoc_url="/redoc" if _EXPOSE_DOCS else None,
    openapi_url="/openapi.json" if _EXPOSE_DOCS else None
)
origins = os.getenv("CORS_ALLOWED_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if origins == "*" else [x.strip() for x in origins.split(",")],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"]
)

# Throttle every /admin/* route in one place. In-process and per-instance only; IP allowlisting and a gateway
# belong in front.
admin_limiter = RateLimiter(ADMIN_RATE_LIMIT_MAX_REQUESTS, ADMIN_RATE_LIMIT_WINDOW_SECONDS)


@app.middleware("http")
async def _admin_rate_limit_middleware(request: Request, call_next):
    if request.url.path.startswith("/admin/"):
        key = request.client.host if request.client else "unknown"
        if not admin_limiter.check(key):
            return JSONResponse(status_code=429, content={"detail": "Too many admin requests."})
    return await call_next(request)


@app.get("/")
async def root():
    return {"service": "escrow-delivery-platform", "version": "2.0", "status": "ok"}


@app.get("/health")
async def health():
    """Unauthenticated liveness probe with no business data. Only a failed DB round-trip reports unhealthy."""

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(select(1))
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "unavailable"})


@app.post("/agents/register", status_code=201, dependencies=[Depends(verify_admin_key)])
async def register_agent(p: RegisterAgentRequest):
    phone = mpesa.normalize_phone_number(p.phone_number)
    if not phone:
        raise HTTPException(422, "Invalid phone number")
    raw = generate_agent_api_key()
    async with AsyncSessionLocal() as db:
        agent = DeliveryAgent(name=p.name, phone_number=phone, api_key_hash=hash_api_key(raw))
        db.add(agent)
        try:
            await db.commit()
            await db.refresh(agent)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "Agent phone already exists")
    return {"agent_id": agent.id, "name": agent.name, "phone_number": agent.phone_number, "api_key": raw}


class RegisterAdminRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    email: Optional[str] = Field(default=None, max_length=255)
    role: AdminRole = Field(default=AdminRole.OPERATIONS_ADMIN)


@app.post("/admin/admins/register", status_code=201)
async def register_admin(p: RegisterAdminRequest, admin_actor: str=Depends(require_admin_role())):
    # SUPER_ADMIN only: a compromised admin must not be able to create another admin identity.
    raw_key = generate_admin_api_key()
    async with AsyncSessionLocal() as db:
        admin = AdminUser(name=p.name, email=p.email, role=p.role.value, api_key_hash=hash_api_key(raw_key))
        db.add(admin)
        await db.flush()
        await record_audit(
            db,
            admin_actor,
            "admin.register",
            target_type="AdminUser",
            target_id=admin.id,
            detail=f"Registered new admin: {p.name} role={p.role.value}"
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "Admin email already exists")
    return {
        "admin_id": admin.id,
        "name": admin.name,
        "role": admin.role,
        "api_key": raw_key,
        "warning": "Store this key now — it is only shown once and cannot be retrieved again."
    }


@app.post("/admin/admins/{admin_id}/deactivate")
async def deactivate_admin(admin_id: int, admin_actor: str=Depends(require_admin_role())):
    # SUPER_ADMIN only. The last active SUPER_ADMIN cannot be deactivated or every role-gated endpoint locks
    # permanently.
    async with AsyncSessionLocal() as db:
        admin = await db.scalar(select(AdminUser).where(AdminUser.id == admin_id).with_for_update())
        if not admin:
            raise HTTPException(404, "Admin not found")
        if admin.role == AdminRole.SUPER_ADMIN.value and admin.is_active:
            other_active_super_admins = await db.scalar(
                select(func.count())
                .select_from(AdminUser)
                .where(
                    AdminUser.role == AdminRole.SUPER_ADMIN.value,
                    AdminUser.is_active == True,
                    AdminUser.id != admin.id
                )
            )
            if not other_active_super_admins:
                raise HTTPException(409, "Cannot deactivate the last active SUPER_ADMIN.")
        admin.is_active = False
        await record_audit(
            db,
            admin_actor,
            "admin.deactivate",
            target_type="AdminUser",
            target_id=admin.id,
            detail=f"Deactivated admin: {admin.name}"
        )
        await db.commit()
    return {"admin_id": admin.id, "is_active": admin.is_active}


@app.get("/admin/audit-log")
async def list_audit_log(
    admin_actor: str=Depends(verify_admin_key),
    limit: int=Query(default=50, ge=1, le=200),
    action: Optional[str]=None
):
    async with AsyncSessionLocal() as db:
        stmt = select(AuditLogEntry).order_by(AuditLogEntry.id.desc()).limit(limit)
        if action:
            stmt = stmt.where(AuditLogEntry.action == action)
        rows = (await db.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "actor": r.actor,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "detail": r.detail,
                "created_at": r.created_at.isoformat()
            } for r in rows
        ]


@app.post("/sellers/register", status_code=201, dependencies=[Depends(verify_admin_key)])
async def register_seller(p: RegisterSellerRequest):
    phone = mpesa.normalize_phone_number(p.phone_number)
    payout = mpesa.normalize_phone_number(p.payout_phone)
    if not phone or not payout:
        raise HTTPException(422, "Invalid seller phone")
    async with AsyncSessionLocal() as db:
        # KYC always starts UNVERIFIED: registration is not identity verification. See
        # /admin/sellers/{id}/kyc.
        seller = Seller(
            business_name=p.business_name,
            phone_number=phone,
            payout_phone=payout,
            national_id=p.national_id,
            kyc_status=SellerKycStatus.PENDING if p.national_id else SellerKycStatus.UNVERIFIED
        )
        db.add(seller)
        try:
            await db.commit()
            await db.refresh(seller)
        except IntegrityError:
            await db.rollback()
            raise HTTPException(409, "Seller phone already exists")
    return {
        "seller_id": seller.id,
        "business_name": seller.business_name,
        "kyc_status": seller.kyc_status.value
    }


class SellerKycRequest(BaseModel):
    status: SellerKycStatus

    notes: Optional[str] = Field(default=None, max_length=1000)


@app.post("/admin/sellers/{seller_id}/kyc")
async def set_seller_kyc(
    seller_id: int,
    p: SellerKycRequest,
    admin_actor: str=Depends(require_admin_role(AdminRole.KYC_ADMIN.value))
):
    """Record the outcome of an out-of-band identity check. Verifies nothing itself; payout eligibility (see
    confirm_delivery) reads this.
    """

    async with AsyncSessionLocal() as db:
        seller = await db.get(Seller, seller_id)
        if not seller:
            raise HTTPException(404, "Seller not found")
        seller.kyc_status = p.status
        seller.kyc_notes = p.notes
        seller.kyc_verified_at = _now() if p.status == SellerKycStatus.VERIFIED else None
        await record_audit(
            db,
            admin_actor,
            "seller.kyc.set",
            target_type="Seller",
            target_id=seller.id,
            detail=f"kyc_status={p.status.value} notes={p.notes!r}"
        )
        await db.commit()
        return {
            "seller_id": seller.id,
            "kyc_status": seller.kyc_status.value,
            "kyc_verified_at": seller.kyc_verified_at.isoformat() if seller.kyc_verified_at else None
        }


class SellerPayoutChannelRequest(BaseModel):
    channel: SellerPayoutChannel

    shortcode: Optional[str] = Field(default=None, max_length=20)


@app.post("/admin/sellers/{seller_id}/payout-channel")
async def set_seller_payout_channel(
    seller_id: int,
    p: SellerPayoutChannelRequest,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    """Set how a seller's RELEASE payout is delivered: B2C to their phone (default) or B2B to their registered
    Till/Paybill.
    """

    if p.channel != SellerPayoutChannel.MPESA_PHONE and not p.shortcode:
        raise HTTPException(422, "shortcode is required for TILL/PAYBILL payout channels")
    async with AsyncSessionLocal() as db:
        seller = await db.get(Seller, seller_id)
        if not seller:
            raise HTTPException(404, "Seller not found")
        seller.payout_channel = p.channel
        seller.payout_shortcode = p.shortcode if p.channel != SellerPayoutChannel.MPESA_PHONE else None
        await record_audit(
            db,
            admin_actor,
            "seller.payout_channel.set",
            target_type="Seller",
            target_id=seller.id,
            detail=f"channel={p.channel.value} shortcode={p.shortcode!r}"
        )
        await db.commit()
        return {
            "seller_id": seller.id,
            "payout_channel": seller.payout_channel.value,
            "payout_shortcode": seller.payout_shortcode
        }


def _idempotency_fingerprint(p: "CreateOrderRequest") -> str:
    """SHA-256 of the normalized order body, so the same Idempotency-Key only replays when it is the same
    request.
    """

    payload = {
        "seller_id": p.seller_id,
        "amount": str(p.amount),
        "delivery_fee": str(p.delivery_fee),
        "delivery_address": p.delivery_address,
        "description": p.description,
        "assigned_agent_id": p.assigned_agent_id,
        "items": [[i.name, i.quantity, str(i.unit_price)] for i in p.items]
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@app.post("/orders", status_code=201, dependencies=[Depends(rate_limit), Depends(verify_platform_key)])
async def create_order(
    p: CreateOrderRequest,
    request: Request,
    idempotency_key: Optional[str]=Header(None, alias="Idempotency-Key")
):
    buyer = mpesa.normalize_phone_number(p.buyer_phone)
    if not buyer:
        raise HTTPException(422, "Invalid buyer phone")
    total = p.amount + p.delivery_fee
    if total != total.quantize(Decimal("1")):
        raise HTTPException(422, "The total M-Pesa charge must be a whole number of KES.")
    if p.items:
        item_subtotal = sum((item.unit_price * item.quantity for item in p.items), Decimal("0.00"))
        if item_subtotal != p.amount:
            raise HTTPException(
                422,
                "amount must equal the sum of order items; delivery_fee is added separately."
            )
    async with AsyncSessionLocal() as db:
        fingerprint = _idempotency_fingerprint(p) if idempotency_key else None
        if idempotency_key:
            # A client retry must return the same order and STK push, not prompt the buyer to pay twice.
            existing_order = await db.scalar(
                select(Order).where(Order.buyer_phone == buyer, Order.idempotency_key == idempotency_key)
            )
            if existing_order:
                # Same key with a different body is a client bug, not a replay.
                if (
                    existing_order.idempotency_fingerprint
                    and existing_order.idempotency_fingerprint != fingerprint
                ):
                    raise HTTPException(
                        409,
                        "This Idempotency-Key was already used for a different order request (different amount/seller/items). Use a new key for a genuinely new order."
                    )
                if existing_order.escrow:
                    tx = existing_order.escrow
                    return {
                        "order_id": existing_order.id,
                        "order_ref": existing_order.public_ref,
                        "escrow_id": tx.id,
                        "status": tx.status.value,
                        "buyer_access_token": tx.buyer_access_token,
                        "amount": str(tx.amount),
                        "message": "This order already exists (idempotency key matched); returning the original result rather than creating a new one."
                    }
        agent = None
        if p.assigned_agent_id:
            agent = await db.get(DeliveryAgent, p.assigned_agent_id)
            if not agent or not agent.is_active:
                raise HTTPException(404, "Active delivery agent not found")
        seller = await db.get(Seller, p.seller_id) if p.seller_id else None
        if p.seller_id and not seller:
            raise HTTPException(404, "Seller not found")
        if seller and not seller.is_active:
            raise HTTPException(403, "Seller account is deactivated and cannot receive new orders")
        if not seller and not agent:
            # Release needs a real, verifiable payee. An older fallback paid the buyer their own money as a
            # "seller" payout and was removed.
            raise HTTPException(
                422,
                "Order must specify seller_id or assigned_agent_id so escrow release has a valid payee."
            )
        total_for_velocity = p.amount + p.delivery_fee
        velocity_ok, recent_count, recent_total = velocity_guard.check(buyer, total_for_velocity)
        order = Order(
            public_ref=generate_public_ref(),
            buyer_phone=buyer,
            seller_id=p.seller_id,
            amount=p.amount,
            delivery_fee=p.delivery_fee,
            description=p.description,
            delivery_address=p.delivery_address,
            status=OrderStatus.PAYMENT_PENDING,
            idempotency_key=idempotency_key,
            idempotency_fingerprint=fingerprint
        )
        if not velocity_ok:
            # Flagged, not blocked: a busy legitimate buyer shouldn't be locked out. An admin reviews flagged
            # orders.
            order.flagged_for_review = True
            order.flag_reason = f"Velocity: {recent_count} orders / KES {recent_total} from this buyer in the last hour"
        for item in p.items:
            order.items.append(OrderItem(name=item.name, quantity=item.quantity, unit_price=item.unit_price))
        db.add(order)
        try:
            await db.flush()
        except IntegrityError:
            # Concurrent requests with the same Idempotency-Key can both pass the pre-check. The unique
            # constraint stops the second order; return the winner's order instead of a 500.
            await db.rollback()
            if idempotency_key:
                existing_order = await db.scalar(
                    select(Order).where(Order.buyer_phone == buyer, Order.idempotency_key == idempotency_key)
                )
                if (
                    existing_order
                    and existing_order.idempotency_fingerprint
                    and (existing_order.idempotency_fingerprint != fingerprint)
                ):
                    raise HTTPException(
                        409,
                        "This Idempotency-Key was already used for a different order request (different amount/seller/items). Use a new key for a genuinely new order."
                    )
                if existing_order and existing_order.escrow:
                    tx = existing_order.escrow
                    return {
                        "order_id": existing_order.id,
                        "order_ref": existing_order.public_ref,
                        "escrow_id": tx.id,
                        "status": tx.status.value,
                        "buyer_access_token": tx.buyer_access_token,
                        "amount": str(tx.amount),
                        "message": "This order already exists (idempotency key matched); returning the original result rather than creating a new one."
                    }
            raise HTTPException(409, "A duplicate order request was detected. Please retry.")
        db.add(
            Delivery(
                order_id=order.id,
                agent_id=agent.id if agent else None,
                status=DeliveryStatus.ASSIGNED if agent else DeliveryStatus.UNASSIGNED
            )
        )
        # The seller is the payee whenever the order has one; the rider only scans and confirms. RIDER payee
        # is a legacy fallback for orders without a seller_id.
        if seller:
            payee_phone = seller.payout_phone
            payee_role = PayeeRole.SELLER
        else:
            payee_phone = agent.phone_number
            payee_role = PayeeRole.RIDER
        tx = EscrowTransaction(
            order_id=order.id,
            buyer_phone=buyer,
            payee_phone=payee_phone,
            payee_role=payee_role,
            assigned_agent_id=agent.id if agent else None,
            amount=total,
            status=EscrowStatus.PENDING_PAYMENT,
            description=p.description,
            buyer_access_token=mpesa.generate_qr_token()
        )
        db.add(tx)
        await db.commit()
        await db.refresh(tx)
        await db.refresh(order)
        try:
            stk = await mpesa.initiate_stk_push(
                request.app.state.http_client,
                buyer,
                _to_whole_kes(total),
                f"ESC{tx.id}",
                (p.description or f"Order {order.public_ref}")[:100]
            )
            tx.mpesa_checkout_request_id = stk.get("CheckoutRequestID")
            tx.mpesa_merchant_request_id = stk.get("MerchantRequestID")
            if not tx.mpesa_checkout_request_id:
                raise RuntimeError("M-Pesa did not return a CheckoutRequestID")
            await db.commit()
        except (httpx.HTTPStatusError, mpesa.MpesaRejected) as exc:
            # Confirmed rejection: safe to tell the buyer the payment was not initiated.
            tx.status = EscrowStatus.PAYMENT_FAILED
            order.status = OrderStatus.CANCELLED
            await db.commit()
            raise HTTPException(502, f"M-Pesa rejected the payment request: {exc}")
        except Exception as exc:
            # Unknown outcome (e.g. timeout after sending): a PIN prompt may still arrive. Not PAYMENT_FAILED,
            # or a retry plus the original succeeding would double-charge.
            tx.status = EscrowStatus.STK_INITIATION_UNKNOWN
            await db.commit()
            await alert_payout_issue(
                "STK_INITIATION_UNKNOWN",
                0,
                tx.id,
                f"order_id={order.id} buyer={mpesa.mask_phone(buyer)} amount={total}: {exc}"
            )
            raise HTTPException(
                503,
                "We could not confirm whether the M-Pesa payment request reached Safaricom. Please wait a few minutes and check the order status before trying again — do not pay for this order twice."
            )
        order_id = order.id
        agent_id = agent.id if agent else None
        order_flagged = order.flagged_for_review
        order_flag_reason = order.flag_reason
    # Commit financial state before notifications; a fresh session keeps the helpers off stale rows.
    await notify_order_events_after_commit(order_id, agent_id)
    if order_flagged:
        await alert_payout_issue(
            "ORDER_FLAGGED_FOR_REVIEW",
            0,
            tx.id,
            f"order_id={order_id} buyer={mpesa.mask_phone(buyer)} {order_flag_reason}"
        )
    return {
        "order_id": order_id,
        "order_ref": order.public_ref,
        "escrow_id": tx.id,
        "status": tx.status.value,
        "buyer_access_token": tx.buyer_access_token,
        "amount": str(total),
        "message": "M-Pesa STK Push sent. Complete the PIN prompt on the buyer's phone."
    }


@app.get("/admin/escrow/stk-unknown", dependencies=[Depends(verify_admin_key)])
async def list_stk_unknown(limit: int=Query(default=50, ge=1, le=200)):
    """Escrows in STK_INITIATION_UNKNOWN. A human checks Safaricom's records before crediting or writing off.
    """

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(EscrowTransaction)
                    .where(EscrowTransaction.status == EscrowStatus.STK_INITIATION_UNKNOWN)
                    .order_by(EscrowTransaction.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "escrow_id": t.id,
                "order_id": t.order_id,
                "order_ref": t.order.public_ref if t.order else None,
                "buyer_phone": t.buyer_phone,
                "amount": str(t.amount)
            } for t in rows
        ]


class ResolveStkUnknownRequest(BaseModel):
    outcome: str  # "PAID" or "NOT_PAID"
    mpesa_receipt_number: Optional[str] = Field(default=None, max_length=100)
    paid_amount: Optional[Decimal] = Field(default=None, gt=0, decimal_places=2, max_digits=18)
    payer_phone: Optional[str] = Field(default=None, max_length=20)
    transaction_time: Optional[str] = Field(default=None, max_length=50)
    note: Optional[str] = Field(default=None, max_length=1000)


@app.post("/admin/escrow/{escrow_id}/resolve-stk-unknown")
async def resolve_stk_unknown(
    escrow_id: int,
    p: ResolveStkUnknownRequest,
    admin_actor: str=Depends(verify_admin_key)
):
    """Resolve an ambiguous STK initiation after an admin checked Safaricom's records.

    PAID needs the real receipt AND the actual paid amount, so a mistyped receipt can't stamp the order's
    amount onto it; the escrow is then held as a successful callback would hold it. NOT_PAID confirms no
    charge and releases the order.
    """

    outcome = p.outcome.upper()
    if outcome not in {"PAID", "NOT_PAID"}:
        raise HTTPException(422, "outcome must be PAID or NOT_PAID")
    async with AsyncSessionLocal() as db:
        tx = await db.scalar(
            select(EscrowTransaction).where(EscrowTransaction.id == escrow_id).with_for_update()
        )
        if not tx:
            raise HTTPException(404, "Escrow transaction not found")
        if tx.status != EscrowStatus.STK_INITIATION_UNKNOWN:
            raise HTTPException(409, "Escrow is not in STK_INITIATION_UNKNOWN state")
        if outcome == "PAID":
            if not p.mpesa_receipt_number:
                raise HTTPException(422, "mpesa_receipt_number is required when outcome=PAID")
            if p.paid_amount is None:
                raise HTTPException(
                    422,
                    "paid_amount is required when outcome=PAID — confirm the actual amount Safaricom shows for this receipt, do not assume it matches the order"
                )
            # Catch a receipt already on another escrow (e.g. a mistyped one) for a clean error instead of an
            # IntegrityError.
            clash = await db.scalar(
                select(EscrowTransaction).where(
                    EscrowTransaction.mpesa_receipt_number == p.mpesa_receipt_number,
                    EscrowTransaction.id != tx.id
                )
            )
            if clash:
                raise HTTPException(
                    409,
                    f"M-Pesa receipt {p.mpesa_receipt_number!r} is already associated with escrow #{clash.id}."
                )
            if _to_whole_kes(p.paid_amount) != _to_whole_kes(tx.amount):
                raise HTTPException(
                    422,
                    f"Amount mismatch: order expects KES {tx.amount}, receipt {p.mpesa_receipt_number!r} was for KES {p.paid_amount}. If this is a genuine partial/short payment, resolve it manually rather than through this endpoint."
                )
            tx.mpesa_receipt_number = p.mpesa_receipt_number
            tx.stk_amount = p.paid_amount
            tx.stk_phone_number = p.payer_phone or tx.buyer_phone
            tx.payment_method = "STK"
            tx.stk_callback_received_at = _now()
            tx.status = EscrowStatus.ESCROW_HELD
            tx.paid_at = _now()
            tx.qr_token = mpesa.generate_qr_token()
            tx.qr_view_token = mpesa.generate_qr_token()
            tx.qr_token_expires_at = mpesa.qr_token_expiry()
            if tx.order:
                tx.order.status = OrderStatus.PAID
            await post_ledger_transaction(
                db,
                reference=f"MPESA-HOLD-{tx.id}",
                idempotency_key=f"mpesa-hold-{tx.id}",
                description=f"Manually reconciled STK payment for escrow #{tx.id}",
                escrow_transaction_id=tx.id,
                debit_account_code="MPESA_CLEARING",
                credit_account_code="ESCROW_HOLDING",
                amount=tx.amount
            )
        else:
            tx.status = EscrowStatus.PAYMENT_FAILED
            if tx.order:
                tx.order.status = OrderStatus.CANCELLED
        await record_audit(
            db,
            admin_actor,
            f"escrow.stk_unknown.resolved_{outcome.lower()}",
            target_type="EscrowTransaction",
            target_id=tx.id,
            detail=f"receipt={p.mpesa_receipt_number!r} note={p.note!r}"
        )
        await db.commit()
        tx_id = tx.id
        resolved_outcome = outcome
        qr_url = _buyer_qr_url(tx) if outcome == "PAID" else None
        buyer_phone = tx.buyer_phone
        order_ref = tx.order.public_ref if tx.order else tx.id
    if resolved_outcome == "PAID":
        message = f"Order {order_ref}: your payment was confirmed after manual review and is now held in escrow."
        message += f" Open: {qr_url}" if qr_url else " Open the buyer app to display your delivery QR."
        async with AsyncSessionLocal() as db:
            await send_sms(db, buyer_phone, message, f"stk-unknown-resolved-buyer-{tx_id}")
            await db.commit()
        await notify_seller_escrow_held_after_commit(tx_id)
    else:
        async with AsyncSessionLocal() as db:
            await send_sms(
                db,
                buyer_phone,
                f"Order {order_ref}: after review, no payment was found. The order has been cancelled.",
                f"stk-unknown-resolved-buyer-{tx_id}"
            )
            await db.commit()
    return {"escrow_id": tx_id, "status": resolved_outcome}


@app.get("/admin/notifications/failed", dependencies=[Depends(verify_admin_key)])
async def list_failed_notifications(limit: int=Query(default=50, ge=1, le=200)):
    """FAILED notifications, whether still retrying or dead-lettered (attempts >=
    NOTIFICATION_MAX_TOTAL_ATTEMPTS).
    """

    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Notification)
                    .where(Notification.status == NotificationStatus.FAILED)
                    .order_by(Notification.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": n.id,
                "recipient_phone": mpesa.mask_phone(n.recipient_phone),
                "message": n.message,
                "attempts": n.attempts,
                "error": n.error,
                "dead_letter": n.attempts >= NOTIFICATION_MAX_TOTAL_ATTEMPTS,
                "created_at": n.created_at.isoformat()
            } for n in rows
        ]


@app.post("/admin/notifications/{notification_id}/retry")
async def retry_notification_now(notification_id: int, admin_actor: str=Depends(verify_admin_key)):
    async with AsyncSessionLocal() as db:
        notification = await db.scalar(
            select(Notification).where(Notification.id == notification_id).with_for_update()
        )
        if not notification:
            raise HTTPException(404, "Notification not found")
        if notification.status != NotificationStatus.FAILED:
            raise HTTPException(409, "Only FAILED notifications can be retried")
        # An admin retry is an explicit decision, so it resets the attempt ceiling.
        notification.attempts = 0
        succeeded = await retry_failed_notification(db, notification)
        await record_audit(
            db,
            admin_actor,
            "notification.retry",
            target_type="Notification",
            target_id=notification.id,
            detail=f"succeeded={succeeded}"
        )
        await db.commit()
        return {
            "notification_id": notification.id,
            "status": notification.status.value,
            "succeeded": succeeded
        }


@app.get("/admin/orders/flagged", dependencies=[Depends(verify_admin_key)])
async def list_flagged_orders(limit: int=Query(default=50, ge=1, le=200)):
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Order)
                    .where(Order.flagged_for_review == True)
                    .order_by(Order.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "order_id": o.id,
                "public_ref": o.public_ref,
                "buyer_phone": o.buyer_phone,
                "amount": str(o.amount),
                "flag_reason": o.flag_reason,
                "status": o.status.value,
                "created_at": o.created_at.isoformat()
            } for o in rows
        ]


# Legacy endpoint kept for backward compatibility.
class CreateEscrowRequest(BaseModel):
    buyer_phone: str
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2)
    assigned_agent_id: int
    description: Optional[str] = None
    delivery_provider: Optional[str] = None
    external_delivery_ref: Optional[str] = None


@app.post("/escrow/create", status_code=201, dependencies=[Depends(rate_limit), Depends(verify_platform_key)])
async def create_legacy_escrow(p: CreateEscrowRequest, request: Request):
    order_payload = CreateOrderRequest(
        buyer_phone=p.buyer_phone,
        amount=p.amount,
        assigned_agent_id=p.assigned_agent_id,
        description=p.description,
        delivery_address="Legacy escrow delivery"
    )
    return await create_order(order_payload, request)


async def get_tx_buyer(txid: int, token: Optional[str]):
    if not token:
        raise HTTPException(401, "Missing X-Buyer-Access-Token")
    async with AsyncSessionLocal() as db:
        tx = await db.get(EscrowTransaction, txid)
        if not tx or not secrets.compare_digest(token, tx.buyer_access_token):
            raise HTTPException(404, "Transaction not found")
        return tx


@app.get("/orders/{order_id}")
async def get_order(order_id: int, buyer_token: Optional[str]=Depends(_buyer)):
    if not buyer_token:
        raise HTTPException(401, "Missing X-Buyer-Access-Token")
    async with AsyncSessionLocal() as db:
        order = await db.get(Order, order_id)
        if (
            not order
            or not order.escrow
            or not secrets.compare_digest(buyer_token, order.escrow.buyer_access_token)
        ):
            raise HTTPException(404, "Order not found")
        return {
            "id": order.id,
            "ref": order.public_ref,
            "status": order.status.value,
            "amount": str(order.amount),
            "delivery_fee": str(order.delivery_fee),
            "address": order.delivery_address,
            "description": order.description
        }


@app.get("/escrow/{transaction_id}/status")
async def escrow_status(transaction_id: int, buyer_token: Optional[str]=Depends(_buyer)):
    tx = await get_tx_buyer(transaction_id, buyer_token)
    return {
        "transaction_id": tx.id,
        "order_id": tx.order_id,
        "status": tx.status.value,
        "amount": str(tx.amount),
        "qr_expires_at": tx.qr_token_expires_at.isoformat() if tx.qr_token_expires_at else None,
        "paid_at": tx.paid_at.isoformat() if tx.paid_at else None,
        "released_at": tx.released_at.isoformat() if tx.released_at else None,
        "buyer_qr_url": _buyer_qr_url(tx)
    }


@app.get("/escrow/{transaction_id}/qr")
async def escrow_qr(transaction_id: int, buyer_token: Optional[str]=Depends(_buyer)):
    tx = await get_tx_buyer(transaction_id, buyer_token)
    if tx.status != EscrowStatus.ESCROW_HELD or not tx.qr_token:
        raise HTTPException(409, "QR is not available")
    if not tx.qr_token_expires_at or _as_aware(tx.qr_token_expires_at) <= _now():
        raise HTTPException(410, "QR expired")
    return Response(
        mpesa.generate_qr_code_png(tx.qr_token),
        media_type="image/png",
        headers={"Cache-Control": "no-store"}
    )


@app.get("/buyer/escrow/{transaction_id}/qr")
async def buyer_qr_view(transaction_id: int, token: str=Query(..., min_length=32, max_length=64)):
    async with AsyncSessionLocal() as db:
        tx = await db.get(EscrowTransaction, transaction_id)
        if not tx or not tx.qr_view_token or not secrets.compare_digest(token, tx.qr_view_token):
            raise HTTPException(404, "QR not found")
        if tx.status != EscrowStatus.ESCROW_HELD or not tx.qr_token:
            raise HTTPException(409, "QR is not available")
        if not tx.qr_token_expires_at or _as_aware(tx.qr_token_expires_at) <= _now():
            raise HTTPException(410, "QR expired")
        return Response(
            mpesa.generate_qr_code_png(tx.qr_token),
            media_type="image/png",
            headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}
        )


@app.post("/admin/orders/{order_id}/assign-agent")
async def assign_agent(order_id: int, agent_id: int, admin_actor: str=Depends(verify_admin_key)):
    async with AsyncSessionLocal() as db:
        order = await db.get(
            Order,
            order_id,
            options=[selectinload(Order.escrow), selectinload(Order.delivery)]
        )
        agent = await db.get(DeliveryAgent, agent_id)
        if not order or not order.escrow:
            raise HTTPException(404, "Order not found")
        if not agent or not agent.is_active:
            raise HTTPException(404, "Agent not found")
        # Lock the escrow; assign only while nothing has been handed over.
        await db.execute(
            select(EscrowTransaction.id).where(EscrowTransaction.id == order.escrow.id).with_for_update()
        )
        await db.refresh(order.escrow)
        if order.escrow.status not in {EscrowStatus.PENDING_PAYMENT, EscrowStatus.ESCROW_HELD}:
            raise HTTPException(409, "This order can no longer be assigned to a different agent.")
        if (
            order.delivery is not None
            and order.delivery.status in {
                DeliveryStatus.PICKED_UP,
                DeliveryStatus.OUT_FOR_DELIVERY,
                DeliveryStatus.DELIVERED
            }
        ):
            raise HTTPException(409, "The delivery has already been picked up and cannot be reassigned.")
        delivery = order.delivery
        if not delivery:
            delivery = Delivery(order_id=order.id)
            db.add(delivery)
        delivery.agent_id = agent.id
        delivery.status = DeliveryStatus.ASSIGNED
        order.status = OrderStatus.ASSIGNED
        # assigned_agent_id only grants scan/confirm authority. It must never redirect the payout.
        order.escrow.assigned_agent_id = agent.id
        if not order.seller_id and order.escrow.status == EscrowStatus.PENDING_PAYMENT:
            # Only before payment: once funded, the payee is fixed.
            order.escrow.payee_phone = agent.phone_number
            order.escrow.payee_role = PayeeRole.RIDER
        await record_audit(
            db,
            admin_actor,
            "order.assign_agent",
            target_type="Order",
            target_id=order.id,
            detail=f"agent_id={agent.id}"
        )
        await db.commit()
        order_id = order.id
        assigned_agent_id = agent.id
        response_status = delivery.status.value
    async with AsyncSessionLocal() as notification_db:
        notification_order = await notification_db.get(Order, order_id, options=[selectinload(Order.escrow)])
        notification_agent = await notification_db.get(DeliveryAgent, assigned_agent_id)
        if notification_order and notification_agent:
            await send_sms(
                notification_db,
                notification_agent.phone_number,
                f"Escrow delivery {notification_order.public_ref} assigned to you. Order value KES {notification_order.escrow.amount} (held in escrow for the seller). Deliver and scan the buyer QR to confirm.",
                f"assigned-{order_id}-{assigned_agent_id}"
            )
            await notification_db.commit()
    return {"order_id": order_id, "agent_id": assigned_agent_id, "status": response_status}


@app.get("/rider/deliveries")
async def rider_deliveries(agent=Depends(get_authenticated_agent)):
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(Delivery).where(Delivery.agent_id == agent.id).order_by(Delivery.id.desc())
                )
            )
            .scalars()
            .all()
        )
        result = []
        for delivery in rows:
            order = await db.get(Order, delivery.order_id)
            result.append(
                {
                    "order_id": order.id,
                    "order_ref": order.public_ref,
                    "status": delivery.status.value,
                    "amount": str(order.amount + order.delivery_fee),
                    "address": order.delivery_address
                }
            )
        return result


async def _require_funded_escrow(db, order_id: int):
    """A rider may advance a delivery only while the buyer's money is held. Unpaid, cancelled, refunded or
    disputed orders can't be handed over.
    """

    tx = await db.scalar(select(EscrowTransaction).where(EscrowTransaction.order_id == order_id))
    if tx is None or tx.status != EscrowStatus.ESCROW_HELD:
        raise HTTPException(
            409,
            "This order is not funded or is no longer active; do not proceed with the delivery."
        )


@app.post("/deliveries/{order_id}/accept")
async def accept_delivery(order_id: int, agent=Depends(get_authenticated_agent)):
    async with AsyncSessionLocal() as db:
        delivery = await db.scalar(
            select(Delivery)
            .where(Delivery.order_id == order_id, Delivery.agent_id == agent.id)
            .with_for_update()
        )
        if not delivery:
            raise HTTPException(404, "Delivery not assigned")
        await _require_funded_escrow(db, order_id)
        if delivery.status != DeliveryStatus.ASSIGNED:
            raise HTTPException(409, "Delivery cannot be accepted in current state")
        delivery.status = DeliveryStatus.ACCEPTED
        order = await db.get(Order, order_id)
        order.status = OrderStatus.ASSIGNED
        await db.commit()
    return {"status": delivery.status.value}


@app.post("/deliveries/{order_id}/pickup")
async def pickup(order_id: int, agent=Depends(get_authenticated_agent)):
    async with AsyncSessionLocal() as db:
        delivery = await db.scalar(
            select(Delivery)
            .where(Delivery.order_id == order_id, Delivery.agent_id == agent.id)
            .with_for_update()
        )
        if not delivery or delivery.status not in [DeliveryStatus.ACCEPTED, DeliveryStatus.ASSIGNED]:
            raise HTTPException(409, "Cannot pickup")
        await _require_funded_escrow(db, order_id)
        delivery.status = DeliveryStatus.PICKED_UP
        delivery.picked_up_at = _now()
        order = await db.get(Order, order_id)
        order.status = OrderStatus.PICKED_UP
        await db.commit()
    return {"status": delivery.status.value}


@app.post("/deliveries/{order_id}/out-for-delivery")
async def out_for_delivery(order_id: int, agent=Depends(get_authenticated_agent)):
    async with AsyncSessionLocal() as db:
        delivery = await db.scalar(
            select(Delivery)
            .where(Delivery.order_id == order_id, Delivery.agent_id == agent.id)
            .with_for_update()
        )
        if not delivery or delivery.status != DeliveryStatus.PICKED_UP:
            raise HTTPException(409, "Cannot move to out-for-delivery")
        await _require_funded_escrow(db, order_id)
        delivery.status = DeliveryStatus.OUT_FOR_DELIVERY
        order = await db.get(Order, order_id)
        order.status = OrderStatus.OUT_FOR_DELIVERY
        await db.commit()
    return {"status": delivery.status.value}


@app.post("/escrow/confirm-delivery")
async def confirm_delivery(
    p: ConfirmDeliveryRequest,
    request: Request,
    agent=Depends(get_authenticated_agent)
):
    now = _now()
    async with AsyncSessionLocal() as db:
        stmt = (
            select(EscrowTransaction)
            .where(EscrowTransaction.qr_token == p.qr_token)
            .options(selectinload(EscrowTransaction.order).selectinload(Order.delivery))
            .with_for_update()
        )
        tx = await db.scalar(stmt)
        if not tx or tx.assigned_agent_id != agent.id:
            raise HTTPException(404, "Invalid QR or delivery assignment")
        if tx.status != EscrowStatus.ESCROW_HELD:
            raise HTTPException(409, "Escrow is not awaiting delivery confirmation")
        if not tx.qr_token_expires_at or _as_aware(tx.qr_token_expires_at) <= now:
            raise HTTPException(410, "QR expired")
        if not tx.order or not tx.order.delivery or tx.order.delivery.agent_id != agent.id:
            raise HTTPException(409, "Delivery assignment is invalid")
        if tx.order.delivery.status != DeliveryStatus.OUT_FOR_DELIVERY:
            raise HTTPException(409, "Delivery must be out-for-delivery before QR confirmation")
        # synchronize_session=False: in-memory sync would re-evaluate the datetime comparison in Python
        # against a possibly naive SQLite value (TypeError). db.refresh(tx) below reloads it anyway.
        claimed = await db.execute(
            update(EscrowTransaction)
            .where(
                EscrowTransaction.id == tx.id,
                EscrowTransaction.assigned_agent_id == agent.id,
                EscrowTransaction.status == EscrowStatus.ESCROW_HELD,
                EscrowTransaction.qr_token == p.qr_token,
                EscrowTransaction.qr_token_used_at.is_(None),
                EscrowTransaction.qr_token_expires_at > now
            )
            .values(status=EscrowStatus.DELIVERY_CONFIRMED, qr_token_used_at=now, delivered_at=now)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:
            raise HTTPException(409, "QR has already been used or escrow is no longer awaiting confirmation")
        await db.refresh(tx)
        tx.order.status = OrderStatus.DELIVERED
        tx.order.delivery.status = DeliveryStatus.DELIVERED
        tx.order.delivery.delivered_at = now
        # Delivery still counts as confirmed, but a deactivated or unverified seller's release goes to admin
        # dispute resolution instead of paying out automatically.
        seller_blocked = False
        seller_block_reason = None
        if tx.payee_role == PayeeRole.SELLER and tx.order.seller_id:
            seller = await db.get(Seller, tx.order.seller_id)
            if seller and not seller.is_active:
                seller_blocked = True
                seller_block_reason = "the seller account is deactivated"
            elif seller and seller.kyc_status != SellerKycStatus.VERIFIED:
                seller_blocked = True
                seller_block_reason = "the seller has not completed identity verification (KYC)"
        if seller_blocked:
            tx.status = EscrowStatus.DISPUTED
            tx.order.status = OrderStatus.DISPUTED
            db.add(
                Dispute(
                    order_id=tx.order.id,
                    opened_by_phone=tx.buyer_phone,
                    reason=f"Automatically opened: {seller_block_reason} at the time of delivery confirmation. Payout held pending admin review."
                )
            )
            ok = None
            payout = None
        else:
            release_method, release_recipient = await _select_release_method_and_recipient(db, tx)
            payout = await create_payout(
                db,
                tx,
                purpose="RELEASE",
                recipient_phone=release_recipient,
                amount=tx.amount,
                debit_account="ESCROW_HOLDING",
                credit_account="RIDER_PAYABLE" if tx.payee_role == PayeeRole.RIDER else "SELLER_PAYABLE",
                method=release_method
            )
            ok = await initiate_payout_for_record(db, payout, request.app.state.http_client)
            if not ok:
                tx.status = EscrowStatus.PAYOUT_FAILED
        await db.commit()
        tx_id = tx.id
        final_status = tx.status.value
    if seller_blocked:
        await notify_delivery_confirmed_hold_after_commit(tx_id)
        return {
            "transaction_id": tx_id,
            "status": final_status,
            "payout_status": None,
            "message": f"Delivery confirmed. Payout held for admin review because {seller_block_reason}."
        }
    if ok:
        await notify_delivery_confirmed_after_commit(tx_id)
    return {"transaction_id": tx_id, "status": final_status, "payout_status": payout.status}


@app.post("/disputes", status_code=201)
async def open_dispute(p: DisputeRequest, buyer_token: Optional[str]=Depends(_buyer)):
    if not buyer_token:
        raise HTTPException(401, "Missing X-Buyer-Access-Token")
    async with AsyncSessionLocal() as db:
        order = await db.get(Order, p.order_id)
        if (
            not order
            or not order.escrow
            or not secrets.compare_digest(buyer_token, order.escrow.buyer_access_token)
        ):
            raise HTTPException(404, "Order/escrow not found")
        tx = order.escrow
        if tx.status != EscrowStatus.ESCROW_HELD:
            raise HTTPException(409, "An escrow dispute must be opened before delivery confirmation/payout")
        existing = await db.scalar(
            select(Dispute).where(
                Dispute.order_id == order.id,
                Dispute.status.in_([DisputeStatus.OPEN, DisputeStatus.UNDER_REVIEW])
            )
        )
        if existing:
            raise HTTPException(409, "An active dispute already exists for this order")
        # Atomic conditional update: a QR scan may be mid-flight with a RELEASE payout. A plain write could
        # let an admin later create a REFUND too (different purpose, so the unique constraint doesn't block
        # it).
        claimed = await db.execute(
            update(EscrowTransaction)
            .where(EscrowTransaction.id == tx.id, EscrowTransaction.status == EscrowStatus.ESCROW_HELD)
            .values(status=EscrowStatus.DISPUTED)
        )
        if claimed.rowcount != 1:
            raise HTTPException(
                409,
                "Escrow moved to a different state while opening the dispute (a delivery confirmation or expiry may have just landed); please check the order status and try again"
            )
        dispute = Dispute(
            order_id=order.id,
            opened_by_phone=tx.buyer_phone,
            reason=p.reason,
            evidence=p.evidence
        )
        db.add(dispute)
        order.status = OrderStatus.DISPUTED
        await db.commit()
        await db.refresh(dispute)
    return {"dispute_id": dispute.id, "status": dispute.status.value}


async def _finalize_escrow_after_payouts(db, tx: EscrowTransaction) -> None:
    payouts = (await db.execute(select(Payout).where(Payout.escrow_transaction_id == tx.id))).scalars().all()
    # SUBMITTING is in-flight like PENDING/INITIATED; otherwise the escrow could be finalized while money is
    # still moving.
    if any((p.status in {"PENDING", "SUBMITTING", "INITIATED"} for p in payouts)):
        return
    if any((p.status in {"FAILED", "TIMEOUT"} for p in payouts)):
        return
    completed_release = sum(
        (p.amount for p in payouts if p.purpose == "RELEASE" and p.status == "COMPLETED"),
        Decimal("0.00")
    )
    completed_refund = sum(
        (p.amount for p in payouts if p.purpose == "REFUND" and p.status == "COMPLETED"),
        Decimal("0.00")
    )
    if completed_refund == tx.amount and completed_release == 0:
        tx.status = EscrowStatus.REFUNDED
        tx.refunded_at = _now()
        if tx.order:
            tx.order.status = OrderStatus.REFUNDED
    elif completed_release == tx.amount and completed_refund == 0:
        tx.status = EscrowStatus.RELEASED
        tx.released_at = _now()
        if tx.order:
            tx.order.status = OrderStatus.COMPLETED
    elif completed_release + completed_refund == tx.amount:
        tx.status = EscrowStatus.PARTIALLY_SETTLED
        if tx.order:
            tx.order.status = OrderStatus.COMPLETED


@app.post("/admin/disputes/{dispute_id}/resolve")
async def resolve_dispute(
    dispute_id: int,
    p: ResolveDisputeRequest,
    request: Request,
    admin_actor: str=Depends(require_admin_role(AdminRole.DISPUTE_ADMIN.value))
):
    decision = p.decision.upper()
    async with AsyncSessionLocal() as db:
        # Lock both rows so two concurrent resolves can't both create payouts.
        dispute = await db.scalar(select(Dispute).where(Dispute.id == dispute_id).with_for_update())
        if not dispute:
            raise HTTPException(404, "Dispute not found")
        tx = await db.scalar(
            select(EscrowTransaction).where(EscrowTransaction.order_id == dispute.order_id).with_for_update()
        )
        order = await db.get(Order, dispute.order_id)
        if not tx or tx.status != EscrowStatus.DISPUTED:
            raise HTTPException(409, "Escrow is not available for dispute resolution")
        if dispute.status not in {DisputeStatus.OPEN, DisputeStatus.UNDER_REVIEW}:
            raise HTTPException(409, "Dispute is already resolved")
        if decision not in {"REFUND", "RELEASE", "PARTIAL"}:
            raise HTTPException(422, "decision must be REFUND, RELEASE, or PARTIAL")
        total = tx.amount
        if decision == "PARTIAL":
            if p.refund_amount is None or p.refund_amount <= 0 or p.refund_amount >= total:
                raise HTTPException(422, "refund_amount must be between 0 and escrow amount")
            refund = p.refund_amount
            release = total - refund
            dispute.status = DisputeStatus.RESOLVED_PARTIAL
            dispute.refund_amount = refund
            release_method, release_recipient = await _select_release_method_and_recipient(db, tx)
            refund_payout = await create_payout(
                db,
                tx,
                purpose="REFUND",
                recipient_phone=tx.buyer_phone,
                amount=refund,
                debit_account="ESCROW_HOLDING",
                credit_account="BUYER_REFUND_PAYABLE",
                method=_select_refund_method(tx)
            )
            release_payout = await create_payout(
                db,
                tx,
                purpose="RELEASE",
                recipient_phone=release_recipient,
                amount=release,
                debit_account="ESCROW_HOLDING",
                credit_account="RIDER_PAYABLE" if tx.payee_role == PayeeRole.RIDER else "SELLER_PAYABLE",
                method=release_method
            )
            tx.status = EscrowStatus.PARTIALLY_SETTLED
            results = []
            for payout in (refund_payout, release_payout):
                ok = await initiate_payout_for_record(db, payout, request.app.state.http_client)
                results.append((payout, ok))
            if any((not ok for _, ok in results)):
                tx.status = EscrowStatus.PAYOUT_FAILED
        else:
            dispute.status = (
                DisputeStatus.RESOLVED_REFUND
                if decision == "REFUND"
                else DisputeStatus.RESOLVED_RELEASE
            )
            if decision == "REFUND":
                payout = await create_payout(
                    db,
                    tx,
                    purpose="REFUND",
                    recipient_phone=tx.buyer_phone,
                    amount=total,
                    debit_account="ESCROW_HOLDING",
                    credit_account="BUYER_REFUND_PAYABLE",
                    method=_select_refund_method(tx)
                )
                tx.status = EscrowStatus.REFUND_INITIATED
            else:
                release_method, release_recipient = await _select_release_method_and_recipient(db, tx)
                payout = await create_payout(
                    db,
                    tx,
                    purpose="RELEASE",
                    recipient_phone=release_recipient,
                    amount=total,
                    debit_account="ESCROW_HOLDING",
                    credit_account="RIDER_PAYABLE" if tx.payee_role == PayeeRole.RIDER else "SELLER_PAYABLE",
                    method=release_method
                )
                tx.status = EscrowStatus.DELIVERY_CONFIRMED
            ok = await initiate_payout_for_record(db, payout, request.app.state.http_client)
            if not ok:
                tx.status = EscrowStatus.REFUND_FAILED if decision == "REFUND" else EscrowStatus.PAYOUT_FAILED
        dispute.resolution_note = p.note
        dispute.resolved_at = _now()
        await record_audit(
            db,
            admin_actor,
            "dispute.resolve",
            target_type="Dispute",
            target_id=dispute.id,
            detail=f"decision={decision} escrow_id={tx.id} note={p.note!r}"
        )
        await db.commit()
        if decision == "PARTIAL":
            payout_results = [
                {
                    "payout_id": payout.id,
                    "purpose": payout.purpose,
                    "payout_status": payout.status,
                    "initiated": ok
                } for payout,
                ok in results
            ]
        else:
            payout_results = [
                {
                    "payout_id": payout.id,
                    "purpose": payout.purpose,
                    "payout_status": payout.status,
                    "initiated": ok
                }
            ]
        escrow_id = tx.id
    await notify_dispute_resolved_after_commit(dispute_id, escrow_id)
    response = {
        "dispute_id": dispute_id,
        "status": dispute.status.value,
        "escrow_status": tx.status.value,
        "payouts": payout_results
    }
    if decision != "PARTIAL":
        response.update(
            {
                "payout_id": payout_results[0]["payout_id"],
                "payout_status": payout_results[0]["payout_status"],
                "initiated": payout_results[0]["initiated"]
            }
        )
    return response


@app.post("/callbacks/c2b-validation")
async def c2b_validation(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    """Runs BEFORE Safaricom credits the shortcode: the only point where a bad reference or amount can be
    rejected. Some paybills never call Validation, so the Confirmation checks remain the real backstop.
    """

    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    parsed = mpesa.parse_c2b_notification(body)
    ref = parsed["bill_ref_number"]
    amount = _parse_c2b_amount(parsed["amount"])
    # Confirm the callback is for our shortcode. X-Mpesa-Secret is the main defense; this catches a cross-
    # wired URL.
    if (
        mpesa.MPESA_SHORTCODE
        and parsed["business_short_code"]
        and (str(parsed["business_short_code"]) != str(mpesa.MPESA_SHORTCODE))
    ):
        return {"ResultCode": "C2B00016", "ResultDesc": "Rejected - shortcode mismatch"}
    async with AsyncSessionLocal() as db:
        order = await db.scalar(
            select(Order).where(Order.public_ref == ref).options(selectinload(Order.escrow))
        )
        tx = order.escrow if order else None
        if not order or not tx or tx.status != EscrowStatus.PENDING_PAYMENT:
            return {
                "ResultCode": "C2B00012",
                "ResultDesc": "Rejected - unknown or already-settled order reference"
            }
        if amount is None or amount != tx.amount:
            return {"ResultCode": "C2B00013", "ResultDesc": "Rejected - amount does not match order total"}
    return {"ResultCode": "0", "ResultDesc": "Accepted"}


@app.post("/callbacks/c2b-confirmation")
async def c2b_confirmation(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    """Fires AFTER the shortcode is credited, so it can only acknowledge. Unmatchable payments go to
    UnmatchedC2BPayment for manual reconciliation.
    """

    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    parsed = mpesa.parse_c2b_notification(body)
    receipt = parsed["mpesa_receipt_number"]
    ref = parsed["bill_ref_number"]
    amount = _parse_c2b_amount(parsed["amount"])
    phone = mpesa.normalize_phone_number(str(parsed["msisdn"])) if parsed["msisdn"] else None
    # Confirmation can't reject (the money already moved), but a shortcode mismatch usually means a cross-
    # wired URL: alert.
    if (
        mpesa.MPESA_SHORTCODE
        and parsed["business_short_code"]
        and (str(parsed["business_short_code"]) != str(mpesa.MPESA_SHORTCODE))
    ):
        await alert_payout_issue(
            "C2B_SHORTCODE_MISMATCH",
            0,
            0,
            f"C2B confirmation for shortcode {parsed['business_short_code']!r} does not match MPESA_SHORTCODE={mpesa.MPESA_SHORTCODE!r}; receipt={receipt} ref={ref!r}"
        )
    async with AsyncSessionLocal() as db:
        if receipt:
            # Cheap unlocked pre-check; not sufficient alone, see the re-check after the row lock.
            if await db.scalar(
                select(UnmatchedC2BPayment).where(UnmatchedC2BPayment.mpesa_receipt_number == receipt)
            ):
                return {"ResultCode": "0", "ResultDesc": "Accepted"}
            # Also check EscrowTransaction globally: a duplicate delivery with a different bill-ref would
            # otherwise hit the unique constraint as a raw IntegrityError. The constraint stays the backstop.
            already = await db.scalar(
                select(EscrowTransaction).where(EscrowTransaction.mpesa_receipt_number == receipt)
            )
            if already:
                return {"ResultCode": "0", "ResultDesc": "Accepted"}
        order = await db.scalar(select(Order).where(Order.public_ref == ref))
        tx = (
            await db.scalar(
                select(EscrowTransaction).where(EscrowTransaction.order_id == order.id).with_for_update()
            )
            if order
            else None
        )
        if receipt and tx and (tx.mpesa_receipt_number == receipt):
            # Re-check after the lock: a concurrent duplicate can slip past the unlocked checks, wait on the
            # lock, and must not be misfiled as unmatched.
            return {"ResultCode": "0", "ResultDesc": "Accepted"}
        # Unlike STK, C2B doesn't require the payer's phone to match: paying on someone's behalf is normal.
        # Reference plus exact amount is the bar.
        matched = (
            tx is not None
            and tx.status == EscrowStatus.PENDING_PAYMENT
            and (amount is not None)
            and receipt
            and phone
            and (amount == tx.amount)
        )
        if not matched:
            db.add(
                UnmatchedC2BPayment(
                    mpesa_receipt_number=receipt or f"unknown-{secrets.token_hex(8)}",
                    amount=amount or Decimal("0"),
                    msisdn=phone or str(parsed["msisdn"] or "unknown"),
                    bill_ref_number=ref or "",
                    transaction_time=parsed["transaction_time"],
                    matched_order_id=order.id if order else None
                )
            )
            await db.commit()
            await alert_payout_issue(
                "UNMATCHED_C2B_PAYMENT",
                0,
                order.id if order else 0,
                f"C2B payment {receipt} of {amount} for ref {ref!r} could not be auto-matched to a pending order; needs manual reconciliation"
            )
            return {"ResultCode": "0", "ResultDesc": "Accepted"}
        tx.mpesa_receipt_number = receipt
        tx.stk_amount = amount
        tx.stk_phone_number = phone
        tx.payment_method = "C2B"
        tx.stk_callback_received_at = _now()
        tx.status = EscrowStatus.ESCROW_HELD
        tx.paid_at = _now()
        tx.qr_token = mpesa.generate_qr_token()
        tx.qr_view_token = mpesa.generate_qr_token()
        tx.qr_token_expires_at = mpesa.qr_token_expiry()
        if tx.order:
            tx.order.status = OrderStatus.PAID
        await post_ledger_transaction(
            db,
            reference=f"MPESA-HOLD-{tx.id}",
            idempotency_key=f"mpesa-hold-{tx.id}",
            description=f"Hold M-Pesa C2B payment for escrow #{tx.id}",
            escrow_transaction_id=tx.id,
            debit_account_code="MPESA_CLEARING",
            credit_account_code="ESCROW_HOLDING",
            amount=tx.amount
        )
        recon = await db.scalar(
            select(ReconciliationRecord).where(
                ReconciliationRecord.provider == "MPESA_C2B",
                ReconciliationRecord.provider_reference == receipt
            )
        )
        if not recon:
            db.add(
                ReconciliationRecord(
                    provider="MPESA_C2B",
                    provider_reference=receipt,
                    escrow_transaction_id=tx.id,
                    amount=tx.amount,
                    status="SUCCESS",
                    raw_payload=json.dumps(body, separators=(",", ":"), default=str)
                )
            )
        qr_url = _buyer_qr_url(tx)
        message = f"Order {(tx.order.public_ref if tx.order else tx.id)}: payment confirmed and held in escrow. Your delivery QR expires in {mpesa.QR_TOKEN_VALIDITY_HOURS} hours."
        message += f" Open: {qr_url}" if qr_url else " Open the buyer app to display your delivery QR."
        await send_sms(db, tx.buyer_phone, message, f"qr-ready-buyer-{tx.id}")
        if tx.assigned_agent:
            await send_sms(
                db,
                tx.assigned_agent.phone_number,
                f"Escrow delivery #{tx.id} is funded. Deliver the order and scan the buyer QR after delivery.",
                f"qr-ready-agent-{tx.id}"
            )
        await db.commit()
        tx_id = tx.id
    await notify_seller_escrow_held_after_commit(tx_id)
    return {"ResultCode": "0", "ResultDesc": "Accepted"}


@app.post("/callbacks/balance-callback")
async def balance_callback(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    parsed = mpesa.parse_account_balance_callback(body)
    conversation_id = parsed["conversation_id"]
    if conversation_id:
        async with AsyncSessionLocal() as db:
            # Row lock: Safaricom retries callbacks, and two deliveries could both read PENDING and clobber
            # each other.
            check = await db.scalar(
                select(AccountBalanceCheck)
                .where(AccountBalanceCheck.conversation_id == conversation_id)
                .with_for_update()
            )
            if check and check.status == "PENDING":
                check.status = "COMPLETED" if parsed["success"] else "FAILED"
                check.result_desc = parsed["result_desc"]
                check.working_account_balance = (
                    Decimal(str(parsed["working_account_balance"]))
                    if parsed["working_account_balance"] is not None
                    else None
                )
                check.raw_result = json.dumps(body, separators=(",", ":"), default=str)
                check.completed_at = _now()
                await db.commit()
                if (
                    check.working_account_balance is not None
                    and check.working_account_balance < FLOAT_LOW_BALANCE_THRESHOLD
                ):
                    await alert_payout_issue(
                        "LOW_B2C_FLOAT",
                        0,
                        0,
                        f"B2C working account balance is KES {check.working_account_balance}, below the KES {FLOAT_LOW_BALANCE_THRESHOLD} alert threshold"
                    )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/callbacks/balance-timeout")
async def balance_timeout(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    conversation_id = body.get("Result", {}).get("ConversationID") or body.get("ConversationID")
    if conversation_id:
        async with AsyncSessionLocal() as db:
            check = await db.scalar(
                select(AccountBalanceCheck)
                .where(AccountBalanceCheck.conversation_id == conversation_id)
                .with_for_update()
            )
            if check and check.status == "PENDING":
                check.status = "FAILED"
                check.result_desc = "Balance query queue timeout"
                check.completed_at = _now()
                await db.commit()
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/admin/mpesa/account-balance/check")
async def trigger_account_balance_check(request: Request, admin_actor: str=Depends(verify_admin_key)):
    """Start an Account Balance query. The balance arrives later on the balance callback; poll GET
    /admin/mpesa/account-balance/latest.
    """

    originator_conversation_id = f"balance-check-{secrets.token_hex(8)}"
    async with AsyncSessionLocal() as db:
        check = AccountBalanceCheck(conversation_id=None, status="PENDING")
        db.add(check)
        await db.flush()
        try:
            reply = await mpesa.query_account_balance(
                request.app.state.http_client,
                originator_conversation_id
            )
        except Exception as exc:
            check.status = "FAILED"
            check.result_desc = str(exc)
            await db.commit()
            raise HTTPException(502, f"Could not initiate account balance query: {exc}")
        check.conversation_id = reply.get("ConversationID")
        await record_audit(
            db,
            admin_actor,
            "mpesa.account_balance.check",
            target_type="AccountBalanceCheck",
            target_id=check.id,
            detail=None
        )
        await db.commit()
        return {
            "account_balance_check_id": check.id,
            "conversation_id": check.conversation_id,
            "status": check.status
        }


@app.get("/admin/mpesa/account-balance/latest", dependencies=[Depends(verify_admin_key)])
async def latest_account_balance():
    async with AsyncSessionLocal() as db:
        check = await db.scalar(
            select(AccountBalanceCheck)
            .where(AccountBalanceCheck.status == "COMPLETED")
            .order_by(AccountBalanceCheck.completed_at.desc())
            .limit(1)
        )
        if not check:
            raise HTTPException(404, "No completed account balance check yet")
        return {
            "working_account_balance": str(check.working_account_balance) if check.working_account_balance is not None else None,
            "checked_at": check.completed_at.isoformat() if check.completed_at else None
        }


@app.post("/callbacks/transaction-status-callback")
async def transaction_status_callback(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    """Record the queried status for manual reconciliation; it never moves a payout by itself. The result is tied
    to its query via the OriginatorConversationID and stored as a TransactionStatusQuery row.
    """

    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    parsed = mpesa.parse_transaction_status_callback(body)
    logging.warning(
        "Transaction status result: conversation_id=%s result_desc=%s status=%s receipt=%s",
        parsed["conversation_id"],
        parsed["result_desc"],
        parsed["transaction_status"],
        parsed["receipt_number"]
    )
    originator_conversation_id = parsed.get("originator_conversation_id")
    if originator_conversation_id:
        async with AsyncSessionLocal() as db:
            q = await db.scalar(
                select(TransactionStatusQuery)
                .where(TransactionStatusQuery.query_conversation_id == originator_conversation_id)
                .with_for_update()
            )
            if q and q.status == "PENDING":
                q.status = "ANSWERED"
                q.result_code = parsed["result_code"]
                q.result_desc = parsed["result_desc"]
                q.transaction_status = parsed["transaction_status"]
                q.receipt_number = parsed["receipt_number"]
                q.raw_response = json.dumps(body, separators=(",", ":"), default=str)
                q.answered_at = _now()
                await db.commit()
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/callbacks/transaction-status-timeout")
async def transaction_status_timeout(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    originator_conversation_id = (
        body.get("Result", {}).get("OriginatorConversationID")
        or body.get("OriginatorConversationID")
    )
    # Without this, a status query whose queue-timeout (not result) callback arrives would sit at PENDING
    # forever.
    if originator_conversation_id:
        async with AsyncSessionLocal() as db:
            q = await db.scalar(
                select(TransactionStatusQuery)
                .where(TransactionStatusQuery.query_conversation_id == originator_conversation_id)
                .with_for_update()
            )
            if q and q.status == "PENDING":
                q.status = "TIMEOUT"
                q.result_desc = "Transaction status query queue timeout"
                q.answered_at = _now()
                await db.commit()
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/admin/payouts/{payout_id}/query-status")
async def query_payout_status(
    payout_id: int,
    request: Request,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    """Ask Safaricom for a stuck payout's real state. The answer is recorded by the transaction-status callback.
    """

    async with AsyncSessionLocal() as db:
        payout = await db.scalar(select(Payout).where(Payout.id == payout_id).with_for_update())
        if not payout:
            raise HTTPException(404, "Payout not found")
        if not payout.transaction_id and not payout.conversation_id:
            raise HTTPException(409, "Payout has no transaction/conversation ID to query yet")
        originator_conversation_id = f"status-query-{payout_id}-{secrets.token_hex(6)}"
        # Persist a structured record before firing, so there is an auditable answer even if the callback
        # lands minutes later.
        db.add(
            TransactionStatusQuery(
                payout_id=payout.id,
                payout_method=payout.method,
                query_conversation_id=originator_conversation_id,
                queried_transaction_id=payout.transaction_id or payout.conversation_id,
                status="PENDING"
            )
        )
        try:
            await mpesa.query_transaction_status(
                request.app.state.http_client,
                transaction_id=payout.transaction_id or payout.conversation_id,
                originator_conversation_id=originator_conversation_id,
                occasion=f"Escrow payout #{payout_id}"
            )
        except Exception as exc:
            raise HTTPException(502, f"Could not initiate transaction status query: {exc}")
        await record_audit(
            db,
            admin_actor,
            "payout.query_status",
            target_type="Payout",
            target_id=payout.id,
            detail=f"query_conversation_id={originator_conversation_id}"
        )
        await db.commit()
    return {"payout_id": payout_id, "queried": True, "query_conversation_id": originator_conversation_id}


@app.get("/admin/c2b/unmatched", dependencies=[Depends(verify_admin_key)])
async def list_unmatched_c2b_payments(resolved: bool=False, limit: int=Query(default=50, ge=1, le=200)):
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(UnmatchedC2BPayment)
                    .where(UnmatchedC2BPayment.resolved == resolved)
                    .order_by(UnmatchedC2BPayment.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "mpesa_receipt_number": r.mpesa_receipt_number,
                "amount": str(r.amount),
                "msisdn": r.msisdn,
                "bill_ref_number": r.bill_ref_number,
                "matched_order_id": r.matched_order_id,
                "resolved": r.resolved,
                "status": r.status,
                "created_at": r.created_at.isoformat()
            } for r in rows
        ]


class ResolveUnmatchedC2BRequest(BaseModel):
    note: str = Field(min_length=1, max_length=1000)
    outcome: str = Field(default="CLOSED")  # REFUNDED_EXTERNALLY, WRITTEN_OFF, or CLOSED


@app.post("/admin/c2b/unmatched/{payment_id}/resolve")
async def resolve_unmatched_c2b_payment(
    payment_id: int,
    p: ResolveUnmatchedC2BRequest,
    admin_actor: str=Depends(verify_admin_key)
):
    """Mark an unmatched C2B payment as resolved outside this system (e.g. refunded by hand). Moves no money; to
    credit an order use apply-to-order.
    """

    outcome = p.outcome.upper()
    if outcome not in {"REFUNDED_EXTERNALLY", "WRITTEN_OFF", "CLOSED"}:
        raise HTTPException(422, "outcome must be REFUNDED_EXTERNALLY, WRITTEN_OFF, or CLOSED")
    async with AsyncSessionLocal() as db:
        row = await db.get(UnmatchedC2BPayment, payment_id)
        if not row:
            raise HTTPException(404, "Unmatched payment not found")
        row.resolved = True
        row.status = outcome
        row.resolution_note = p.note
        await record_audit(
            db,
            admin_actor,
            "c2b.unmatched.resolve",
            target_type="UnmatchedC2BPayment",
            target_id=row.id,
            detail=f"outcome={outcome} note={p.note}"
        )
        await db.commit()
    return {"id": row.id, "resolved": True, "status": row.status}


class ApplyUnmatchedC2BRequest(BaseModel):
    order_id: int

    note: Optional[str] = Field(default=None, max_length=1000)


@app.post("/admin/c2b/unmatched/{payment_id}/apply-to-order")
async def apply_unmatched_c2b_to_order(
    payment_id: int,
    p: ApplyUnmatchedC2BRequest,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    """Credit an unmatched C2B payment to a real order. Needs the escrow still PENDING_PAYMENT and an exact
    amount match; for anything else resolve it manually.
    """

    async with AsyncSessionLocal() as db:
        row = await db.scalar(
            select(UnmatchedC2BPayment).where(UnmatchedC2BPayment.id == payment_id).with_for_update()
        )
        if not row:
            raise HTTPException(404, "Unmatched payment not found")
        if row.resolved:
            raise HTTPException(409, "This unmatched payment was already resolved")
        order = await db.get(Order, p.order_id)
        if not order or not order.escrow:
            raise HTTPException(404, "Order not found")
        tx = await db.scalar(
            select(EscrowTransaction).where(EscrowTransaction.id == order.escrow.id).with_for_update()
        )
        if tx.status != EscrowStatus.PENDING_PAYMENT:
            raise HTTPException(409, "Order's escrow is not awaiting payment")
        if _to_whole_kes(tx.amount) != _to_whole_kes(row.amount):
            raise HTTPException(
                422,
                f"Amount mismatch: order expects KES {tx.amount}, payment was KES {row.amount}"
            )
        # Catch a receipt already on a different escrow for a clean reconciliation error instead of an
        # IntegrityError mid-commit.
        clash = await db.scalar(
            select(EscrowTransaction).where(
                EscrowTransaction.mpesa_receipt_number == row.mpesa_receipt_number,
                EscrowTransaction.id != tx.id
            )
        )
        if clash:
            raise HTTPException(
                409,
                f"M-Pesa receipt {row.mpesa_receipt_number!r} is already associated with escrow #{clash.id}."
            )
        tx.mpesa_receipt_number = row.mpesa_receipt_number
        tx.stk_amount = row.amount
        tx.stk_phone_number = row.msisdn
        tx.payment_method = "C2B"
        tx.stk_callback_received_at = _now()
        tx.status = EscrowStatus.ESCROW_HELD
        tx.paid_at = _now()
        tx.qr_token = mpesa.generate_qr_token()
        tx.qr_view_token = mpesa.generate_qr_token()
        tx.qr_token_expires_at = mpesa.qr_token_expiry()
        order.status = OrderStatus.PAID
        row.resolved = True
        row.status = "APPLIED_TO_ORDER"
        row.matched_order_id = order.id
        row.resolution_note = p.note or f"Manually applied to order {order.public_ref}"
        await post_ledger_transaction(
            db,
            reference=f"MPESA-HOLD-{tx.id}",
            idempotency_key=f"mpesa-hold-{tx.id}",
            description=f"Manually reconciled unmatched C2B payment for escrow #{tx.id}",
            escrow_transaction_id=tx.id,
            debit_account_code="MPESA_CLEARING",
            credit_account_code="ESCROW_HOLDING",
            amount=tx.amount
        )
        await record_audit(
            db,
            admin_actor,
            "c2b.unmatched.apply_to_order",
            target_type="UnmatchedC2BPayment",
            target_id=row.id,
            detail=f"order_id={order.id} escrow_id={tx.id}"
        )
        await db.commit()
        tx_id = tx.id
        buyer_phone = tx.buyer_phone
        order_ref = order.public_ref
        qr_url = _buyer_qr_url(tx)
    message = f"Order {order_ref}: payment confirmed after manual review and held in escrow."
    message += f" Open: {qr_url}" if qr_url else " Open the buyer app to display your delivery QR."
    async with AsyncSessionLocal() as db:
        await send_sms(db, buyer_phone, message, f"c2b-applied-buyer-{tx_id}")
        await db.commit()
    await notify_seller_escrow_held_after_commit(tx_id)
    return {"payment_id": payment_id, "applied_to_order_id": p.order_id, "escrow_id": tx_id}


_UNAPPLIED_STATUSES = {"OPEN", "APPLIED", "REFUNDED_EXTERNALLY", "WRITTEN_OFF"}


@app.get("/admin/unapplied-payments")
async def list_unapplied_payments(
    status: str="OPEN",
    limit: int=50,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    """Money Safaricom confirmed that couldn't be applied to an escrow. Held for a finance admin, never refunded
    automatically.
    """

    status = status.upper()
    if status not in _UNAPPLIED_STATUSES:
        raise HTTPException(422, "status must be one of " + ", ".join(sorted(_UNAPPLIED_STATUSES)))
    limit = max(1, min(int(limit), 200))
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(UnappliedPayment)
                    .where(UnappliedPayment.status == status)
                    .order_by(UnappliedPayment.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "source": r.source,
                "provider_reference": r.provider_reference,
                "mpesa_receipt_number": r.mpesa_receipt_number,
                "amount": str(r.amount) if r.amount is not None else None,
                "phone": mpesa.mask_phone(r.phone_number),
                "escrow_id": r.escrow_transaction_id,
                "reason": r.reason,
                "status": r.status,
                "created_at": r.created_at.isoformat()
            } for r in rows
        ]


class ResolveUnappliedRequest(BaseModel):
    outcome: str
    note: str = Field(min_length=3, max_length=1000)
    refund_reference: str = Field(min_length=3, max_length=100)


@app.post("/admin/unapplied-payments/{payment_id}/resolve")
async def resolve_unapplied_payment(
    payment_id: int,
    p: ResolveUnappliedRequest,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    """Mark a held payment as refunded outside this system and clear it from UNAPPLIED_PAYMENTS. Needs the
    refund's M-Pesa receipt. Only REFUNDED_EXTERNALLY is supported; a write-off is a business/legal decision.
    """

    outcome = p.outcome.upper()
    if outcome != "REFUNDED_EXTERNALLY":
        raise HTTPException(422, "outcome must be REFUNDED_EXTERNALLY")
    async with AsyncSessionLocal() as db:
        row = await db.scalar(
            select(UnappliedPayment).where(UnappliedPayment.id == payment_id).with_for_update()
        )
        if not row:
            raise HTTPException(404, "Unapplied payment not found")
        if row.status != "OPEN":
            raise HTTPException(409, "This payment is already resolved")
        if row.amount is not None:
            await post_ledger_transaction(
                db,
                reference=f"UNAPPLIED-RESOLVE-{row.id}",
                idempotency_key=f"unapplied-resolve-{row.id}",
                description=f"Unapplied payment #{row.id} refunded externally",
                escrow_transaction_id=row.escrow_transaction_id,
                debit_account_code="UNAPPLIED_PAYMENTS",
                credit_account_code="MPESA_CLEARING",
                amount=row.amount
            )
        row.status = outcome
        row.resolved_at = _now()
        row.resolution_note = f"{p.note} | refund_reference={p.refund_reference}"
        await record_audit(
            db,
            admin_actor,
            "unapplied_payment.resolve",
            target_type="UnappliedPayment",
            target_id=row.id,
            detail=f"outcome={outcome} refund_reference={p.refund_reference} receipt={row.mpesa_receipt_number} amount={row.amount}"
        )
        await db.commit()
        return {"id": row.id, "status": row.status, "resolved_at": row.resolved_at.isoformat()}


@app.post("/callbacks/stk-callback")
async def stk_callback(request: Request, x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")):
    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    parsed = mpesa.parse_stk_callback(body)
    if not parsed["checkout_request_id"] or parsed["result_code"] is None:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
    raw_payload = json.dumps(body, separators=(",", ":"), default=str)

    async def _hold_unapplied(db, reason: str, tx_id_for_hold=None):
        """Record money Safaricom reports received that can't be applied to this escrow, account for it in the
        ledger, alert once and acknowledge. Never refunded automatically.
        """

        phone = (
            mpesa.normalize_phone_number(str(parsed["phone_number"]))
            if parsed["phone_number"] is not None
            else None
        )
        row, created = await record_unapplied_payment(
            db,
            source="STK",
            provider_reference=parsed["checkout_request_id"],
            reason=reason,
            raw_payload=raw_payload,
            amount=parsed["amount"],
            phone_number=phone,
            mpesa_receipt_number=parsed["receipt_number"],
            escrow_transaction_id=tx_id_for_hold
        )
        await db.commit()
        if created:
            await alert_payout_issue(
                "UNAPPLIED_STK_PAYMENT",
                0,
                tx_id_for_hold if tx_id_for_hold is not None else 0,
                f"Safaricom reports a successful STK payment (checkout {parsed['checkout_request_id']}, receipt {parsed['receipt_number']}, amount {parsed['amount']}) that could not be applied: {reason}. Held for admin review; NOT refunded automatically."
            )
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    # Safaricom resends STK callbacks; the row lock closes the double-credit race.
    async with AsyncSessionLocal() as db:
        tx = await db.scalar(
            select(EscrowTransaction)
            .where(EscrowTransaction.mpesa_checkout_request_id == parsed["checkout_request_id"])
            .with_for_update()
        )
        _tx_id_for_hold = tx.id if tx is not None else None
        if not tx:
            if parsed["success"]:
                return await _hold_unapplied(db, "NO_MATCHING_ESCROW")
            return {"ResultCode": 0, "ResultDesc": "Accepted"}
        if (
            tx.mpesa_merchant_request_id
            and parsed["merchant_request_id"]
            and (tx.mpesa_merchant_request_id != parsed["merchant_request_id"])
        ):
            if parsed["success"]:
                return await _hold_unapplied(db, "MERCHANT_REQUEST_MISMATCH", tx.id)
            raise HTTPException(400, "M-Pesa merchant request mismatch")
        if tx.status != EscrowStatus.PENDING_PAYMENT:
            if (
                parsed["success"]
                and not (parsed["receipt_number"] and tx.mpesa_receipt_number == parsed["receipt_number"])
            ):
                return await _hold_unapplied(db, "ESCROW_NOT_AWAITING_PAYMENT", tx.id)
            return {"ResultCode": 0, "ResultDesc": "Already processed"}
        tx.stk_callback_received_at = _now()
        tx.stk_result_code = parsed["result_code"]
        tx.stk_result_desc = parsed["result_desc"]
        if parsed["success"]:
            receipt = parsed["receipt_number"]
            try:
                callback_amount = Decimal(str(parsed["amount"])) if parsed["amount"] is not None else None
            except InvalidOperation:
                callback_amount = None
            callback_phone = (
                mpesa.normalize_phone_number(str(parsed["phone_number"]))
                if parsed["phone_number"] is not None
                else None
            )
            if not receipt or callback_amount is None or callback_phone is None:
                await db.rollback()
                async with AsyncSessionLocal() as db2:
                    return await _hold_unapplied(db2, "INCOMPLETE_CALLBACK", _tx_id_for_hold)
            if callback_amount != tx.amount or callback_phone != tx.buyer_phone:
                await db.rollback()
                async with AsyncSessionLocal() as db2:
                    return await _hold_unapplied(db2, "AMOUNT_OR_PHONE_MISMATCH", _tx_id_for_hold)
            duplicate_receipt = await db.scalar(
                select(EscrowTransaction).where(
                    EscrowTransaction.mpesa_receipt_number == receipt,
                    EscrowTransaction.id != tx.id
                )
            )
            if duplicate_receipt:
                await db.rollback()
                async with AsyncSessionLocal() as db2:
                    return await _hold_unapplied(db2, "DUPLICATE_RECEIPT", _tx_id_for_hold)
            tx.mpesa_receipt_number = receipt
            tx.stk_amount = callback_amount
            tx.stk_phone_number = callback_phone
            tx.payment_method = "STK"
        recon = await db.scalar(
            select(ReconciliationRecord).where(
                ReconciliationRecord.provider == "MPESA_STK",
                ReconciliationRecord.provider_reference == parsed["checkout_request_id"]
            )
        )
        if not recon:
            db.add(
                ReconciliationRecord(
                    provider="MPESA_STK",
                    provider_reference=parsed["checkout_request_id"],
                    escrow_transaction_id=tx.id,
                    amount=tx.amount,
                    status="SUCCESS" if parsed["success"] else "FAILED",
                    raw_payload=raw_payload
                )
            )
        if parsed["success"]:
            tx.status = EscrowStatus.ESCROW_HELD
            tx.paid_at = _now()
            tx.qr_token = mpesa.generate_qr_token()
            tx.qr_view_token = mpesa.generate_qr_token()
            tx.qr_token_expires_at = mpesa.qr_token_expiry()
            if tx.order:
                tx.order.status = OrderStatus.PAID
            await post_ledger_transaction(
                db,
                reference=f"MPESA-HOLD-{tx.id}",
                idempotency_key=f"mpesa-hold-{tx.id}",
                description=f"Hold M-Pesa payment for escrow #{tx.id}",
                escrow_transaction_id=tx.id,
                debit_account_code="MPESA_CLEARING",
                credit_account_code="ESCROW_HOLDING",
                amount=tx.amount
            )
            qr_url = _buyer_qr_url(tx)
            message = f"Order {(tx.order.public_ref if tx.order else tx.id)}: payment confirmed and held in escrow. Your delivery QR expires in {mpesa.QR_TOKEN_VALIDITY_HOURS} hours."
            message += f" Open: {qr_url}" if qr_url else " Open the buyer app to display your delivery QR."
            await send_sms(db, tx.buyer_phone, message, f"qr-ready-buyer-{tx.id}")
            if tx.assigned_agent:
                await send_sms(
                    db,
                    tx.assigned_agent.phone_number,
                    f"Escrow delivery #{tx.id} is funded. Deliver the order and scan the buyer QR after delivery.",
                    f"qr-ready-agent-{tx.id}"
                )
        else:
            tx.status = EscrowStatus.PAYMENT_FAILED
            if tx.order:
                tx.order.status = OrderStatus.CANCELLED
        await db.commit()
        escrow_held = parsed["success"]
        tx_id = tx.id
    if escrow_held:
        await notify_seller_escrow_held_after_commit(tx_id)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


def _payout_attempt_from_originator(originator_id):
    """Parses 'escrow-payout-{payout_id}-{attempt}', the OriginatorConversationID we generate and persist before
    every send. Returns (payout_id, attempt) or None.
    """

    import re as _re
    m = _re.match("^escrow-payout-(\\d+)-(\\d+)$", originator_id or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


async def _handle_payout_result_callback(
    request: Request,
    x_mpesa_secret: Optional[str],
    provider_label: str
):
    """Shared handler for the B2C, Reversal and B2B Result callbacks: same envelope, same effect on the Payout.
    Success completes settlement; failure reverses the reservation.
    """

    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    parsed = mpesa.parse_payout_result_callback(body)
    conversation_id = parsed["conversation_id"]
    if not conversation_id:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
    async with AsyncSessionLocal() as db:
        # Locked: this and the queue-timeout callback can race, and without the lock a timeout could overwrite
        # a COMPLETED payout back to TIMEOUT.
        payout = None
        _orig = _payout_attempt_from_originator(parsed.get("originator_conversation_id"))
        if _orig:
            payout = await db.scalar(select(Payout).where(Payout.id == _orig[0]).with_for_update())
            if payout is not None and _orig[1] != payout.attempt_count:
                # Callback for an EARLIER attempt: must not touch the current one and must never be lost (a
                # late SUCCESS can mean a double payment).
                _cur_attempt = payout.attempt_count
                _po_id = payout.id
                _tx_id = payout.escrow_transaction_id
                _stale_ref = f"stale-{conversation_id}"
                _already = await db.scalar(
                    select(ReconciliationRecord).where(
                        ReconciliationRecord.provider == provider_label,
                        ReconciliationRecord.provider_reference == _stale_ref
                    )
                )
                if not _already:
                    db.add(
                        ReconciliationRecord(
                            provider=provider_label,
                            provider_reference=_stale_ref,
                            escrow_transaction_id=_tx_id,
                            amount=payout.amount,
                            status="STALE_ATTEMPT_" + str(parsed.get("outcome", "UNKNOWN")),
                            raw_payload=json.dumps(body, separators=(",", ":"), default=str)
                        )
                    )
                await db.commit()
                if parsed.get("outcome") != "FAILURE":
                    await alert_payout_issue(
                        "PAYOUT_STALE_ATTEMPT_RESULT",
                        _po_id,
                        _tx_id,
                        f"{provider_label} callback for attempt {_orig[1]} (outcome={parsed.get('outcome')}) arrived after the payout moved to attempt {_cur_attempt}; payout left unchanged. If SUCCESS, the payee may have been paid twice: check Safaricom records."
                    )
                return {"ResultCode": 0, "ResultDesc": "Accepted"}
        if payout is None:
            payout = await db.scalar(
                select(Payout).where(Payout.conversation_id == conversation_id).with_for_update()
            )
        elif payout.conversation_id is None:
            payout.conversation_id = conversation_id  # the callback beat the commit that stores it
        if not payout:
            # The callback can beat the commit that stores ConversationID; retry rather than acknowledge and
            # lose it.
            raise HTTPException(503, "Payout callback is not yet reconciled; retry the callback")
        tx = await db.get(EscrowTransaction, payout.escrow_transaction_id)
        # A missing or unreadable result code is UNKNOWN, not failure: the money may have moved. Touch
        # nothing, keep the raw payload, alert.
        if parsed.get("outcome") == "UNKNOWN":
            payout.result_desc = f"Unreadable result code in {provider_label} callback: {parsed.get('result_desc')}"
            recon_unknown = await db.scalar(
                select(ReconciliationRecord).where(
                    ReconciliationRecord.provider == provider_label,
                    ReconciliationRecord.provider_reference == conversation_id
                )
            )
            if not recon_unknown:
                db.add(
                    ReconciliationRecord(
                        provider=provider_label,
                        provider_reference=conversation_id,
                        escrow_transaction_id=tx.id,
                        amount=payout.amount,
                        status="UNKNOWN",
                        raw_payload=json.dumps(body, separators=(",", ":"), default=str)
                    )
                )
            await db.commit()
            await alert_payout_issue(
                "PAYOUT_RESULT_UNREADABLE",
                payout.id,
                tx.id,
                f"{provider_label} callback had a missing or unreadable result code; payout left unchanged, needs manual reconciliation"
            )
            return {"ResultCode": 0, "ResultDesc": "Accepted"}
        if payout.status == "COMPLETED":
            return {"ResultCode": 0, "ResultDesc": "Already processed"}
        if payout.status == "FAILED" and not parsed["success"]:
            # Reservation already reversed (failure callback, provider rejection or admin reconcile-timeout);
            # reversing again would double-count.
            await alert_payout_issue(
                "PAYOUT_LATE_FAILURE_CALLBACK",
                payout.id,
                payout.escrow_transaction_id,
                f"{provider_label} failure callback arrived for a payout already FAILED; ledger not touched"
            )
            return {"ResultCode": 0, "ResultDesc": "Already processed"}
        if payout.status == "FAILED" and parsed["success"]:
            # Admin already reversed this as unpaid but Safaricom now reports success: flag for a human, don't
            # re-finalize.
            await alert_payout_issue(
                "PAYOUT_LATE_SUCCESS_AFTER_RECONCILE",
                payout.id,
                payout.escrow_transaction_id,
                f"{provider_label} success callback arrived for a payout already reconciled as FAILED; needs manual review"
            )
            return {"ResultCode": 0, "ResultDesc": "Already processed"}
        # ConversationID identifies the payout but not the attempt. Cross-check the OriginatorConversationID
        # we stored so a stale or replayed callback isn't applied as current.
        callback_originator_id = parsed.get("originator_conversation_id")
        if (
            payout.originator_conversation_id
            and callback_originator_id
            and (callback_originator_id != payout.originator_conversation_id)
        ):
            await alert_payout_issue(
                "PAYOUT_ORIGINATOR_MISMATCH",
                payout.id,
                payout.escrow_transaction_id,
                f"{provider_label} callback OriginatorConversationID={callback_originator_id!r} does not match stored payout.originator_conversation_id={payout.originator_conversation_id!r}; ignoring this callback pending manual review"
            )
            return {"ResultCode": 0, "ResultDesc": "Accepted"}
        payout.result_code = parsed["result_code"]
        payout.result_desc = parsed["result_desc"]
        recon = await db.scalar(
            select(ReconciliationRecord).where(
                ReconciliationRecord.provider == provider_label,
                ReconciliationRecord.provider_reference == conversation_id
            )
        )
        if not recon:
            db.add(
                ReconciliationRecord(
                    provider=provider_label,
                    provider_reference=conversation_id,
                    escrow_transaction_id=tx.id,
                    amount=payout.amount,
                    status="SUCCESS" if parsed["success"] else "FAILED",
                    raw_payload=json.dumps(body, separators=(",", ":"), default=str)
                )
            )
        if parsed["success"]:
            if not parsed["transaction_id"]:
                raise HTTPException(400, f"Successful {provider_label} callback missing transaction receipt")
            # Safaricom is authoritative that the money moved, but a reported amount that differs from what we
            # asked to pay must reach a human.
            if (
                parsed["completed_amount"] is not None
                and Decimal(str(parsed["completed_amount"])) != payout.amount
            ):
                await alert_payout_issue(
                    "PAYOUT_AMOUNT_MISMATCH",
                    payout.id,
                    tx.id,
                    f"{provider_label} reported completed amount {parsed['completed_amount']} but payout was for {payout.amount}"
                )
            payout.status = "COMPLETED"
            payout.transaction_id = parsed["transaction_id"]
            payout.completed_at = _now()
            await _finalize_escrow_after_payouts(db, tx)
        else:
            payout.status = "FAILED"
            payable = (
                "BUYER_REFUND_PAYABLE"
                if payout.purpose == "REFUND"
                else "RIDER_PAYABLE"
                if tx.payee_role == PayeeRole.RIDER
                else "SELLER_PAYABLE"
            )
            await post_ledger_transaction(
                db,
                reference=f"{provider_label}-REVERSE-{payout.id}-{payout.attempt_count}",
                idempotency_key=f"{provider_label.lower()}-reverse-{payout.id}-{payout.attempt_count}",
                description="Reverse failed external payout reservation",
                escrow_transaction_id=tx.id,
                debit_account_code="MPESA_CLEARING",
                credit_account_code=payable,
                amount=payout.amount
            )
            tx.status = (
                EscrowStatus.REFUND_FAILED
                if payout.purpose == "REFUND"
                else EscrowStatus.PAYOUT_FAILED
            )
        await db.commit()
        tx_id = tx.id
        should_notify = parsed["success"]
    if should_notify:
        await notify_escrow_finalized_after_commit(tx_id)
        await notify_seller_escrow_finalized_after_commit(tx_id)
        await notify_rider_escrow_finalized_after_commit(tx_id)
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


async def _handle_payout_timeout(request: Request, x_mpesa_secret: Optional[str], provider_label: str):
    _require_callback_secret(x_mpesa_secret)
    body = await request.json()
    conversation_id = body.get("Result", {}).get("ConversationID") or body.get("ConversationID")
    result_desc = str(body.get("Result", {}).get("ResultDesc") or f"{provider_label} queue timeout")
    if conversation_id:
        async with AsyncSessionLocal() as db:
            # Same lock and reason as _handle_payout_result_callback.
            _orig = _payout_attempt_from_originator(
                body.get("Result", {}).get("OriginatorConversationID") or body.get("OriginatorConversationID")
            )
            payout = None
            if _orig:
                payout = await db.scalar(select(Payout).where(Payout.id == _orig[0]).with_for_update())
                if payout is not None and _orig[1] != payout.attempt_count:
                    # A queue-timeout for an EARLIER attempt says nothing about the current one.
                    logging.warning(
                        "%s timeout for earlier attempt %s of payout %s ignored (current attempt %s)",
                        provider_label,
                        _orig[1],
                        payout.id,
                        payout.attempt_count
                    )
                    await db.commit()
                    payout = None
                    conversation_id = None
            if payout is None and conversation_id:
                payout = await db.scalar(
                    select(Payout).where(Payout.conversation_id == conversation_id).with_for_update()
                )
            elif payout is not None and payout.conversation_id is None and conversation_id:
                payout.conversation_id = conversation_id
            if payout and payout.status not in {"COMPLETED", "FAILED"}:
                payout.status = "TIMEOUT"
                payout.timed_out_at = _now()
                payout.result_desc = result_desc
                tx = await db.get(EscrowTransaction, payout.escrow_transaction_id)
                if tx:
                    tx.status = (
                        EscrowStatus.REFUND_INITIATED
                        if payout.purpose == "REFUND"
                        else EscrowStatus.PAYOUT_FAILED
                    )
                await db.commit()
                await alert_payout_issue(
                    "PAYOUT_TIMEOUT",
                    payout.id,
                    payout.escrow_transaction_id,
                    f"{provider_label} queue timeout callback received: {result_desc}"
                )
    # Log a redacted summary, not the raw body (it can contain recipient names and phone numbers).
    logging.warning(
        "%s timeout: conversation_id=%s result_desc=%s",
        provider_label,
        conversation_id,
        result_desc
    )
    return {"ResultCode": 0, "ResultDesc": "Accepted"}


@app.post("/callbacks/b2c-callback")
async def b2c_callback(request: Request, x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")):
    return await _handle_payout_result_callback(request, x_mpesa_secret, "MPESA_B2C")


@app.post("/callbacks/b2c-timeout")
async def b2c_timeout(request: Request, x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")):
    return await _handle_payout_timeout(request, x_mpesa_secret, "MPESA_B2C")


@app.post("/callbacks/reversal-callback")
async def reversal_callback(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    return await _handle_payout_result_callback(request, x_mpesa_secret, "MPESA_REVERSAL")


@app.post("/callbacks/reversal-timeout")
async def reversal_timeout(
    request: Request,
    x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")
):
    return await _handle_payout_timeout(request, x_mpesa_secret, "MPESA_REVERSAL")


@app.post("/callbacks/b2b-callback")
async def b2b_callback(request: Request, x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")):
    return await _handle_payout_result_callback(request, x_mpesa_secret, "MPESA_B2B")


@app.post("/callbacks/b2b-timeout")
async def b2b_timeout(request: Request, x_mpesa_secret: Optional[str]=Header(None, alias="X-Mpesa-Secret")):
    return await _handle_payout_timeout(request, x_mpesa_secret, "MPESA_B2B")


@app.get("/admin/ledger/accounts", dependencies=[Depends(verify_admin_key)])
async def ledger_accounts():
    async with AsyncSessionLocal() as db:
        accounts = (await db.execute(select(LedgerAccount))).scalars().all()
        out = []
        for account in accounts:
            debit = await db.scalar(
                select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                    LedgerEntry.account_id == account.id,
                    LedgerEntry.direction == LedgerEntryDirection.DEBIT
                )
            )
            credit = await db.scalar(
                select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                    LedgerEntry.account_id == account.id,
                    LedgerEntry.direction == LedgerEntryDirection.CREDIT
                )
            )
            out.append(
                {"code": account.code, "name": account.name, "debits": str(debit), "credits": str(credit)}
            )
        return out


@app.get("/admin/reconciliation", dependencies=[Depends(verify_admin_key)])
async def reconciliation():
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(ReconciliationRecord).order_by(ReconciliationRecord.id.desc()).limit(100)
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "provider": r.provider,
                "reference": r.provider_reference,
                "escrow_id": r.escrow_transaction_id,
                "amount": str(r.amount) if r.amount is not None else None,
                "status": r.status
            } for r in rows
        ]


@app.get("/admin/payouts", dependencies=[Depends(verify_admin_key)])
async def admin_payouts():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(Payout).order_by(Payout.id.desc()).limit(200))).scalars().all()
        # Mask phones, except for B2B payouts: recipient_phone then holds a business shortcode, not PII.
        return [
            {
                "id": p.id,
                "escrow_id": p.escrow_transaction_id,
                "purpose": p.purpose,
                "recipient": p.recipient_phone if p.method == "B2B" else mpesa.mask_phone(p.recipient_phone),
                "amount": str(p.amount),
                "status": p.status,
                "conversation_id": p.conversation_id,
                "transaction_id": p.transaction_id
            } for p in rows
        ]


async def _reverse_settlement_reservation(db, payout, tx):
    """Reverse the settlement reservation exactly once. A payout reserves its payable account before any provider
    request, so a TIMEOUT reconciled as NOT PAID must release it before becoming retryable.
    """

    payable = (
        "BUYER_REFUND_PAYABLE"
        if payout.purpose == "REFUND"
        else "RIDER_PAYABLE"
        if tx.payee_role == PayeeRole.RIDER
        else "SELLER_PAYABLE"
    )
    attempt = payout.attempt_count
    reference = f"MPESA-SETTLE-REVERSE-{payout.id}-{attempt}"
    idempotency_key = f"mpesa-settle-reverse-{payout.id}-{attempt}"
    existing = await db.scalar(
        select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key)
    )
    if existing:
        return existing
    return await post_ledger_transaction(
        db,
        reference=reference,
        idempotency_key=idempotency_key,
        description="Reverse unreconciled settlement reservation after provider-confirmed non-payment",
        escrow_transaction_id=tx.id,
        debit_account_code="MPESA_CLEARING",
        credit_account_code=payable,
        amount=payout.amount
    )


@app.post("/admin/payouts/{payout_id}/reconcile-timeout")
async def reconcile_timeout_payout(
    payout_id: int,
    request: Request,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    """Reconcile a TIMEOUT as not paid: reverse the reservation, then make it retryable.

    The caller must confirm Safaricom shows no completed transaction. The reversal is committed before FAILED
    so a retry can't double-reserve.
    """

    async with AsyncSessionLocal() as db:
        payout = await db.scalar(select(Payout).where(Payout.id == payout_id).with_for_update())
        if not payout:
            raise HTTPException(404, "Payout not found")
        if payout.status != "TIMEOUT":
            raise HTTPException(409, "Only TIMEOUT payouts can be reconciled")
        tx = await db.get(EscrowTransaction, payout.escrow_transaction_id)
        if not tx:
            raise HTTPException(409, "Escrow transaction not found")
        try:
            await _reverse_settlement_reservation(db, payout, tx)
            payout.status = "FAILED"
            payout.result_desc = (
                payout.result_desc or ""
            ) + " | Admin reconciled: provider confirmed payout was not completed; reservation reversed; safe to retry."
            await record_audit(
                db,
                admin_actor,
                "payout.reconcile_timeout",
                target_type="Payout",
                target_id=payout.id,
                detail=f"escrow_id={tx.id} admin confirmed provider shows no completed transaction"
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise HTTPException(
                500,
                "Could not reverse the B2C settlement reservation; payout remains TIMEOUT and is not retryable."
            )
        return {
            "payout_id": payout.id,
            "status": payout.status,
            "retryable": True,
            "reservation_reversed": True
        }


class ConfirmProviderCompletedRequest(BaseModel):
    transaction_id: str = Field(min_length=1, max_length=100)
    note: Optional[str] = Field(default=None, max_length=1000)


@app.post("/admin/payouts/{payout_id}/confirm-provider-completed")
async def confirm_provider_completed_payout(
    payout_id: int,
    p: ConfirmProviderCompletedRequest,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    """Counterpart to reconcile-timeout: record that Safaricom did complete the payout.

    The caller supplies the real transaction ID from Safaricom's records. The reservation made at submission
    is confirmed as settled, not reversed.
    """

    async with AsyncSessionLocal() as db:
        payout = await db.scalar(select(Payout).where(Payout.id == payout_id).with_for_update())
        if not payout:
            raise HTTPException(404, "Payout not found")
        if payout.status not in {"TIMEOUT", "SUBMITTING", "INITIATED"}:
            raise HTTPException(409, "Only a TIMEOUT/SUBMITTING/INITIATED payout can be confirmed completed")
        tx = await db.get(EscrowTransaction, payout.escrow_transaction_id)
        if not tx:
            raise HTTPException(409, "Escrow transaction not found")
        clash = await db.scalar(
            select(Payout).where(Payout.transaction_id == p.transaction_id, Payout.id != payout.id)
        )
        if clash:
            raise HTTPException(
                409,
                f"Transaction ID {p.transaction_id!r} is already recorded against payout #{clash.id}."
            )
        payout.status = "COMPLETED"
        payout.transaction_id = p.transaction_id
        payout.completed_at = _now()
        payout.result_desc = (payout.result_desc or "") + f" | Admin confirmed COMPLETED from provider records: {p.note or ''}".rstrip(
            
        )
        await _finalize_escrow_after_payouts(db, tx)
        await record_audit(
            db,
            admin_actor,
            "payout.confirm_provider_completed",
            target_type="Payout",
            target_id=payout.id,
            detail=f"escrow_id={tx.id} transaction_id={p.transaction_id} note={p.note!r}"
        )
        await db.commit()
        tx_id = tx.id
    await notify_escrow_finalized_after_commit(tx_id)
    await notify_seller_escrow_finalized_after_commit(tx_id)
    await notify_rider_escrow_finalized_after_commit(tx_id)
    return {"payout_id": payout_id, "status": "COMPLETED", "transaction_id": p.transaction_id}


@app.post("/admin/payouts/{payout_id}/retry")
async def retry_payout(
    payout_id: int,
    request: Request,
    admin_actor: str=Depends(require_admin_role(AdminRole.FINANCE_ADMIN.value))
):
    async with AsyncSessionLocal() as db:
        # Locked so an admin retry can't race the background sweep on the same payout.
        payout = await db.scalar(select(Payout).where(Payout.id == payout_id).with_for_update())
        if not payout:
            raise HTTPException(404, "Payout not found")
        if payout.status not in ["FAILED"]:
            raise HTTPException(409, "Only failed payouts can be retried")
        ok = await initiate_payout_for_record(db, payout, request.app.state.http_client)
        await record_audit(
            db,
            admin_actor,
            "payout.retry",
            target_type="Payout",
            target_id=payout.id,
            detail=f"initiated={ok}"
        )
        await db.commit()
        return {"payout_id": payout.id, "status": payout.status, "initiated": ok}


@app.post("/ussd/callback")
async def ussd_callback(
    request: Request,
    p: USSDRequest,
    x_ussd_secret: Optional[str]=Header(None, alias="X-Ussd-Secret")
):
    """Stateful USSD handler. The next expected input is stored in USSDSession.state, so retries and submenu
    navigation are recoverable; for a state expecting an order reference the last path segment is used.
    """

    if not mpesa.USSD_CALLBACK_SECRET:
        raise HTTPException(503, "USSD callback authentication is not configured.")
    if not mpesa.verify_ussd_secret(x_ussd_secret):
        raise HTTPException(401, "Invalid USSD secret")
    phone = mpesa.normalize_phone_number(p.phoneNumber)
    if not phone:
        return "END Invalid phone number."

    text = (p.text or "").strip()
    parts = [part.strip() for part in text.split("*") if part.strip()]

    async with AsyncSessionLocal() as db:
        # Locked: gateways send overlapping or duplicate requests for one session, which would corrupt the
        # menu state machine.
        session = await db.scalar(
            select(USSDSession).where(USSDSession.session_id == p.sessionId).with_for_update()
        )
        if not session:
            session = USSDSession(session_id=p.sessionId, phone_number=phone, state="MAIN")
            db.add(session)
            await db.flush()
        else:
            if session.phone_number != phone:
                raise HTTPException(403, "USSD session phone mismatch")
            if _now() - _as_aware(session.updated_at) > timedelta(seconds=USSD_SESSION_TTL_SECONDS):
                # Expire stale sessions and restart at the main menu rather than resume an abandoned step
                # (e.g. mid-payment-confirmation).
                session.state = "MAIN"
                session.order_ref = None

        session.updated_at = _now()

        # Retries may resend the whole path; for single-value states the last segment is the value.
        value = parts[-1] if parts else ""
        state = session.state

        if state == "MAIN":
            if not value:
                await db.commit()
                return "CON Escrow\n1. Check order\n2. Pay order\n3. Cancel"
            if value == "1":
                session.state = "AWAITING_ORDER_REF_CHECK"
                await db.commit()
                return "CON Enter order reference:"
            if value == "2":
                session.state = "AWAITING_ORDER_REF_PAY"
                await db.commit()
                return "CON Enter order reference:"
            if value == "3":
                session.state = "AWAITING_ORDER_REF_CANCEL"
                await db.commit()
                return "CON Enter order reference:"
            await db.commit()
            return "END Invalid option."

        if state in {"AWAITING_ORDER_REF_CHECK", "AWAITING_ORDER_REF_PAY", "AWAITING_ORDER_REF_CANCEL"}:
            if not value:
                await db.commit()
                return "CON Enter order reference:"

            session.order_ref = value.upper()[:40]
            order = await db.scalar(
                select(Order).where(Order.public_ref == session.order_ref, Order.buyer_phone == phone)
            )
            if not order:
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return "END Order not found."

            if state == "AWAITING_ORDER_REF_CHECK":
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return f"END {order.public_ref}: {order.status.value}. Amount KES {order.amount + order.delivery_fee}."

            if state == "AWAITING_ORDER_REF_CANCEL":
                if not order.escrow:
                    session.state = "MAIN"
                    session.order_ref = None
                    await db.commit()
                    return "END This order can no longer be cancelled."
                # PENDING_PAYMENT with a checkout ID means an STK prompt is (or may be) on the phone;
                # cancelling now invites paying anyway.
                if (
                    order.escrow.status == EscrowStatus.PENDING_PAYMENT
                    and order.escrow.mpesa_checkout_request_id
                ):
                    session.state = "MAIN"
                    session.order_ref = None
                    await db.commit()
                    return "END Your payment is still being processed. Please wait a few minutes and check the order status before trying to cancel."
                # Atomic conditional update: cancel only if still PENDING_PAYMENT at write time, so a landing
                # STK payment isn't raced (same as confirm_delivery's QR claim).
                claimed = await db.execute(
                    update(EscrowTransaction)
                    .where(
                        EscrowTransaction.id == order.escrow.id,
                        EscrowTransaction.status == EscrowStatus.PENDING_PAYMENT
                    )
                    .values(status=EscrowStatus.PAYMENT_FAILED)
                )
                if claimed.rowcount != 1:
                    # Already resolved, or a payment just landed: never overwrite with a plain write.
                    session.state = "MAIN"
                    session.order_ref = None
                    await db.commit()
                    return "END This order can no longer be cancelled."
                order.status = OrderStatus.CANCELLED
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return "END Order cancelled."

            # M-Pesa asks for the PIN on the handset, so the session only tracks that the STK request was
            # sent.
            if not order.escrow:
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return "END Order not found."
            # Lock the escrow before deciding to push, so overlapping requests can't both see no checkout ID
            # and both push.
            await db.execute(
                select(EscrowTransaction.id).where(EscrowTransaction.id == order.escrow.id).with_for_update()
            )
            await db.refresh(order.escrow)
            if order.escrow.status != EscrowStatus.PENDING_PAYMENT:
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return f"END Payment is {order.escrow.status.value}."
            # PENDING_PAYMENT doesn't mean no STK was initiated: one may be in flight. Without this check a
            # re-entered Pay flow fires a duplicate push.
            if order.escrow.mpesa_checkout_request_id:
                session.state = "AWAITING_PAYMENT_CALLBACK"
                await db.commit()
                return "CON Payment already in progress. Complete the M-Pesa PIN prompt, then retry to check status."
            try:
                stk = await mpesa.initiate_stk_push(
                    request.app.state.http_client,
                    phone,
                    _to_whole_kes(order.amount + order.delivery_fee),
                    f"ESC{order.escrow.id}",
                    f"Order {order.public_ref}"
                )
                order.escrow.mpesa_checkout_request_id = stk.get("CheckoutRequestID")
                order.escrow.mpesa_merchant_request_id = stk.get("MerchantRequestID")
                if not order.escrow.mpesa_checkout_request_id:
                    raise RuntimeError("M-Pesa did not return a CheckoutRequestID")
                session.state = "AWAITING_PAYMENT_CALLBACK"
                await db.commit()
                return "END Payment request sent to your M-Pesa phone. Enter your PIN on the M-Pesa prompt to continue."
            except (httpx.HTTPStatusError, mpesa.MpesaRejected) as exc:
                # Confirmed rejection: safe to tell the buyer to try again (same as the /orders STK path).
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return "END M-Pesa rejected the payment request. Please try again."
            except Exception as exc:
                # Unknown outcome: mark STK_INITIATION_UNKNOWN (never PAYMENT_FAILED) so a USSD retry is
                # blocked instead of firing a duplicate push. Needs manual reconciliation.
                order.escrow.status = EscrowStatus.STK_INITIATION_UNKNOWN
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                await alert_payout_issue(
                    "STK_INITIATION_UNKNOWN",
                    0,
                    order.escrow.id,
                    f"USSD order_id={order.id} buyer={mpesa.mask_phone(phone)}: {exc}"
                )
                return "END We could not confirm whether the M-Pesa payment request reached Safaricom. Please wait a few minutes and dial in to check the order status before trying again. Do not pay for this order twice."

        if state == "AWAITING_PAYMENT_CALLBACK":
            # The callback updates the escrow asynchronously; a repeat request can inspect state without
            # resending STK.
            if not session.order_ref:
                session.state = "MAIN"
                await db.commit()
                return "END Payment session expired."
            order = await db.scalar(
                select(Order).where(Order.public_ref == session.order_ref, Order.buyer_phone == phone)
            )
            if not order or not order.escrow:
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return "END Order not found."
            if order.escrow.status == EscrowStatus.ESCROW_HELD:
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return "END Payment confirmed. Your money is now held in escrow."
            if order.escrow.status == EscrowStatus.STK_INITIATION_UNKNOWN:
                session.state = "MAIN"
                session.order_ref = None
                await db.commit()
                return "END We still can't confirm your last payment request. Please wait a few minutes before trying again. Do not pay for this order twice."
            await db.commit()
            return "CON Payment is still pending. Complete the M-Pesa PIN prompt, then retry."

        session.state = "MAIN"
        session.order_ref = None
        await db.commit()
        return "END Session reset. Please try again."
