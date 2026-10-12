"""C5/N7: a SUCCESSFUL STK payment that cannot be applied to its escrow must be
recorded (and accounted for), alerted once, and acknowledged with 200. It is
never dropped, never applied to the wrong escrow, and never refunded
automatically."""
import pytest
from sqlalchemy import func, select

from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerAccount, LedgerEntry,
    LedgerEntryDirection, UnappliedPayment,
)
from tests.test_stk_callback_flow import BUYER, CHECKOUT, _body, _call, _escrow, _ledger_keys, _tx


async def _unapplied():
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(UnappliedPayment))).scalars().all()


async def _unapplied_balance():
    async with AsyncSessionLocal() as s:
        acct = await s.scalar(select(LedgerAccount).where(LedgerAccount.code == "UNAPPLIED_PAYMENTS"))
        if acct is None:
            return 0
        credit = await s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.account_id == acct.id, LedgerEntry.direction == LedgerEntryDirection.CREDIT))
        debit = await s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.account_id == acct.id, LedgerEntry.direction == LedgerEntryDirection.DEBIT))
        return credit - debit


def _alert_kinds(provider):
    return [a[0] for a in provider.alerts]


@pytest.mark.asyncio
async def test_no_matching_escrow_is_recorded_not_dropped(provider):
    resp = await _call(_body(checkout="ws_CO_NOBODY"))
    assert resp["ResultCode"] == 0
    rows = await _unapplied()
    assert len(rows) == 1 and rows[0].reason == "NO_MATCHING_ESCROW" and rows[0].status == "OPEN"
    assert rows[0].mpesa_receipt_number == "QWE123"
    assert await _unapplied_balance() == 500
    assert "UNAPPLIED_STK_PAYMENT" in _alert_kinds(provider)


@pytest.mark.asyncio
async def test_failed_payment_with_no_matching_escrow_is_just_ignored(provider):
    await _call(_body(1032, checkout="ws_CO_NOBODY"))
    assert await _unapplied() == []


@pytest.mark.asyncio
async def test_payment_after_the_order_was_cancelled_is_held_not_lost(provider):
    # N7: the buyer cancelled (escrow PAYMENT_FAILED) but still entered their PIN.
    tx_id = await _escrow(status=EscrowStatus.PAYMENT_FAILED)
    resp = await _call(_body())
    assert resp["ResultCode"] == 0
    rows = await _unapplied()
    assert len(rows) == 1 and rows[0].reason == "ESCROW_NOT_AWAITING_PAYMENT"
    assert rows[0].escrow_transaction_id == tx_id
    assert (await _tx(tx_id)).status == EscrowStatus.PAYMENT_FAILED     # not resurrected
    assert await _unapplied_balance() == 500


@pytest.mark.asyncio
async def test_a_second_payment_on_an_already_paid_escrow_is_held(provider):
    tx_id = await _escrow()
    await _call(_body(receipt="FIRST1"))
    await _call(_body(receipt="SECOND2"))
    rows = await _unapplied()
    assert len(rows) == 1 and rows[0].mpesa_receipt_number == "SECOND2"
    assert (await _tx(tx_id)).mpesa_receipt_number == "FIRST1"
    assert (await _ledger_keys(tx_id)).count(f"mpesa-hold-{tx_id}") == 1


@pytest.mark.asyncio
async def test_redelivery_of_an_unapplied_payment_is_recorded_and_alerted_once(provider):
    await _escrow(status=EscrowStatus.PAYMENT_FAILED)
    await _call(_body())
    await _call(_body())
    assert len(await _unapplied()) == 1
    assert await _unapplied_balance() == 500
    assert _alert_kinds(provider).count("UNAPPLIED_STK_PAYMENT") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,reason", [
    (dict(receipt=None), "INCOMPLETE_CALLBACK"),
    (dict(amount=None), "INCOMPLETE_CALLBACK"),
    (dict(phone=None), "INCOMPLETE_CALLBACK"),
    (dict(amount=499), "AMOUNT_OR_PHONE_MISMATCH"),
    (dict(phone=254799999999), "AMOUNT_OR_PHONE_MISMATCH"),
])
async def test_unusable_success_callback_is_held_and_the_escrow_stays_pending(provider, kwargs, reason):
    tx_id = await _escrow()
    resp = await _call(_body(**kwargs))
    assert resp["ResultCode"] == 0
    rows = await _unapplied()
    assert len(rows) == 1 and rows[0].reason == reason
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.PENDING_PAYMENT                    # not held, not failed
    assert tx.mpesa_receipt_number is None
    assert f"mpesa-hold-{tx_id}" not in await _ledger_keys(tx_id)


@pytest.mark.asyncio
async def test_receipt_already_on_another_escrow_is_held(provider):
    await _escrow(checkout="ws_CO_A")
    await _call(_body(checkout="ws_CO_A", receipt="SAME99"))
    tx_b = await _escrow(checkout="ws_CO_B")
    resp = await _call(_body(checkout="ws_CO_B", receipt="SAME99"))
    assert resp["ResultCode"] == 0
    rows = await _unapplied()
    assert len(rows) == 1 and rows[0].reason == "DUPLICATE_RECEIPT"
    assert (await _tx(tx_b)).status == EscrowStatus.PENDING_PAYMENT


@pytest.mark.asyncio
async def test_unreadable_amount_is_still_recorded_with_its_raw_payload(provider):
    await _escrow(status=EscrowStatus.PAYMENT_FAILED)
    body = _body(amount="not-a-number")
    await _call(body)
    rows = await _unapplied()
    assert len(rows) == 1 and rows[0].amount is None and "not-a-number" in rows[0].raw_payload
    assert await _unapplied_balance() == 0