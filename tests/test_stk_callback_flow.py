"""STK callback handler: the happy path and the harmless cases, as they behave
today. (Cases where a successful payment cannot be applied are in
test_stk_unapplied.py.)"""
import json
import secrets
from decimal import Decimal

import pytest
from sqlalchemy import select

from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerTransaction, PayeeRole,
)
from tests.support import FakeRequest

CHECKOUT = "ws_CO_TEST_1"
BUYER = "254700000001"


async def _escrow(*, status=EscrowStatus.PENDING_PAYMENT, amount="500.00", checkout=CHECKOUT, **extra):
    async with AsyncSessionLocal() as db:
        tx = EscrowTransaction(
            order_id=None, buyer_phone=BUYER, payee_phone="254700000002", payee_role=PayeeRole.RIDER,
            amount=Decimal(amount), status=status, buyer_access_token=secrets.token_hex(16),
            mpesa_checkout_request_id=checkout, **extra)
        db.add(tx)
        await db.flush()
        tx_id = tx.id
        await db.commit()
        return tx_id


def _body(code=0, *, checkout=CHECKOUT, amount=500, phone=int(BUYER), receipt="QWE123", merchant="m1"):
    cb = {"CheckoutRequestID": checkout, "MerchantRequestID": merchant, "ResultCode": code,
          "ResultDesc": "x"}
    if code == 0:
        items = []
        if amount is not None:
            items.append({"Name": "Amount", "Value": amount})
        if receipt is not None:
            items.append({"Name": "MpesaReceiptNumber", "Value": receipt})
        if phone is not None:
            items.append({"Name": "PhoneNumber", "Value": phone})
        cb["CallbackMetadata"] = {"Item": items}
    return {"Body": {"stkCallback": cb}}


async def _call(body):
    import escrow_main

    return await escrow_main.stk_callback(FakeRequest(body), "test-secret")


async def _tx(tx_id):
    async with AsyncSessionLocal() as s:
        return await s.get(EscrowTransaction, tx_id)


async def _ledger_keys(tx_id):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(LedgerTransaction.idempotency_key)
                                .where(LedgerTransaction.escrow_transaction_id == tx_id))).scalars().all()


@pytest.mark.asyncio
async def test_successful_payment_holds_the_escrow(provider):
    tx_id = await _escrow()
    resp = await _call(_body())
    assert resp["ResultCode"] == 0
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.ESCROW_HELD
    assert tx.mpesa_receipt_number == "QWE123" and tx.qr_token
    assert f"mpesa-hold-{tx_id}" in await _ledger_keys(tx_id)


@pytest.mark.asyncio
async def test_failed_payment_marks_the_escrow_failed_and_posts_nothing(provider):
    tx_id = await _escrow()
    await _call(_body(1032))
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.PAYMENT_FAILED
    assert await _ledger_keys(tx_id) == []


@pytest.mark.asyncio
async def test_redelivered_success_is_a_no_op(provider):
    tx_id = await _escrow()
    await _call(_body())
    resp = await _call(_body())
    assert resp["ResultDesc"] == "Already processed"
    assert (await _ledger_keys(tx_id)).count(f"mpesa-hold-{tx_id}") == 1


@pytest.mark.asyncio
async def test_callback_without_ids_is_acknowledged_and_ignored(provider):
    resp = await _call({"Body": {"stkCallback": {}}})
    assert resp["ResultCode"] == 0