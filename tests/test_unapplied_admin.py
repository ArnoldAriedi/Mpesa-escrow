"""C5: finance admins can list held payments and mark them refunded externally.
The ledger is cleared exactly once, an audit entry is written, and nothing else
is possible (no write-off, no double resolve)."""
import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from escrow_models import (
    AsyncSessionLocal, AuditLogEntry, LedgerAccount, LedgerEntry, LedgerEntryDirection,
    UnappliedPayment, record_unapplied_payment,
)


async def _hold(reference="ws_CO_1", amount="500", phone="254700000001", source="STK"):
    async with AsyncSessionLocal() as db:
        row, _ = await record_unapplied_payment(
            db, source=source, provider_reference=reference, reason="NO_MATCHING_ESCROW",
            raw_payload="{}", amount=amount, phone_number=phone, mpesa_receipt_number=f"R-{reference}")
        await db.commit()
        return row.id


async def _balance():
    """CREDIT minus DEBIT on UNAPPLIED_PAYMENTS."""
    async with AsyncSessionLocal() as s:
        acct = await s.scalar(select(LedgerAccount).where(LedgerAccount.code == "UNAPPLIED_PAYMENTS"))
        if acct is None:
            return 0
        credit = await s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.account_id == acct.id, LedgerEntry.direction == LedgerEntryDirection.CREDIT))
        debit = await s.scalar(select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
            LedgerEntry.account_id == acct.id, LedgerEntry.direction == LedgerEntryDirection.DEBIT))
        return credit - debit


def _req(**overrides):
    import escrow_main

    fields = dict(outcome="REFUNDED_EXTERNALLY", note="Refunded the buyer by hand", refund_reference="SKJ9ABC123")
    fields.update(overrides)
    return escrow_main.ResolveUnappliedRequest(**fields)


async def _resolve(payment_id, **overrides):
    import escrow_main

    return await escrow_main.resolve_unapplied_payment(payment_id, _req(**overrides), "finance-admin (test)")


async def _list(**kwargs):
    import escrow_main

    return await escrow_main.list_unapplied_payments(admin_actor="finance-admin (test)", **kwargs)


@pytest.mark.asyncio
async def test_list_shows_open_payments_with_the_phone_masked():
    await _hold()
    rows = await _list()
    assert len(rows) == 1 and rows[0]["status"] == "OPEN"
    assert rows[0]["amount"] == "500.00"
    assert rows[0]["phone"] != "254700000001" and "*" in rows[0]["phone"]


@pytest.mark.asyncio
async def test_list_filters_by_status_and_rejects_unknown_statuses():
    pid = await _hold()
    await _resolve(pid)
    assert await _list() == []
    assert [r["id"] for r in await _list(status="refunded_externally")] == [pid]
    with pytest.raises(HTTPException) as e:
        await _list(status="BOGUS")
    assert e.value.status_code == 422


@pytest.mark.asyncio
async def test_resolve_clears_the_ledger_and_records_the_reference():
    pid = await _hold()
    assert await _balance() == 500
    out = await _resolve(pid)
    assert out["status"] == "REFUNDED_EXTERNALLY"
    assert await _balance() == 0
    async with AsyncSessionLocal() as s:
        row = await s.get(UnappliedPayment, pid)
        assert row.status == "REFUNDED_EXTERNALLY" and row.resolved_at is not None
        assert "SKJ9ABC123" in row.resolution_note
        audit = (await s.execute(select(AuditLogEntry).where(AuditLogEntry.action == "unapplied_payment.resolve"))).scalars().all()
        assert len(audit) == 1 and "SKJ9ABC123" in audit[0].detail


@pytest.mark.asyncio
async def test_a_payment_cannot_be_resolved_twice():
    pid = await _hold()
    await _resolve(pid)
    with pytest.raises(HTTPException) as e:
        await _resolve(pid)
    assert e.value.status_code == 409
    assert await _balance() == 0                        # cleared once, not twice


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["WRITTEN_OFF", "APPLIED", "BOGUS"])
async def test_only_refunded_externally_is_supported(outcome):
    pid = await _hold()
    with pytest.raises(HTTPException) as e:
        await _resolve(pid, outcome=outcome)
    assert e.value.status_code == 422
    assert await _balance() == 500                      # nothing moved


@pytest.mark.asyncio
async def test_unknown_payment_is_a_404():
    with pytest.raises(HTTPException) as e:
        await _resolve(9999)
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_a_refund_reference_and_note_are_required():
    from pydantic import ValidationError

    for bad in (dict(refund_reference=""), dict(refund_reference="ab"), dict(note="")):
        with pytest.raises(ValidationError):
            _req(**bad)


@pytest.mark.asyncio
async def test_a_payment_with_no_amount_can_still_be_resolved_without_touching_the_ledger():
    pid = await _hold(amount=None)
    assert await _balance() == 0
    out = await _resolve(pid)
    assert out["status"] == "REFUNDED_EXTERNALLY"
    assert await _balance() == 0


def test_both_endpoints_are_restricted_to_finance_admins():
    import escrow_main

    for path in ("/admin/unapplied-payments", "/admin/unapplied-payments/{payment_id}/resolve"):
        route = next(r for r in escrow_main.app.routes if getattr(r, "path", None) == path)
        names = [d.call.__qualname__ for d in route.dependant.dependencies]
        assert any("require_admin_role" in n for n in names), (path, names)