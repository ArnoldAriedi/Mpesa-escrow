"""B1: when the delivery QR expires, the buyer is refunded automatically ONLY if the goods
cannot have been handed over. Once the order is out for delivery the refund is held for an
admin, so a buyer cannot keep the goods and withhold the QR."""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from escrow_models import (
    AsyncSessionLocal, Delivery, DeliveryStatus, Dispute, DisputeStatus, EscrowStatus,
    EscrowTransaction, LedgerTransaction, Order, OrderStatus, PayeeRole, Payout,
)


async def _expired_escrow(delivery_status=None):
    async with AsyncSessionLocal() as db:
        o = Order(public_ref=f"X{secrets.token_hex(3)}", buyer_phone="254700000001", amount=Decimal("500.00"),
                  delivery_fee=Decimal("0.00"), delivery_address="x", status=OrderStatus.PAID)
        db.add(o)
        await db.flush()
        tx = EscrowTransaction(order_id=o.id, buyer_phone="254700000001", payee_phone="254700000002",
                               payee_role=PayeeRole.RIDER, amount=Decimal("500.00"), status=EscrowStatus.ESCROW_HELD,
                               buyer_access_token=secrets.token_hex(16), qr_token=secrets.token_urlsafe(16),
                               qr_token_expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
        db.add(tx)
        if delivery_status is not None:
            db.add(Delivery(order_id=o.id, agent_id=None, status=delivery_status))
        await db.flush()
        ids = (tx.id, o.id)
        await db.commit()
        return ids


async def _state(tx_id, order_id):
    async with AsyncSessionLocal() as s:
        tx = await s.get(EscrowTransaction, tx_id)
        order = await s.get(Order, order_id)
        disputes = (await s.execute(select(Dispute).where(Dispute.order_id == order_id))).scalars().all()
        payouts = (await s.execute(select(Payout).where(Payout.escrow_transaction_id == tx_id))).scalars().all()
        ledger = (await s.execute(select(LedgerTransaction.idempotency_key)
                                  .where(LedgerTransaction.escrow_transaction_id == tx_id))).scalars().all()
        return tx, order, disputes, payouts, ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DeliveryStatus.OUT_FOR_DELIVERY, DeliveryStatus.DELIVERED])
async def test_expiry_once_out_for_delivery_is_held_for_review(provider, status):
    import escrow_main

    tx_id, order_id = await _expired_escrow(status)
    await escrow_main._expire_one_escrow_by_id(tx_id, None)
    tx, order, disputes, payouts, ledger = await _state(tx_id, order_id)
    assert tx.status == EscrowStatus.DISPUTED
    assert order.status == OrderStatus.DISPUTED
    assert len(disputes) == 1 and disputes[0].status == DisputeStatus.OPEN
    assert payouts == [] and ledger == []                       # no refund, no money moved
    assert provider.b2c_calls == []
    assert ("EXPIRY_HELD_FOR_REVIEW", 0, tx_id) in provider.alerts


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, DeliveryStatus.UNASSIGNED, DeliveryStatus.ASSIGNED,
                                    DeliveryStatus.ACCEPTED, DeliveryStatus.PICKED_UP, DeliveryStatus.CANCELLED])
async def test_expiry_before_the_handover_still_refunds_automatically(provider, status):
    import escrow_main

    tx_id, order_id = await _expired_escrow(status)
    await escrow_main._expire_one_escrow_by_id(tx_id, None)
    tx, order, disputes, payouts, ledger = await _state(tx_id, order_id)
    assert tx.status == EscrowStatus.REFUND_INITIATED
    assert len(payouts) == 1 and payouts[0].purpose == "REFUND"
    assert disputes == []
    assert len(provider.b2c_calls) == 1
    assert not any(a[0] == "EXPIRY_HELD_FOR_REVIEW" for a in provider.alerts)


@pytest.mark.asyncio
async def test_a_second_expiry_pass_does_not_open_a_second_dispute(provider):
    import escrow_main

    tx_id, order_id = await _expired_escrow(DeliveryStatus.OUT_FOR_DELIVERY)
    await escrow_main._expire_one_escrow_by_id(tx_id, None)
    await escrow_main._expire_one_escrow_by_id(tx_id, None)
    _, _, disputes, payouts, _ = await _state(tx_id, order_id)
    assert len(disputes) == 1 and payouts == []
    assert provider.alerts.count(("EXPIRY_HELD_FOR_REVIEW", 0, tx_id)) == 1


@pytest.mark.asyncio
async def test_the_expiry_loop_applies_the_rule(provider, run_once):
    import escrow_main

    held_tx, held_order = await _expired_escrow(DeliveryStatus.OUT_FOR_DELIVERY)
    refund_tx, refund_order = await _expired_escrow(DeliveryStatus.ASSIGNED)
    await run_once(escrow_main.expiry_loop)
    held, *_ = await _state(held_tx, held_order)
    refunded, *_ = await _state(refund_tx, refund_order)
    assert held.status == EscrowStatus.DISPUTED
    assert refunded.status == EscrowStatus.REFUND_INITIATED


@pytest.mark.asyncio
async def test_an_admin_can_settle_a_held_expiry_with_the_normal_dispute_flow(provider):
    import escrow_main
    from tests.support import FakeRequest

    tx_id, order_id = await _expired_escrow(DeliveryStatus.OUT_FOR_DELIVERY)
    await escrow_main._expire_one_escrow_by_id(tx_id, None)
    _, _, disputes, _, _ = await _state(tx_id, order_id)
    req = escrow_main.ResolveDisputeRequest(decision="REFUND", note="Buyer confirmed the goods never arrived")
    out = await escrow_main.resolve_dispute(disputes[0].id, req, FakeRequest(None), admin_actor="test-admin")
    assert out["escrow_status"] == "REFUND_INITIATED"
    tx, _, _, payouts, _ = await _state(tx_id, order_id)
    assert tx.status == EscrowStatus.REFUND_INITIATED
    assert len(payouts) == 1 and payouts[0].purpose == "REFUND"