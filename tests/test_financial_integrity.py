from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from escrow_models import (
    AsyncSessionLocal, Base, Delivery, EscrowStatus, EscrowTransaction,
    LedgerEntry, LedgerTransaction, Order, OrderItem, OrderStatus, PayeeRole,
    Payout, engine, post_ledger_transaction,
)
from escrow_main import claim_expired_transaction

@pytest.mark.asyncio
async def test_order_with_financial_history_cannot_be_deleted(db):
    order = Order(
        public_ref="ORD-DELETE-TEST", buyer_phone="254700000001",
        amount=Decimal("1000.00"), delivery_fee=Decimal("100.00"),
        delivery_address="Test address", status=OrderStatus.PAID,
    )
    order.items.append(OrderItem(name="Item", quantity=1, unit_price=Decimal("1000.00")))
    db.add(order)
    await db.flush()
    order_id = order.id  # captured now, while still loaded — see note below
    escrow = EscrowTransaction(
        order_id=order.id, buyer_phone=order.buyer_phone,
        payee_phone="254700000002", payee_role=PayeeRole.RIDER,
        amount=Decimal("1100.00"), status=EscrowStatus.ESCROW_HELD,
        buyer_access_token="buyer-delete-test-token",
    )
    db.add(escrow)
    await db.flush()
    await post_ledger_transaction(
        db, reference="TEST-DELETE-1", idempotency_key="TEST-DELETE-1",
        description="Test escrow hold", debit_account_code="MPESA_CLEARING",
        credit_account_code="ESCROW_HOLDING", amount=Decimal("1100.00"),
        escrow_transaction_id=escrow.id,
    )
    await db.commit()

    await db.delete(order)
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # Checking via a brand-new session rather than re-querying `db` here
    # deliberately: after the rollback, `order` is an expired object still
    # sitting in this session's identity map — even its `id` needs a fresh
    # load once expired, which is why order_id was captured earlier rather
    # than read here. Merging a freshly-selected row into an existing
    # expired identity-map entry also hits a SQLAlchemy code path that
    # isn't reliably async-safe in every version. A fresh session has no
    # such pre-existing entry to merge into, so the check is simple and
    # reliable, and is just as good a confirmation that the rows survived
    # the rolled-back delete.
    async with AsyncSessionLocal() as check:
        assert (await check.execute(select(Order).where(Order.id == order_id))).scalar_one_or_none() is not None
        assert (await check.execute(select(LedgerTransaction).where(LedgerTransaction.id == 1))).scalar_one_or_none() is not None

@pytest.mark.asyncio
async def test_duplicate_payout_for_same_escrow_is_rejected(db):
    escrow = EscrowTransaction(
        buyer_phone="254700000001", payee_phone="254700000002",
        payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
        status=EscrowStatus.EXPIRED, buyer_access_token="payout-unique-token",
    )
    db.add(escrow)
    await db.flush()
    db.add(Payout(escrow_transaction_id=escrow.id, purpose="REFUND", recipient_phone=escrow.buyer_phone, amount=Decimal("500.00")))
    await db.commit()
    db.add(Payout(escrow_transaction_id=escrow.id, purpose="REFUND", recipient_phone=escrow.buyer_phone, amount=Decimal("500.00")))
    with pytest.raises(IntegrityError):
        await db.commit()

@pytest.mark.asyncio
async def test_two_workers_only_one_can_claim_expired_escrow():
    async with AsyncSessionLocal() as setup:
        tx = EscrowTransaction(
            buyer_phone="254700000001", payee_phone="254700000002",
            payee_role=PayeeRole.RIDER, amount=Decimal("700.00"),
            status=EscrowStatus.ESCROW_HELD, buyer_access_token="race-token",
            qr_token_expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        setup.add(tx)
        await setup.commit()
        tx_id = tx.id

    async def worker():
        async with AsyncSessionLocal() as session:
            claimed = await claim_expired_transaction(session, tx_id)
            await session.commit()
            return claimed

    # The conditional UPDATE is the final guard. Even where SQLite ignores
    # FOR UPDATE, only one worker can transition HELD -> EXPIRED.
    first, second = await __import__("asyncio").gather(worker(), worker())
    assert sorted([first, second]) == [False, True]

    async with AsyncSessionLocal() as check:
        tx = await check.get(EscrowTransaction, tx_id)
        assert tx.status == EscrowStatus.EXPIRED
