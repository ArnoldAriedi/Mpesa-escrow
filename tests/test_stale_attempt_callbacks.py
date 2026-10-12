"""Regression tests for C4: a payout Result callback is matched to the exact
attempt that sent it (by OriginatorConversationID). A callback for an earlier
attempt must never change the current attempt, but must never be lost either."""
import secrets
from decimal import Decimal

import pytest
from sqlalchemy import select

from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerTransaction, PayeeRole, Payout,
    ReconciliationRecord,
)
from tests.support import FakeRequest


async def _payout(*, attempt_count, conversation_id, originator, status="INITIATED"):
    async with AsyncSessionLocal() as db:
        tx = EscrowTransaction(
            order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
            payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
            status=EscrowStatus.DELIVERY_CONFIRMED, buyer_access_token=secrets.token_hex(16))
        db.add(tx)
        await db.flush()
        po = Payout(escrow_transaction_id=tx.id, purpose="RELEASE", recipient_phone=tx.payee_phone,
                    amount=tx.amount, status=status, attempt_count=attempt_count,
                    conversation_id=conversation_id, originator_conversation_id=originator)
        db.add(po)
        await db.flush()
        ids = (tx.id, po.id)
        await db.commit()
        return ids


def _body(code, *, conversation_id, originator, receipt=None):
    result = {"ResultCode": code, "ResultDesc": "x", "ConversationID": conversation_id,
              "OriginatorConversationID": originator}
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
        return po.status, po.transaction_id, keys, recon


def test_originator_id_parser():
    import escrow_main

    assert escrow_main._payout_attempt_from_originator("escrow-payout-12-3") == (12, 3)
    for bad in (None, "", "scheduled-balance-ab12", "escrow-payout-x-1", "escrow-payout-1-", "xescrow-payout-1-1"):
        assert escrow_main._payout_attempt_from_originator(bad) is None


@pytest.mark.asyncio
async def test_late_success_from_an_earlier_attempt_is_recorded_not_applied(provider):
    # attempt 2 is current (conversation AG_2); the callback is for attempt 1 (AG_1)
    tx_id, po_id = await _payout(attempt_count=2, conversation_id="AG_2", originator="escrow-payout-1-2")
    resp = await _call(_body(0, conversation_id="AG_1", originator="escrow-payout-1-1", receipt="OLD111"))
    assert resp["ResultCode"] == 0
    status, txn_id, keys, recon = await _state(tx_id, po_id)
    assert status == "INITIATED" and txn_id is None            # current attempt untouched
    assert "STALE_ATTEMPT_SUCCESS" in recon                    # but recorded
    assert ("PAYOUT_STALE_ATTEMPT_RESULT", po_id, tx_id) in provider.alerts


@pytest.mark.asyncio
async def test_late_failure_from_an_earlier_attempt_is_recorded_without_alert(provider):
    tx_id, po_id = await _payout(attempt_count=2, conversation_id="AG_2", originator="escrow-payout-1-2")
    await _call(_body(2001, conversation_id="AG_1", originator="escrow-payout-1-1"))
    status, _, keys, recon = await _state(tx_id, po_id)
    assert status == "INITIATED"
    assert "STALE_ATTEMPT_FAILURE" in recon
    assert not any("reverse" in k for k in keys)               # nothing reversed
    assert not any(a[0] == "PAYOUT_STALE_ATTEMPT_RESULT" for a in provider.alerts)


@pytest.mark.asyncio
async def test_a_redelivered_stale_callback_is_recorded_once(provider):
    tx_id, po_id = await _payout(attempt_count=2, conversation_id="AG_2", originator="escrow-payout-1-2")
    body = _body(0, conversation_id="AG_1", originator="escrow-payout-1-1", receipt="OLD111")
    await _call(body)
    await _call(body)
    _, _, _, recon = await _state(tx_id, po_id)
    assert recon.count("STALE_ATTEMPT_SUCCESS") == 1


@pytest.mark.asyncio
async def test_callback_for_the_current_attempt_still_completes_the_payout(provider):
    tx_id, po_id = await _payout(attempt_count=2, conversation_id="AG_2", originator="escrow-payout-1-2")
    await _call(_body(0, conversation_id="AG_2", originator="escrow-payout-1-2", receipt="NEW222"))
    status, txn_id, _, _ = await _state(tx_id, po_id)
    assert status == "COMPLETED" and txn_id == "NEW222"


@pytest.mark.asyncio
async def test_callback_that_beats_the_conversation_id_commit_is_not_lost(provider):
    # The request was sent but the commit storing ConversationID had not landed yet.
    tx_id, po_id = await _payout(attempt_count=1, conversation_id=None,
                                 originator="escrow-payout-1-1", status="SUBMITTING")
    await _call(_body(0, conversation_id="AG_EARLY", originator="escrow-payout-1-1", receipt="EARLY333"))
    status, txn_id, _, _ = await _state(tx_id, po_id)
    assert status == "COMPLETED" and txn_id == "EARLY333"

async def _timeout(*, conversation_id, originator):
    import escrow_main

    body = {"Result": {"ConversationID": conversation_id, "OriginatorConversationID": originator,
                       "ResultDesc": "The service request timed out."}}
    return await escrow_main._handle_payout_timeout(FakeRequest(body), "test-secret", "MPESA_B2C")


@pytest.mark.asyncio
async def test_stale_timeout_does_not_park_the_current_attempt(provider):
    tx_id, po_id = await _payout(attempt_count=2, conversation_id="AG_2", originator="escrow-payout-1-2")
    await _timeout(conversation_id="AG_1", originator="escrow-payout-1-1")
    status, _, _, _ = await _state(tx_id, po_id)
    assert status == "INITIATED"


@pytest.mark.asyncio
async def test_timeout_for_the_current_attempt_parks_it_as_timeout(provider):
    tx_id, po_id = await _payout(attempt_count=2, conversation_id="AG_2", originator="escrow-payout-1-2")
    await _timeout(conversation_id="AG_2", originator="escrow-payout-1-2")
    status, _, _, _ = await _state(tx_id, po_id)
    assert status == "TIMEOUT"
    assert ("PAYOUT_TIMEOUT", po_id, tx_id) in provider.alerts


@pytest.mark.asyncio
async def test_timeout_that_beats_the_conversation_id_commit_is_not_lost(provider):
    tx_id, po_id = await _payout(attempt_count=1, conversation_id=None,
                                 originator="escrow-payout-1-1", status="SUBMITTING")
    await _timeout(conversation_id="AG_EARLY", originator="escrow-payout-1-1")
    status, _, _, _ = await _state(tx_id, po_id)
    assert status == "TIMEOUT"