"""Regression tests for C3: the automatic payout retry sweep must re-check
each payout under its own lock right before retrying it, and must pause
between attempts."""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from escrow_models import AsyncSessionLocal, EscrowStatus, EscrowTransaction, PayeeRole, Payout


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


async def _failed_payout(db, *, last_attempt_at, attempt_count=1):
    tx = EscrowTransaction(
        order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
        payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
        status=EscrowStatus.PAYOUT_FAILED, buyer_access_token=secrets.token_hex(16))
    db.add(tx)
    await db.flush()
    po = Payout(escrow_transaction_id=tx.id, purpose="RELEASE", recipient_phone=tx.payee_phone,
                amount=tx.amount, status="FAILED", attempt_count=attempt_count,
                last_attempt_at=last_attempt_at)
    db.add(po)
    await db.flush()
    return po.id


@pytest.mark.asyncio
async def test_recently_attempted_payout_waits_for_the_backoff(provider):
    import escrow_main

    async with AsyncSessionLocal() as db:
        fresh_id = await _failed_payout(db, last_attempt_at=_ago(seconds=10))
        old_id = await _failed_payout(db, last_attempt_at=_ago(hours=1))
        await db.commit()
        await escrow_main.retry_failed_payouts(db, None)
    assert len(provider.b2c_calls) == 1
    async with AsyncSessionLocal() as s:
        assert (await s.get(Payout, fresh_id)).status == "FAILED"
        assert (await s.get(Payout, old_id)).status == "INITIATED"


@pytest.mark.asyncio
async def test_payout_claimed_elsewhere_is_not_retried(provider, monkeypatch):
    import escrow_main

    async with AsyncSessionLocal() as db:
        first_id = await _failed_payout(db, last_attempt_at=_ago(hours=2))
        second_id = await _failed_payout(db, last_attempt_at=_ago(hours=2))
        await db.commit()
    seen = []

    async def fake_initiate(db_, payout, client):
        seen.append(payout.id)
        if len(seen) == 1:
            # Another worker / an admin claims the second payout mid-sweep.
            async with AsyncSessionLocal() as other:
                po = await other.get(Payout, second_id)
                po.status = "SUBMITTING"
                await other.commit()
        await db_.rollback()
        return True

    monkeypatch.setattr(escrow_main, "initiate_payout_for_record", fake_initiate)
    async with AsyncSessionLocal() as db:
        await escrow_main.retry_failed_payouts(db, None)
    assert seen == [first_id]


@pytest.mark.asyncio
async def test_one_failing_retry_does_not_block_the_rest(provider, monkeypatch):
    import escrow_main

    async with AsyncSessionLocal() as db:
        first_id = await _failed_payout(db, last_attempt_at=_ago(hours=2))
        second_id = await _failed_payout(db, last_attempt_at=_ago(hours=2))
        await db.commit()
    seen = []

    async def fake_initiate(db_, payout, client):
        seen.append(payout.id)
        if payout.id == first_id:
            raise RuntimeError("boom")
        await db_.rollback()
        return True

    monkeypatch.setattr(escrow_main, "initiate_payout_for_record", fake_initiate)
    async with AsyncSessionLocal() as db:
        await escrow_main.retry_failed_payouts(db, None)
    assert seen == [first_id, second_id]