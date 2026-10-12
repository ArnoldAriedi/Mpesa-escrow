import re
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from escrow_models import (
    AsyncSessionLocal, Delivery, DeliveryAgent, DeliveryStatus, Dispute, DisputeStatus,
    EscrowStatus, EscrowTransaction, LedgerEntry, LedgerEntryDirection,
    LedgerTransaction, Order, OrderStatus, PayeeRole, Payout, Seller, SellerKycStatus,
    post_ledger_transaction, hash_api_key,
)
import escrow_main
from escrow_main import reconcile_timeout_payout, resolve_dispute


ROOT = Path(__file__).resolve().parents[1]


def _squash(text):
    """Source-text checks must not depend on formatting."""
    return re.sub(r"\s+", "", text)


def test_ussd_callback_accepts_request_for_stk_flow():
    source = _squash((ROOT / "escrow_main.py").read_text())
    assert "asyncdefussd_callback(request:Request,p:USSDRequest" in source
    assert "request.app.state.http_client" in source


def test_timeout_has_explicit_reconciliation_path():
    source = (ROOT / "escrow_main.py").read_text()
    assert '/admin/payouts/{payout_id}/reconcile-timeout' in source
    assert re.search(r'\w+\.status!="TIMEOUT"', _squash(source))



@pytest.mark.asyncio
async def test_reconcile_timeout_reverses_reservation_before_retryable(db):
    escrow = EscrowTransaction(
        buyer_phone="254700000001", payee_phone="254700000002",
        payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
        status=EscrowStatus.DELIVERY_CONFIRMED,
        buyer_access_token="timeout-reconcile-token",
    )
    db.add(escrow)
    await db.flush()
    payout = Payout(
        escrow_transaction_id=escrow.id, purpose="RELEASE",
        recipient_phone=escrow.payee_phone, amount=Decimal("500.00"),
        status="TIMEOUT", attempt_count=1, result_desc="provider request timed out",
    )
    db.add(payout)
    await db.flush()
    await post_ledger_transaction(
        db, reference=f"MPESA-SETTLE-{payout.id}-1",
        idempotency_key=f"mpesa-settle-{payout.id}-1",
        description="Original B2C reservation",
        escrow_transaction_id=escrow.id,
        debit_account_code="RIDER_PAYABLE",
        credit_account_code="MPESA_CLEARING",
        amount=payout.amount,
    )
    await db.commit()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=None)))
    response = await reconcile_timeout_payout(payout.id, request, admin_actor="test-admin")
    assert response["status"] == "FAILED"
    assert response["reservation_reversed"] is True

    async with AsyncSessionLocal() as check:
        current = await check.get(Payout, payout.id)
        assert current.status == "FAILED"
        reversals = (await check.execute(
            select(LedgerTransaction).where(
                LedgerTransaction.idempotency_key == f"mpesa-settle-reverse-{payout.id}-1"
            )
        )).scalars().all()
        assert len(reversals) == 1
        entries = (await check.execute(
            select(LedgerEntry).where(LedgerEntry.ledger_transaction_id == reversals[0].id)
        )).scalars().all()
        assert {(e.direction, e.amount) for e in entries} == {
            (LedgerEntryDirection.DEBIT, Decimal("500.00")),
            (LedgerEntryDirection.CREDIT, Decimal("500.00")),
        }


