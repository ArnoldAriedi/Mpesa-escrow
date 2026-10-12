"""H5: a posting that would take a liability account below zero is refused. The guard is ON in
these tests (guard_on); escrows are built through the real STK callback so the payment hold entry
exists exactly as it does in production."""
import secrets
from decimal import Decimal

import pytest
from sqlalchemy import select

import escrow_models as models
from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerOverdraft, LedgerTransaction, PayeeRole,
    post_ledger_transaction,
)
from tests.test_stk_callback_flow import _body, _call, _escrow, _tx


async def _post(db, debit, credit, amount, tx_id, key=None):
    key = key or secrets.token_hex(6)
    return await post_ledger_transaction(
        db, reference=f"T-{key}", idempotency_key=key, description="test",
        debit_account_code=debit, credit_account_code=credit, amount=Decimal(amount), escrow_transaction_id=tx_id)


async def _bare_escrow():
    async with AsyncSessionLocal() as db:
        tx = EscrowTransaction(order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
                               payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
                               status=EscrowStatus.ESCROW_HELD, buyer_access_token=secrets.token_hex(16))
        db.add(tx)
        await db.flush()
        tx_id = tx.id
        await db.commit()
        return tx_id


# ---- the guard itself ----

@pytest.mark.asyncio
async def test_reserving_without_a_hold_is_refused(guard_on):
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        with pytest.raises(LedgerOverdraft):
            await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", tx_id)


@pytest.mark.asyncio
async def test_a_normal_hold_reserve_send_sequence_is_allowed(guard_on):
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        await _post(db, "MPESA_CLEARING", "ESCROW_HOLDING", "500", tx_id)       # payment received
        await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", tx_id)       # reserve the payout
        await _post(db, "SELLER_PAYABLE", "MPESA_CLEARING", "500", tx_id)       # sent to Safaricom
        await db.commit()


@pytest.mark.asyncio
async def test_reserving_more_than_was_held_is_refused(guard_on):
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        await _post(db, "MPESA_CLEARING", "ESCROW_HOLDING", "500", tx_id)
        await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "300", tx_id)
        with pytest.raises(LedgerOverdraft):
            await _post(db, "ESCROW_HOLDING", "BUYER_REFUND_PAYABLE", "300", tx_id)   # only 200 left


@pytest.mark.asyncio
async def test_a_double_release_is_refused(guard_on):
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        await _post(db, "MPESA_CLEARING", "ESCROW_HOLDING", "500", tx_id)
        await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", tx_id)
        with pytest.raises(LedgerOverdraft):
            await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", tx_id)


@pytest.mark.asyncio
async def test_sending_more_than_was_reserved_is_refused(guard_on):
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        await _post(db, "MPESA_CLEARING", "ESCROW_HOLDING", "500", tx_id)
        await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "200", tx_id)
        with pytest.raises(LedgerOverdraft):
            await _post(db, "SELLER_PAYABLE", "MPESA_CLEARING", "500", tx_id)


@pytest.mark.asyncio
async def test_one_escrow_cannot_spend_another_escrows_hold(guard_on):
    funded = await _bare_escrow()
    empty = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        await _post(db, "MPESA_CLEARING", "ESCROW_HOLDING", "500", funded)
        with pytest.raises(LedgerOverdraft):
            await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", empty)


@pytest.mark.asyncio
async def test_a_refused_posting_leaves_no_entries_behind(guard_on):
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        with pytest.raises(LedgerOverdraft):
            await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", tx_id, key="refused-1")
        await db.rollback()
    async with AsyncSessionLocal() as s:
        keys = (await s.execute(select(LedgerTransaction.idempotency_key))).scalars().all()
    assert "refused-1" not in keys


@pytest.mark.asyncio
async def test_unapplied_payments_cannot_be_cleared_below_zero(guard_on):
    async with AsyncSessionLocal() as db:
        with pytest.raises(LedgerOverdraft):
            await _post(db, "UNAPPLIED_PAYMENTS", "MPESA_CLEARING", "100", None)


@pytest.mark.asyncio
async def test_mpesa_clearing_is_allowed_to_go_either_way(guard_on):
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        await _post(db, "MPESA_CLEARING", "ESCROW_HOLDING", "500", tx_id)
        await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", tx_id)
        await _post(db, "SELLER_PAYABLE", "MPESA_CLEARING", "500", tx_id)
        await db.commit()                              # clearing ends where it started: net 0


@pytest.mark.asyncio
async def test_the_switch_really_turns_the_guard_off():
    # no guard_on: the autouse fixture has it OFF, which is what the legacy tests rely on
    assert models.LEDGER_OVERDRAFT_GUARD is False
    tx_id = await _bare_escrow()
    async with AsyncSessionLocal() as db:
        await _post(db, "ESCROW_HOLDING", "SELLER_PAYABLE", "500", tx_id)
        await db.commit()


# ---- end to end: real flows with the guard ON ----

@pytest.mark.asyncio
async def test_a_real_stk_payment_then_full_release_passes_the_guard(guard_on, provider):
    import escrow_main

    tx_id = await _escrow()
    await _call(_body())                                              # real callback posts the hold
    assert (await _tx(tx_id)).status == EscrowStatus.ESCROW_HELD
    async with AsyncSessionLocal() as db:
        tx = await db.get(EscrowTransaction, tx_id)
        payout = await escrow_main.create_payout(db, tx, purpose="RELEASE", recipient_phone=tx.payee_phone,
                                                 amount=tx.amount, debit_account="ESCROW_HOLDING",
                                                 credit_account="RIDER_PAYABLE")
        await db.commit()
        ok = await escrow_main.initiate_payout_for_record(db, payout, None)
    assert ok is True


@pytest.mark.asyncio
async def test_a_real_payment_then_a_second_full_release_is_refused(guard_on, provider):
    import escrow_main

    tx_id = await _escrow()
    await _call(_body())
    async with AsyncSessionLocal() as db:
        tx = await db.get(EscrowTransaction, tx_id)
        await escrow_main.create_payout(db, tx, purpose="RELEASE", recipient_phone=tx.payee_phone, amount=tx.amount,
                                        debit_account="ESCROW_HOLDING", credit_account="RIDER_PAYABLE")
        await db.commit()
    async with AsyncSessionLocal() as db:
        tx = await db.get(EscrowTransaction, tx_id)
        with pytest.raises(Exception) as e:                            # unique(escrow, purpose) or the guard
            await escrow_main.create_payout(db, tx, purpose="REFUND", recipient_phone=tx.buyer_phone, amount=tx.amount,
                                            debit_account="ESCROW_HOLDING", credit_account="BUYER_REFUND_PAYABLE")
            await db.commit()
        assert isinstance(e.value, LedgerOverdraft)