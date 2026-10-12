"""USSD handler: cancel / pay / check, as they behave today. The handler had no
behavioural tests; these pin its current behaviour before it is changed."""
import secrets
from decimal import Decimal
from types import SimpleNamespace

import pytest

import escrow_core as mpesa
from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, Order, OrderStatus, PayeeRole,
)
from tests.support import FakeRequest

BUYER = "254700000001"


@pytest.fixture
def ussd(monkeypatch, provider):
    import escrow_main

    monkeypatch.setattr(escrow_main.mpesa, "USSD_CALLBACK_SECRET", "ussd-test-secret")
    s = SimpleNamespace(pushes=[], exc=None)

    async def fake_push(client, phone, amount, ref, desc):
        s.pushes.append((phone, amount, ref))
        if s.exc:
            raise s.exc
        n = len(s.pushes)
        return {"CheckoutRequestID": f"ws_CO_U{n}", "MerchantRequestID": f"m{n}"}

    monkeypatch.setattr(escrow_main.mpesa, "initiate_stk_push", fake_push)
    return s


async def _dial(session_id, text, phone=BUYER):
    import escrow_main

    p = escrow_main.USSDRequest(sessionId=session_id, phoneNumber=phone, text=text)
    return await escrow_main.ussd_callback(FakeRequest(None), p, "ussd-test-secret")


async def _order(ref="ORD1", *, status=EscrowStatus.PENDING_PAYMENT, checkout=None, amount="500.00"):
    async with AsyncSessionLocal() as db:
        order = Order(public_ref=ref, buyer_phone=BUYER, amount=Decimal(amount), delivery_fee=Decimal("0.00"),
                      delivery_address="x", status=OrderStatus.PAYMENT_PENDING)
        db.add(order)
        await db.flush()
        tx = EscrowTransaction(order_id=order.id, buyer_phone=BUYER, payee_phone="254700000002",
                               payee_role=PayeeRole.RIDER, amount=Decimal(amount), status=status,
                               buyer_access_token=secrets.token_hex(16), mpesa_checkout_request_id=checkout)
        db.add(tx)
        await db.flush()
        ids = (order.id, tx.id)
        await db.commit()
        return ids


async def _tx(tx_id):
    async with AsyncSessionLocal() as s:
        return await s.get(EscrowTransaction, tx_id)


async def _order_row(order_id):
    async with AsyncSessionLocal() as s:
        return await s.get(Order, order_id)


@pytest.mark.asyncio
async def test_check_order_reports_its_status(ussd):
    await _order()
    assert (await _dial("s1", "1")).startswith("CON")
    out = await _dial("s1", "1*ORD1")
    assert out.startswith("END ORD1:") and "KES 500" in out


@pytest.mark.asyncio
async def test_cancel_before_any_payment_request(ussd):
    order_id, tx_id = await _order()
    await _dial("s2", "3")
    out = await _dial("s2", "3*ORD1")
    assert out == "END Order cancelled."
    assert (await _tx(tx_id)).status == EscrowStatus.PAYMENT_FAILED
    assert (await _order_row(order_id)).status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_cancel_after_payment_is_refused(ussd):
    _, tx_id = await _order(status=EscrowStatus.ESCROW_HELD)
    await _dial("s3", "3")
    out = await _dial("s3", "3*ORD1")
    assert out == "END This order can no longer be cancelled."
    assert (await _tx(tx_id)).status == EscrowStatus.ESCROW_HELD


@pytest.mark.asyncio
async def test_pay_sends_exactly_one_stk_push(ussd):
    _, tx_id = await _order()
    await _dial("s4", "2")
    out = await _dial("s4", "2*ORD1")
    assert out.startswith("END Payment request sent")
    assert len(ussd.pushes) == 1
    assert (await _tx(tx_id)).mpesa_checkout_request_id == "ws_CO_U1"


@pytest.mark.asyncio
async def test_pay_while_a_request_is_already_in_flight_sends_nothing(ussd):
    await _order(checkout="ws_CO_EXISTING")
    await _dial("s5", "2")
    out = await _dial("s5", "2*ORD1")
    assert out.startswith("CON Payment already in progress")
    assert ussd.pushes == []


