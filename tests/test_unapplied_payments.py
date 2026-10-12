"""C5 groundwork: money Safaricom confirmed as received that cannot be applied
to an escrow must be recorded exactly once, accounted for in the ledger, and
must never crash the callback that records it."""
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from escrow_models import (
    AsyncSessionLocal, LedgerAccount, LedgerEntry, LedgerEntryDirection, LedgerTransaction,
    UnappliedPayment, record_unapplied_payment,
)


async def _record(**overrides):
    fields = dict(source="STK", provider_reference="ws_CO_1", reason="NO_MATCHING_ESCROW",
                  raw_payload="{}", amount="500", phone_number="254700000001",
                  mpesa_receipt_number="QWE123")
    fields.update(overrides)
    async with AsyncSessionLocal() as db:
        row, created = await record_unapplied_payment(db, **fields)
        await db.commit()
        return row.id, created


async def _unapplied_balance():
    """CREDIT minus DEBIT on the UNAPPLIED_PAYMENTS account."""
    async with AsyncSessionLocal() as s:
        acct = await s.scalar(select(LedgerAccount).where(LedgerAccount.code == "UNAPPLIED_PAYMENTS"))
        if acct is None:
            return 0
        credit = await s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.account_id == acct.id, LedgerEntry.direction == LedgerEntryDirection.CREDIT))
        debit = await s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.account_id == acct.id, LedgerEntry.direction == LedgerEntryDirection.DEBIT))
        return credit - debit


async def _row_count():
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(func.count()).select_from(UnappliedPayment))


@pytest.mark.asyncio
async def test_payment_is_recorded_and_accounted_for():
    _, created = await _record()
    assert created is True
    assert await _row_count() == 1
    assert await _unapplied_balance() == 500


@pytest.mark.asyncio
async def test_a_redelivered_callback_is_recorded_once():
    await _record()
    _, created = await _record()
    assert created is False
    assert await _row_count() == 1
    assert await _unapplied_balance() == 500          # not double-counted


@pytest.mark.asyncio
async def test_same_reference_from_a_different_source_is_a_different_payment():
    await _record(source="STK", provider_reference="X1")
    _, created = await _record(source="C2B", provider_reference="X1")
    assert created is True
    assert await _row_count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_amount", [None, "abc", "0", "-5", "NaN", "Infinity"])
async def test_unreadable_amount_still_records_the_payment_but_posts_nothing(bad_amount):
    row_id, created = await _record(amount=bad_amount)
    assert created is True
    assert await _row_count() == 1
    assert await _unapplied_balance() == 0            # no trustworthy amount, no ledger entry
    async with AsyncSessionLocal() as s:
        row = await s.get(UnappliedPayment, row_id)
        assert row.amount is None and row.raw_payload == "{}"


@pytest.mark.asyncio
async def test_amount_is_stored_to_two_decimals():
    row_id, _ = await _record(amount=500.4)
    async with AsyncSessionLocal() as s:
        assert (await s.get(UnappliedPayment, row_id)).amount == Decimal("500.40")


@pytest.mark.asyncio
async def test_new_rows_start_open():
    row_id, _ = await _record()
    async with AsyncSessionLocal() as s:
        assert (await s.get(UnappliedPayment, row_id)).status == "OPEN"


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("status", "BOGUS"), ("source", "WIRE")])
async def test_database_rejects_invalid_status_or_source(field, value):
    async with AsyncSessionLocal() as db:
        row = UnappliedPayment(source="STK", provider_reference="ws_CO_9", reason="X", raw_payload="{}")
        setattr(row, field, value)
        db.add(row)
        with pytest.raises(IntegrityError):
            await db.commit()


@pytest.mark.asyncio
async def test_database_rejects_a_duplicate_source_and_reference():
    async with AsyncSessionLocal() as db:
        db.add(UnappliedPayment(source="STK", provider_reference="DUP", reason="X", raw_payload="{}"))
        await db.commit()
    async with AsyncSessionLocal() as db:
        db.add(UnappliedPayment(source="STK", provider_reference="DUP", reason="X", raw_payload="{}"))
        with pytest.raises(IntegrityError):
            await db.commit()