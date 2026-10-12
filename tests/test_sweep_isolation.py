"""Regression tests for N1 (one failing escrow must not block the expiry sweep)
and N2 (scheduled status queries need a cooldown and must not starve newer
stuck payouts)."""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from escrow_models import AsyncSessionLocal, EscrowStatus, EscrowTransaction, PayeeRole, Payout


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


def _escrow(status, **extra):
    return EscrowTransaction(
        order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
        payee_role=PayeeRole.RIDER, amount=Decimal("500.00"), status=status,
        buyer_access_token=secrets.token_hex(16), **extra)


@pytest.mark.asyncio
async def test_one_failing_expiry_does_not_block_the_others(provider, run_once, monkeypatch):
    import escrow_main

    escrow_main._expiry_error_alerted.clear()
    ids = []
    async with AsyncSessionLocal() as db:
        for _ in range(2):
            tx = _escrow(EscrowStatus.ESCROW_HELD, qr_token=secrets.token_urlsafe(16),
                         qr_token_expires_at=_ago(hours=1))
            db.add(tx)
            await db.flush()
            ids.append(tx.id)
        await db.commit()
    first, second = ids

    async def fake_expire_one(db_, tx, client):
        if tx.id == first:
            raise RuntimeError("boom")
        tx.status = EscrowStatus.REFUND_INITIATED

    monkeypatch.setattr(escrow_main, "expire_one", fake_expire_one)
    await run_once(escrow_main.expiry_loop)
    await run_once(escrow_main.expiry_loop)

    async with AsyncSessionLocal() as s:
        assert (await s.get(EscrowTransaction, first)).status == EscrowStatus.ESCROW_HELD  # rolled back, retried next sweep
        assert (await s.get(EscrowTransaction, second)).status == EscrowStatus.REFUND_INITIATED
    assert provider.alerts.count(("EXPIRY_REFUND_ERROR", 0, first)) == 1  # alerted once, not every sweep


async def _stale_initiated(n):
    async with AsyncSessionLocal() as db:
        for i in range(n):
            tx = _escrow(EscrowStatus.DELIVERY_CONFIRMED)
            db.add(tx)
            await db.flush()
            db.add(Payout(escrow_transaction_id=tx.id, purpose="RELEASE",
                          recipient_phone=tx.payee_phone, amount=tx.amount, status="INITIATED",
                          conversation_id=f"AG_{i}_{secrets.token_hex(4)}", attempt_count=1,
                          last_attempt_at=_ago(hours=2)))
            await db.flush()
        await db.commit()


@pytest.mark.asyncio
async def test_stale_payout_is_queried_once_per_cooldown(provider, run_once):
    import escrow_main

    await _stale_initiated(1)
    await run_once(escrow_main.reconciliation_loop)
    await run_once(escrow_main.reconciliation_loop)
    assert len(provider.status_calls) == 1


@pytest.mark.asyncio
async def test_older_stale_payouts_do_not_starve_newer_ones(provider, run_once):
    import escrow_main

    await _stale_initiated(11)            # the sweep takes at most 10 per run
    await run_once(escrow_main.reconciliation_loop)
    await run_once(escrow_main.reconciliation_loop)
    assert len(provider.status_calls) == 11
    assert len(set(provider.status_calls)) == 11