@pytest.mark.asyncio
async def test_reconcile_timeout_is_idempotent_for_existing_reversal(db):
    escrow = EscrowTransaction(
        buyer_phone="254700000003", payee_phone="254700000004",
        payee_role=PayeeRole.RIDER, amount=Decimal("300.00"),
        status=EscrowStatus.DELIVERY_CONFIRMED,
        buyer_access_token="timeout-reconcile-idempotent-token",
    )
    db.add(escrow)
    await db.flush()
    payout = Payout(
        escrow_transaction_id=escrow.id, purpose="RELEASE",
        recipient_phone=escrow.payee_phone, amount=Decimal("300.00"),
        status="TIMEOUT", attempt_count=1,
    )
    db.add(payout)
    await db.flush()
    await post_ledger_transaction(
        db, reference=f"MPESA-SETTLE-{payout.id}-1",
        idempotency_key=f"mpesa-settle-{payout.id}-1",
        description="Original B2C reservation",
        escrow_transaction_id=escrow.id,
        debit_account_code="RIDER_PAYABLE", credit_account_code="MPESA_CLEARING",
        amount=payout.amount,
    )
    await post_ledger_transaction(
        db, reference=f"MPESA-SETTLE-REVERSE-{payout.id}-1",
        idempotency_key=f"mpesa-settle-reverse-{payout.id}-1",
        description="Existing reversal",
        escrow_transaction_id=escrow.id,
        debit_account_code="MPESA_CLEARING", credit_account_code="RIDER_PAYABLE",
        amount=payout.amount,
    )
    await db.commit()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=None)))
    response = await reconcile_timeout_payout(payout.id, request, admin_actor="test-admin")
    assert response["status"] == "FAILED"
    async with AsyncSessionLocal() as check:
        count = await check.scalar(
            select(func.count(LedgerTransaction.id)).where(
                LedgerTransaction.idempotency_key == f"mpesa-settle-reverse-{payout.id}-1"
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_partial_dispute_returns_both_payout_results(monkeypatch):
    async with AsyncSessionLocal() as setup:
        order = Order(
            public_ref="ORD-PARTIAL-TEST", buyer_phone="254700000005",
            amount=Decimal("1000.00"), delivery_fee=Decimal("0.00"),
            delivery_address="Test address", status=OrderStatus.PAID,
        )
        setup.add(order)
        await setup.flush()
        tx = EscrowTransaction(
            order_id=order.id, buyer_phone=order.buyer_phone,
            payee_phone="254700000006", payee_role=PayeeRole.RIDER,
            amount=Decimal("1000.00"), status=EscrowStatus.DISPUTED,
            buyer_access_token="partial-dispute-token",
        )
        setup.add(tx)
        await setup.flush()
        dispute = Dispute(
            order_id=order.id, opened_by_phone=order.buyer_phone,
            reason="Partial resolution test", status=DisputeStatus.OPEN,
        )
        setup.add(dispute)
        await setup.commit()
        dispute_id = dispute.id

    outcomes = {"REFUND": False, "RELEASE": True}

    async def fake_initiate(db, payout, http_client):
        payout.status = "FAILED" if not outcomes[payout.purpose] else "INITIATED"
        return outcomes[payout.purpose]

    monkeypatch.setattr(escrow_main, "initiate_payout_for_record", fake_initiate)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=None)))
    response = await resolve_dispute(
        dispute_id,
        escrow_main.ResolveDisputeRequest(decision="PARTIAL", refund_amount=Decimal("400.00")),
        request,
        admin_actor="test-admin",
    )

    assert response["escrow_status"] == EscrowStatus.PAYOUT_FAILED.value
    assert len(response["payouts"]) == 2
    by_purpose = {item["purpose"]: item for item in response["payouts"]}
    assert by_purpose["REFUND"]["initiated"] is False
    assert by_purpose["REFUND"]["payout_status"] == "FAILED"
    assert by_purpose["RELEASE"]["initiated"] is True
    assert by_purpose["RELEASE"]["payout_status"] == "INITIATED"
    assert by_purpose["REFUND"]["payout_id"] != by_purpose["RELEASE"]["payout_id"]


def test_production_mode_rejects_wildcard_cors():
    with pytest.raises(RuntimeError, match="CORS_ALLOWED_ORIGINS"):
        escrow_main._validate_production_config(
            db_auto_create=False,
            database_url="postgresql+asyncpg://user:pw@host/db",
            cors_origins="*",
        )


def test_production_mode_accepts_explicit_cors_allowlist():
    # Should not raise.
    escrow_main._validate_production_config(
        db_auto_create=False,
        database_url="postgresql+asyncpg://user:pw@host/db",
        cors_origins="https://app.example.com,https://admin.example.com",
    )


def test_dev_mode_does_not_enforce_cors_allowlist():
    # DB_AUTO_CREATE=true (dev/test mode) should not enforce the production
    # CORS restriction, matching the existing sqlite/dev-friendly test setup.
    escrow_main._validate_production_config(
        db_auto_create=True, database_url="sqlite+aiosqlite:///x.db", cors_origins="*",
    )


