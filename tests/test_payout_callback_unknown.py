"""Regression tests for N14: a payout Result callback whose result code cannot
be read must leave the payout untouched (never reverse the reservation) and
alert a human; real success and real failure behave as before."""
import secrets
from decimal import Decimal

import pytest
from sqlalchemy import select

from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerTransaction, PayeeRole, Payout,
    ReconciliationRecord,
)
from tests.support import FakeRequest


async def _initiated_payout():
    async with AsyncSessionLocal() as db:
        tx = EscrowTransaction(
            order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
            payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
            status=EscrowStatus.DELIVERY_CONFIRMED, buyer_access_token=secrets.token_hex(16))
        db.add(tx)
        await db.flush()
        po = Payout(escrow_transaction_id=tx.id, purpose="RELEASE", recipient_phone=tx.payee_phone,
                    amount=tx.amount, status="INITIATED", attempt_count=1, conversation_id="AG_CONV_1",
                    originator_conversation_id="escrow-payout-1-1")
        db.add(po)
        await db.flush()
        ids = (tx.id, po.id)
        await db.commit()
        return ids


def _body(result_code, receipt=None):
    result = {"ResultCode": result_code, "ResultDesc": "x", "ConversationID": "AG_CONV_1",
              "OriginatorConversationID": "escrow-payout-1-1"}
    if receipt:
        result["ResultParameters"] = {"ResultParameter": [
            {"Key": "TransactionReceipt", "Value": receipt},
            {"Key": "TransactionAmount", "Value": 500},
        ]}
    return {"Result": result}


async def _call(body):
    import escrow_main

    return await escrow_main._handle_payout_result_callback(FakeRequest(body), "test-secret", "MPESA_B2C")


async def _state(tx_id, po_id):
    async with AsyncSessionLocal() as s:
        po = await s.get(Payout, po_id)
        keys = (await s.execute(select(LedgerTransaction.idempotency_key)
                                .where(LedgerTransaction.escrow_transaction_id == tx_id))).scalars().all()
        recon = (await s.execute(select(ReconciliationRecord.status))).scalars().all()
        return po.status, keys, recon


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_code", [None, "", "garbage"])
async def test_unreadable_result_code_changes_nothing_and_alerts(provider, bad_code):
    tx_id, po_id = await _initiated_payout()
    resp = await _call(_body(bad_code))
    assert resp["ResultCode"] == 0
    status, keys, recon = await _state(tx_id, po_id)
    assert status == "INITIATED"                                   # not FAILED, not COMPLETED
    assert not any(k.endswith("-reverse-%d-1" % po_id) for k in keys)   # nothing reversed
    assert "UNKNOWN" in recon                                      # raw payload kept
    assert ("PAYOUT_RESULT_UNREADABLE", po_id, tx_id) in provider.alerts


@pytest.mark.asyncio
async def test_string_zero_with_a_receipt_completes_the_payout(provider):
    tx_id, po_id = await _initiated_payout()
    await _call(_body("0", receipt="ABC111"))
    status, keys, _ = await _state(tx_id, po_id)
    assert status == "COMPLETED"
    assert not any("reverse" in k for k in keys)


@pytest.mark.asyncio
async def test_a_real_failure_code_still_reverses_and_fails(provider):
    tx_id, po_id = await _initiated_payout()
    await _call(_body(2001))
    status, keys, _ = await _state(tx_id, po_id)
    assert status == "FAILED"
    assert any("reverse" in k for k in keys)