@pytest.mark.asyncio
async def test_a_confirmed_rejection_leaves_the_escrow_payable(ussd, provider):
    _, tx_id = await _order()
    ussd.exc = mpesa.MpesaRejected("bad shortcode")
    await _dial("s6", "2")
    out = await _dial("s6", "2*ORD1")
    assert out.startswith("END M-Pesa rejected")
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.PENDING_PAYMENT and tx.mpesa_checkout_request_id is None


@pytest.mark.asyncio
async def test_an_unknown_outcome_blocks_a_retry_and_alerts(ussd, provider):
    # C2 changed what escrow_core raises for an HTTP 5xx, so USSD must now land here.
    _, tx_id = await _order()
    ussd.exc = mpesa.MpesaOutcomeUnknown("HTTP 503: outcome unknown")
    await _dial("s7", "2")
    out = await _dial("s7", "2*ORD1")
    assert out.startswith("END We could not confirm")
    assert (await _tx(tx_id)).status == EscrowStatus.STK_INITIATION_UNKNOWN
    assert ("STK_INITIATION_UNKNOWN", 0, tx_id) in provider.alerts


@pytest.mark.asyncio
async def test_someone_elses_order_is_not_found(ussd):
    await _order()
    await _dial("s8", "1", phone="254799999999")
    out = await _dial("s8", "1*ORD1", phone="254799999999")
    assert out == "END Order not found."

# ---- N7 / N8: cancel while a payment is in flight, and overlapping pay requests ----

@pytest.mark.asyncio
async def test_cancel_is_refused_while_a_payment_request_is_in_flight(ussd):
    order_id, tx_id = await _order(checkout="ws_CO_INFLIGHT")
    await _dial("s9", "3")
    out = await _dial("s9", "3*ORD1")
    assert out.startswith("END Your payment is still being processed")
    tx = await _tx(tx_id)
    assert tx.status == EscrowStatus.PENDING_PAYMENT              # not cancelled
    assert (await _order_row(order_id)).status == OrderStatus.PAYMENT_PENDING


@pytest.mark.asyncio
async def test_cancel_still_works_when_no_payment_request_was_ever_sent(ussd):
    order_id, tx_id = await _order(checkout=None)
    await _dial("s10", "3")
    assert await _dial("s10", "3*ORD1") == "END Order cancelled."
    assert (await _tx(tx_id)).status == EscrowStatus.PAYMENT_FAILED


@pytest.mark.asyncio
async def test_cancel_after_the_pay_request_is_refused_end_to_end(ussd):
    # the real sequence: pay, then try to cancel straight away
    _, tx_id = await _order()
    await _dial("s11", "2")
    assert (await _dial("s11", "2*ORD1")).startswith("END Payment request sent")
    await _dial("s12", "3")
    out = await _dial("s12", "3*ORD1")
    assert out.startswith("END Your payment is still being processed")
    assert (await _tx(tx_id)).status == EscrowStatus.PENDING_PAYMENT


@pytest.mark.asyncio
async def test_two_pay_requests_in_a_row_send_one_push(ussd):
    await _order()
    await _dial("s13", "2")
    await _dial("s13", "2*ORD1")
    await _dial("s14", "2")
    out = await _dial("s14", "2*ORD1")
    assert out.startswith("CON Payment already in progress")
    assert len(ussd.pushes) == 1


@pytest.mark.asyncio
async def test_overlapping_pay_requests_send_one_push(ussd):
    """Two USSD sessions for the same order arriving at the same moment. SQLite
    ignores row locks, so this proves the handler's own re-check; the
    PostgreSQL CI step proves the lock itself."""
    import asyncio

    await _order()
    await _dial("s15", "2")
    await _dial("s16", "2")
    await asyncio.gather(_dial("s15", "2*ORD1"), _dial("s16", "2*ORD1"))
    assert len(ussd.pushes) == 1