@pytest.mark.asyncio
async def test_b2c_payout_request_includes_unique_originator_conversation_id(db, monkeypatch):
    escrow = EscrowTransaction(
        buyer_phone="254700000007", payee_phone="254700000008",
        payee_role=PayeeRole.RIDER, amount=Decimal("250.00"),
        status=EscrowStatus.DELIVERY_CONFIRMED,
        buyer_access_token="originator-id-token",
    )
    db.add(escrow)
    await db.flush()
    payout = Payout(
        escrow_transaction_id=escrow.id, purpose="RELEASE",
        recipient_phone=escrow.payee_phone, amount=Decimal("250.00"),
        status="PENDING", attempt_count=0,
    )
    db.add(payout)
    await db.commit()

    captured = {}

    async def fake_initiate_b2c_payout(client, phone_number, amount, remarks, originator_conversation_id, occasion=""):
        # The value must already be persisted on the payout row before the
        # provider call is made, and must be unique per attempt.
        captured["sent_id"] = originator_conversation_id
        return {"ConversationID": "conv-1", "OriginatorConversationID": originator_conversation_id}

    monkeypatch.setattr(escrow_main.mpesa, "initiate_b2c_payout", fake_initiate_b2c_payout)
    ok = await escrow_main.initiate_payout_for_record(db, payout, http_client=None)
    assert ok is True
    assert captured["sent_id"] == f"escrow-payout-{payout.id}-1"
    assert payout.originator_conversation_id == f"escrow-payout-{payout.id}-1"


@pytest.mark.asyncio
async def test_confirm_delivery_notifies_buyer(db, monkeypatch):
    order = Order(
        public_ref="ORD-NOTIFY-DELIVERY", buyer_phone="254700000009",
        amount=Decimal("200.00"), delivery_fee=Decimal("0.00"),
        delivery_address="Test address", status=OrderStatus.OUT_FOR_DELIVERY,
    )
    db.add(order)
    await db.flush()
    tx = EscrowTransaction(
        order_id=order.id, buyer_phone=order.buyer_phone,
        payee_phone="254700000010", payee_role=PayeeRole.RIDER,
        amount=Decimal("200.00"), status=EscrowStatus.RELEASED,
        buyer_access_token="notify-delivery-token",
    )
    db.add(tx)
    await db.commit()

    sent = []

    async def fake_send_sms(notification_db, phone, message, idempotency_key):
        sent.append((phone, idempotency_key))
        return SimpleNamespace()

    monkeypatch.setattr(escrow_main, "send_sms", fake_send_sms)
    await escrow_main.notify_delivery_confirmed_after_commit(tx.id)
    assert (order.buyer_phone, f"delivery-confirmed-buyer-{tx.id}") in sent


@pytest.mark.asyncio
async def test_seller_notified_on_escrow_held_without_qr(db, monkeypatch):
    seller = Seller(
        business_name="Test Shop", phone_number="254700000020",
        payout_phone="254700000020",
    )
    db.add(seller)
    await db.flush()
    order = Order(
        public_ref="ORD-NOTIFY-SELLER", buyer_phone="254700000021",
        seller_id=seller.id, amount=Decimal("300.00"), delivery_fee=Decimal("0.00"),
        delivery_address="Test address", status=OrderStatus.PAID,
    )
    db.add(order)
    await db.flush()
    tx = EscrowTransaction(
        order_id=order.id, buyer_phone=order.buyer_phone,
        payee_phone=seller.payout_phone, payee_role=PayeeRole.SELLER,
        amount=Decimal("300.00"), status=EscrowStatus.ESCROW_HELD,
        buyer_access_token="notify-seller-token",
        qr_token="qr-notify-seller",
    )
    db.add(tx)
    await db.commit()

    sent = []

    async def fake_send_sms(notification_db, phone, message, idempotency_key):
        sent.append((phone, message, idempotency_key))
        return SimpleNamespace()

    monkeypatch.setattr(escrow_main, "send_sms", fake_send_sms)
    await escrow_main.notify_seller_escrow_held_after_commit(tx.id)

    assert len(sent) == 1
    phone, message, idem_key = sent[0]
    assert phone == seller.phone_number
    assert idem_key == f"escrow-held-seller-{tx.id}"
    # The seller only gets a plain confirmation — never the QR token/link.
    assert tx.qr_token not in message
    assert "qr" not in message.lower()


