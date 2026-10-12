"""Regression tests for C1: once a payout request may have reached Safaricom,
initiate_payout_for_record must never mark it FAILED (which makes it
auto-retryable and can pay twice)."""
import secrets
from decimal import Decimal

import pytest
from sqlalchemy import select

import escrow_core as mpesa
from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerTransaction, PayeeRole, Payout,
)


async def _pending_payout(db):
    tx = EscrowTransaction(
        order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
        payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
        status=EscrowStatus.DELIVERY_CONFIRMED, buyer_access_token=secrets.token_hex(16))
    db.add(tx)
    await db.flush()
    po = Payout(escrow_transaction_id=tx.id, purpose="RELEASE",
                recipient_phone=tx.payee_phone, amount=tx.amount, status="PENDING")
    db.add(po)
    await db.flush()
    ids = (tx.id, po.id)
    await db.commit()
    return po, ids


async def _state(tx_id, po_id):
    async with AsyncSessionLocal() as s:
        po = await s.get(Payout, po_id)
        keys = (await s.execute(
            select(LedgerTransaction.idempotency_key)
            .where(LedgerTransaction.escrow_transaction_id == tx_id))).scalars().all()
        return po.status, po.conversation_id, keys


@pytest.mark.asyncio
async def test_accepted_request_is_initiated(provider):
    import escrow_main

    async with AsyncSessionLocal() as db:
        po, (tx_id, po_id) = await _pending_payout(db)
        ok = await escrow_main.initiate_payout_for_record(db, po, None)
    assert ok is True
    status, conv, keys = await _state(tx_id, po_id)
    assert status == "INITIATED" and conv == "AG_B2C_1"
    assert f"mpesa-settle-reverse-{po_id}-1" not in keys


@pytest.mark.asyncio
async def test_confirmed_rejection_reverses_and_fails(provider):
    import escrow_main

    provider.b2c_exc = mpesa.MpesaRejected("insufficient float")
    async with AsyncSessionLocal() as db:
        po, (tx_id, po_id) = await _pending_payout(db)
        ok = await escrow_main.initiate_payout_for_record(db, po, None)
    assert ok is False
    status, _, keys = await _state(tx_id, po_id)
    assert status == "FAILED"
    assert f"mpesa-settle-reverse-{po_id}-1" in keys
    assert ("PAYOUT_FAILED", po_id, tx_id) in provider.alerts


@pytest.mark.asyncio
async def test_rejection_whose_reversal_fails_is_not_marked_failed(provider, monkeypatch):
    import escrow_main

    provider.b2c_exc = mpesa.MpesaRejected("insufficient float")
    real = escrow_main.post_ledger_transaction

    async def failing(db_, **kw):
        if kw["idempotency_key"].startswith("mpesa-settle-reverse"):
            raise RuntimeError("ledger unavailable")
        return await real(db_, **kw)

    monkeypatch.setattr(escrow_main, "post_ledger_transaction", failing)
    async with AsyncSessionLocal() as db:
        po, (tx_id, po_id) = await _pending_payout(db)
        ok = await escrow_main.initiate_payout_for_record(db, po, None)
    assert ok is False
    status, _, keys = await _state(tx_id, po_id)
    assert status == "TIMEOUT"          # never FAILED: that would allow a double reservation
    assert f"mpesa-settle-reverse-{po_id}-1" not in keys
    assert ("PAYOUT_TIMEOUT", po_id, tx_id) in provider.alerts


@pytest.mark.asyncio
async def test_db_failure_after_send_parks_as_timeout(provider, monkeypatch):
    import escrow_main

    async with AsyncSessionLocal() as db:
        po, (tx_id, po_id) = await _pending_payout(db)
        real_commit = db.commit
        calls = {"n": 0}

        async def flaky_commit():
            calls["n"] += 1
            if calls["n"] == 2:          # 1 = reservation commit, 2 = INITIATED commit
                raise RuntimeError("database went away")
            return await real_commit()

        monkeypatch.setattr(db, "commit", flaky_commit)
        ok = await escrow_main.initiate_payout_for_record(db, po, None)
    assert ok is False
    status, conv, keys = await _state(tx_id, po_id)
    assert status == "TIMEOUT"
    assert conv == "AG_B2C_1"            # the provider reference was kept
    assert f"mpesa-settle-reverse-{po_id}-1" not in keys
    assert ("PAYOUT_TIMEOUT", po_id, tx_id) in provider.alerts