"""N6: recovering STK payments whose callback never arrived. A failed or
unreadable QUERY must never cancel an order; a PAID answer is parked for an
admin (it carries no receipt); a late real callback is never double-counted."""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerTransaction, Order, OrderStatus,
    PayeeRole, UnappliedPayment,
)


def _ago(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


async def _pending(*, age=timedelta(minutes=10), checkout="ws_CO_R1", status=EscrowStatus.PENDING_PAYMENT,
                   queried_ago=None, with_order=False):
    async with AsyncSessionLocal() as db:
        order_id = None
        if with_order:
            order = Order(public_ref=f"R{secrets.token_hex(3)}", buyer_phone="254700000001",
                          amount=Decimal("500.00"), delivery_fee=Decimal("0.00"), delivery_address="x",
                          status=OrderStatus.PAYMENT_PENDING)
            db.add(order)
            await db.flush()
            order_id = order.id
        tx = EscrowTransaction(
            order_id=order_id, buyer_phone="254700000001", payee_phone="254700000002",
            payee_role=PayeeRole.RIDER, amount=Decimal("500.00"), status=status,
            buyer_access_token=secrets.token_hex(16), mpesa_checkout_request_id=checkout,
            created_at=datetime.now(timezone.utc) - age,
            stk_last_queried_at=(_ago(**queried_ago) if queried_ago else None))
        db.add(tx)
        await db.flush()
        ids = (tx.id, order_id)
        await db.commit()
        return ids


@pytest.fixture
def stk(monkeypatch, provider):
    import escrow_main

    s = type("S", (), {})()
    s.calls = []
    s.answer = {"outcome": "UNKNOWN", "result_code": None, "result_desc": None}
    s.exc = None

    async def fake_query(client, checkout_request_id):
        s.calls.append(checkout_request_id)
        if s.exc:
            raise s.exc
        return s.answer

    monkeypatch.setattr(escrow_main.mpesa, "query_stk_status", fake_query)
    return s


async def _tx(tx_id):
    async with AsyncSessionLocal() as s:
        return await s.get(EscrowTransaction, tx_id)


async def _sweep():
    import escrow_main

    await escrow_main.stk_recovery_sweep(None)


@pytest.mark.asyncio
async def test_paid_is_parked_for_an_admin_and_alerted(stk, provider):
    tx_id, _ = await _pending()
    stk.answer = {"outcome": "PAID", "result_code": 0, "result_desc": "ok"}
    await _sweep()
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.STK_INITIATION_UNKNOWN
    assert tx.mpesa_receipt_number is None                       # never invented
    assert ("STK_PAID_NO_CALLBACK", 0, tx_id) in provider.alerts


@pytest.mark.asyncio
async def test_failed_cancels_the_escrow_and_the_order(stk, provider):
    tx_id, order_id = await _pending(with_order=True)
    stk.answer = {"outcome": "FAILED", "result_code": 1032, "result_desc": "cancelled"}
    await _sweep()
    assert (await _tx(tx_id)).status == EscrowStatus.PAYMENT_FAILED
    async with AsyncSessionLocal() as s:
        assert (await s.get(Order, order_id)).status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_unknown_changes_nothing(stk, provider):
    tx_id, _ = await _pending()
    await _sweep()
    assert (await _tx(tx_id)).status == EscrowStatus.PENDING_PAYMENT
    assert provider.alerts == []


@pytest.mark.asyncio
async def test_a_failed_query_never_cancels_the_order(stk, provider):
    tx_id, order_id = await _pending(with_order=True)
    stk.exc = RuntimeError("safaricom unreachable")
    await _sweep()
    assert (await _tx(tx_id)).status == EscrowStatus.PENDING_PAYMENT
    async with AsyncSessionLocal() as s:
        assert (await s.get(Order, order_id)).status == OrderStatus.PAYMENT_PENDING


@pytest.mark.asyncio
async def test_too_young_escrows_are_left_alone(stk):
    await _pending(age=timedelta(seconds=30))
    await _sweep()
    assert stk.calls == []


@pytest.mark.asyncio
async def test_too_old_escrows_are_left_for_an_admin(stk):
    await _pending(age=timedelta(hours=30))
    await _sweep()
    assert stk.calls == []


@pytest.mark.asyncio
async def test_an_escrow_without_a_checkout_id_is_ignored(stk):
    await _pending(checkout=None)
    await _sweep()
    assert stk.calls == []


@pytest.mark.asyncio
async def test_other_states_are_ignored(stk):
    await _pending(status=EscrowStatus.ESCROW_HELD, checkout="ws_CO_H")
    await _pending(status=EscrowStatus.PAYMENT_FAILED, checkout="ws_CO_F")
    await _sweep()
    assert stk.calls == []


@pytest.mark.asyncio
async def test_each_escrow_is_asked_once_per_cooldown(stk):
    await _pending()
    await _sweep()
    await _sweep()
    assert len(stk.calls) == 1


@pytest.mark.asyncio
async def test_a_recently_queried_escrow_is_skipped(stk):
    await _pending(queried_ago=dict(minutes=2))
    await _sweep()
    assert stk.calls == []


@pytest.mark.asyncio
async def test_an_escrow_queried_long_ago_is_asked_again(stk):
    await _pending(queried_ago=dict(minutes=30))
    await _sweep()
    assert len(stk.calls) == 1


@pytest.mark.asyncio
async def test_one_failing_escrow_does_not_block_the_others(stk, provider, monkeypatch):
    import escrow_main

    first, _ = await _pending(checkout="ws_CO_A")
    second, _ = await _pending(checkout="ws_CO_B")
    stk.answer = {"outcome": "FAILED", "result_code": 1032, "result_desc": "cancelled"}
    real = escrow_main._recover_one_stk

    async def flaky(tx_id, client):
        if tx_id == first:
            raise RuntimeError("boom")
        return await real(tx_id, client)

    monkeypatch.setattr(escrow_main, "_recover_one_stk", flaky)
    await _sweep()
    assert (await _tx(first)).status == EscrowStatus.PENDING_PAYMENT
    assert (await _tx(second)).status == EscrowStatus.PAYMENT_FAILED


@pytest.mark.asyncio
async def test_a_real_callback_that_lands_mid_query_is_not_overwritten(stk, provider):
    """The callback applies the payment while the status query is in flight; the
    sweep must notice the escrow moved on and change nothing."""
    from tests.test_stk_callback_flow import _body, _call

    tx_id, _ = await _pending(checkout="ws_CO_RACE")

    async def racing_query(client, checkout_request_id):
        await _call(_body(checkout="ws_CO_RACE"))                # the real callback arrives now
        return {"outcome": "FAILED", "result_code": 1032, "result_desc": "cancelled"}

    import escrow_main
    stk_real = escrow_main.mpesa.query_stk_status
    escrow_main.mpesa.query_stk_status = racing_query
    try:
        await _sweep()
    finally:
        escrow_main.mpesa.query_stk_status = stk_real
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.ESCROW_HELD and tx.mpesa_receipt_number == "QWE123"


@pytest.mark.asyncio
async def test_a_real_callback_after_the_sweep_cancelled_is_held_not_lost(stk, provider):
    """The sweep wrongly cancelled (the customer paid after all); the real callback
    then arrives. The money must land in unapplied_payments, never vanish."""
    from tests.test_stk_callback_flow import _body, _call

    tx_id, _ = await _pending(checkout="ws_CO_LATE")
    stk.answer = {"outcome": "FAILED", "result_code": 1032, "result_desc": "cancelled"}
    await _sweep()
    assert (await _tx(tx_id)).status == EscrowStatus.PAYMENT_FAILED
    await _call(_body(checkout="ws_CO_LATE"))
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(select(UnappliedPayment))).scalars().all()
    assert len(rows) == 1 and rows[0].reason == "ESCROW_NOT_AWAITING_PAYMENT"
    assert (await _tx(tx_id)).status == EscrowStatus.PAYMENT_FAILED


@pytest.mark.asyncio
async def test_a_real_callback_after_a_paid_answer_applies_normally(stk, provider):
    """The sweep parked the escrow as STK_INITIATION_UNKNOWN; the real callback
    then arrives. That is held, not double-applied; an admin finishes it."""
    from tests.test_stk_callback_flow import _body, _call

    tx_id, _ = await _pending(checkout="ws_CO_PAID")
    stk.answer = {"outcome": "PAID", "result_code": 0, "result_desc": "ok"}
    await _sweep()
    await _call(_body(checkout="ws_CO_PAID"))
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.STK_INITIATION_UNKNOWN
    async with AsyncSessionLocal() as s:
        keys = (await s.execute(select(LedgerTransaction.idempotency_key)
                                .where(LedgerTransaction.escrow_transaction_id == tx_id))).scalars().all()
        held = (await s.execute(select(UnappliedPayment))).scalars().all()
    assert f"mpesa-hold-{tx_id}" not in keys
    assert len(held) == 1