@pytest.mark.asyncio
async def test_seller_notified_on_release_gets_no_qr(db, monkeypatch):
    seller = Seller(
        business_name="Test Shop 2", phone_number="254700000030",
        payout_phone="254700000030",
    )
    db.add(seller)
    await db.flush()
    order = Order(
        public_ref="ORD-NOTIFY-SELLER-RELEASE", buyer_phone="254700000031",
        seller_id=seller.id, amount=Decimal("400.00"), delivery_fee=Decimal("0.00"),
        delivery_address="Test address", status=OrderStatus.COMPLETED,
    )
    db.add(order)
    await db.flush()
    tx = EscrowTransaction(
        order_id=order.id, buyer_phone=order.buyer_phone,
        payee_phone=seller.payout_phone, payee_role=PayeeRole.SELLER,
        amount=Decimal("400.00"), status=EscrowStatus.RELEASED,
        buyer_access_token="notify-seller-release-token",
        qr_token="qr-notify-seller-release",
    )
    db.add(tx)
    await db.commit()

    sent = []

    async def fake_send_sms(notification_db, phone, message, idempotency_key):
        sent.append((phone, message, idempotency_key))
        return SimpleNamespace()

    monkeypatch.setattr(escrow_main, "send_sms", fake_send_sms)
    await escrow_main.notify_seller_escrow_finalized_after_commit(tx.id)

    assert len(sent) == 1
    phone, message, idem_key = sent[0]
    assert phone == seller.phone_number
    assert idem_key == f"escrow-finalized-seller-released-{tx.id}"
    assert "400.00" in message
    assert tx.qr_token not in message
    assert "qr" not in message.lower()


@pytest.mark.asyncio
async def test_seller_not_notified_when_rider_is_payee_but_order_completes(db, monkeypatch):
    seller = Seller(
        business_name="Test Shop 3", phone_number="254700000032",
        payout_phone="254700000032",
    )
    db.add(seller)
    await db.flush()
    order = Order(
        public_ref="ORD-NOTIFY-SELLER-RIDER", buyer_phone="254700000033",
        seller_id=seller.id, amount=Decimal("250.00"), delivery_fee=Decimal("0.00"),
        delivery_address="Test address", status=OrderStatus.COMPLETED,
    )
    db.add(order)
    await db.flush()
    tx = EscrowTransaction(
        order_id=order.id, buyer_phone=order.buyer_phone,
        payee_phone="254700000034", payee_role=PayeeRole.RIDER,
        amount=Decimal("250.00"), status=EscrowStatus.RELEASED,
        buyer_access_token="notify-seller-rider-token",
    )
    db.add(tx)
    await db.commit()

    sent = []

    async def fake_send_sms(notification_db, phone, message, idempotency_key):
        sent.append((phone, message, idempotency_key))
        return SimpleNamespace()

    monkeypatch.setattr(escrow_main, "send_sms", fake_send_sms)
    await escrow_main.notify_seller_escrow_finalized_after_commit(tx.id)

    # The seller still gets a completion notice, but it must not claim they
    # were paid, since the rider was the actual payee in this scenario.
    assert len(sent) == 1
    assert "released to your payout account" not in sent[0][1]


@pytest.mark.asyncio
async def test_finalize_notification_fires_only_for_terminal_states(db, monkeypatch):
    order = Order(
        public_ref="ORD-NOTIFY-REFUND", buyer_phone="254700000011",
        amount=Decimal("150.00"), delivery_fee=Decimal("0.00"),
        delivery_address="Test address", status=OrderStatus.REFUNDED,
    )
    db.add(order)
    await db.flush()
    tx = EscrowTransaction(
        order_id=order.id, buyer_phone=order.buyer_phone,
        payee_phone="254700000012", payee_role=PayeeRole.RIDER,
        amount=Decimal("150.00"), status=EscrowStatus.REFUNDED,
        buyer_access_token="notify-refund-token",
    )
    db.add(tx)
    await db.commit()

    sent = []

    async def fake_send_sms(notification_db, phone, message, idempotency_key):
        sent.append((phone, idempotency_key))
        return SimpleNamespace()

    monkeypatch.setattr(escrow_main, "send_sms", fake_send_sms)
    await escrow_main.notify_escrow_finalized_after_commit(tx.id)
    assert (order.buyer_phone, f"refund-completed-buyer-{tx.id}") in sent

    # A non-terminal status (e.g. still DISPUTED) should not trigger a
    # "your money moved" message - nothing has actually finalized yet.
    tx.status = EscrowStatus.DISPUTED
    await db.commit()
    sent.clear()
    await escrow_main.notify_escrow_finalized_after_commit(tx.id)
    assert sent == []


@pytest.mark.asyncio
async def test_rider_scans_qr_but_seller_is_paid(db, monkeypatch):
    """A rider is assigned to physically deliver and scan the QR, but the
    escrowed money must go to the seller, not the rider."""
    seller = Seller(
        business_name="Rider Payout Test Shop", phone_number="254700000040",
        payout_phone="254700000041",
        # This test is about payee routing (money goes to the seller, not
        # the rider who scans the QR) — not about the separate KYC-gating
        # safeguard in confirm_delivery, which deliberately routes an
        # unverified seller's release to DISPUTED/admin review instead of
        # auto-paying them. A default Seller is UNVERIFIED, which would
        # trip that gate and mask what this test is actually checking.
        kyc_status=SellerKycStatus.VERIFIED,
    )
    rider = DeliveryAgent(
        name="Test Rider", phone_number="254700000042",
        api_key_hash=hash_api_key("rider-test-key"),
    )
    db.add_all([seller, rider])
    await db.flush()

    order = Order(
        public_ref="ORD-RIDER-SELLER-PAYOUT", buyer_phone="254700000043",
        seller_id=seller.id, amount=Decimal("500.00"), delivery_fee=Decimal("0.00"),
        delivery_address="Test address", status=OrderStatus.PAID,
    )
    db.add(order)
    await db.flush()

    # Mirrors create_order()'s payee-selection logic: seller present -> seller is payee.
    tx = EscrowTransaction(
        order_id=order.id, buyer_phone=order.buyer_phone,
        payee_phone=seller.payout_phone, payee_role=PayeeRole.SELLER,
        amount=Decimal("500.00"), status=EscrowStatus.ESCROW_HELD,
        buyer_access_token="rider-seller-payout-buyer-token",
        qr_token="rider-seller-payout-qr",
        qr_token_expires_at=escrow_main._now() + __import__("datetime").timedelta(hours=1),
    )
    db.add(tx)
    await db.flush()
    delivery = Delivery(order_id=order.id, agent_id=None, status=DeliveryStatus.UNASSIGNED)
    db.add(delivery)
    await db.commit()

    # Admin assigns the rider to scan/deliver. Because the order has a
    # seller, this must NOT redirect the payout to the rider.
    await escrow_main.assign_agent(order.id, rider.id, admin_actor="test-admin")

    async with AsyncSessionLocal() as check:
        refreshed_tx = await check.get(EscrowTransaction, tx.id)
        assert refreshed_tx.assigned_agent_id == rider.id
        assert refreshed_tx.payee_role == PayeeRole.SELLER
        assert refreshed_tx.payee_phone == seller.payout_phone

    async with AsyncSessionLocal() as check:
        refreshed_delivery = await check.scalar(
            select(Delivery).where(Delivery.order_id == order.id)
        )
        refreshed_delivery.status = DeliveryStatus.OUT_FOR_DELIVERY
        await check.commit()

    captured_recipient = {}

    async def fake_initiate_b2c_payout(client, phone_number, amount, remarks, originator_conversation_id, occasion=""):
        captured_recipient["phone"] = phone_number
        return {"ConversationID": "conv-rider-seller-payout", "OriginatorConversationID": originator_conversation_id}

    monkeypatch.setattr(escrow_main.mpesa, "initiate_b2c_payout", fake_initiate_b2c_payout)

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(http_client=None)))
    response = await escrow_main.confirm_delivery(
        escrow_main.ConfirmDeliveryRequest(qr_token="rider-seller-payout-qr"),
        request,
        agent=rider,
    )

    assert response["status"] == "DELIVERY_CONFIRMED"
    # The rider's scan triggered the release, but the money went to the seller.
    assert captured_recipient["phone"] == seller.payout_phone
    assert captured_recipient["phone"] != rider.phone_number

    async with AsyncSessionLocal() as check:
        payout = await check.scalar(select(Payout).where(Payout.escrow_transaction_id == tx.id))
        assert payout.recipient_phone == seller.payout_phone
