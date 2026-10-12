"""Tests written from reading the code that I expect to FAIL today.

Each one asserts the behaviour a money-handling system should have, and each
docstring says why the current code looks like it violates it. If one passes,
my reading was wrong and the test is just a normal regression test. If one
fails, read the failure before changing anything: it is either a real defect or
a wrong assumption in the test.
"""
import pytest
from fastapi import HTTPException
from decimal import Decimal

import escrow_main
from escrow_models import (
    EscrowStatus, EscrowTransaction, Order, OrderStatus, Payout, UnmatchedC2BPayment,
    post_ledger_transaction,
)
from support import FakeRequest, ago, count, ledger_net, make_escrow, make_payout, reload
from test_c2b_flow import REF, body, confirm, validate, spy, pending  # noqa: F401  (fixtures)


# ---- 1. Late failure callback after an admin reconcile-timeout double-reverses -------

async def test_late_failure_callback_after_reconcile_timeout_does_not_reverse_twice(db, provider, monkeypatch):
    """reconcile_timeout_payout reverses the reservation under idempotency key
    `mpesa-settle-reverse-{id}-{attempt}`. _handle_payout_result_callback's
    failure branch reverses the same reservation under a DIFFERENT key
    (`mpesa_b2c-reverse-{id}-{attempt}`), and only skips when the payout is
    COMPLETED - not when it is already FAILED. So a Result callback that
    arrives after the admin has reconciled posts a second reversal, leaving
    MPESA_CLEARING debited twice for one reservation."""
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="TIMEOUT", attempt_count=1, conversation_id="AG_LATE",
                          originator_conversation_id="escrow-payout-x-1")
    await post_ledger_transaction(
        db, reference=f"MPESA-SETTLE-{p.id}-1", idempotency_key=f"mpesa-settle-{p.id}-1",
        description="reservation", escrow_transaction_id=tx.id,
        debit_account_code="RIDER_PAYABLE", credit_account_code="MPESA_CLEARING", amount=p.amount)
    await db.commit()
    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("-500.00")

    await escrow_main.reconcile_timeout_payout(p.id, FakeRequest({}), admin_actor="test-admin")
    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("0")  # reserved then reversed once

    late_failure = {"Result": {"ResultCode": 2001, "ResultDesc": "The initiator information is invalid.",
                               "ConversationID": "AG_LATE", "OriginatorConversationID": "escrow-payout-x-1"}}
    await escrow_main.b2c_callback(FakeRequest(late_failure), x_mpesa_secret=escrow_main.MPESA_CALLBACK_SECRET)

    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("0"), "reservation reversed twice"


# ---- 2/3/4. C2B validation for an EXISTING order ------------------------------------

async def test_validation_accepts_a_matching_pending_order(pending):
    """c2b_validation loads the Order and then reads `order.escrow` (a one-to-one
    relationship, default lazy="select") on an AsyncSession with no
    selectinload. SQLAlchemy cannot lazy-load there and raises MissingGreenlet,
    so the endpoint would fail for exactly the requests it exists to accept."""
    assert (await validate(body()))["ResultCode"] == "0"


async def test_validation_rejects_wrong_amount(pending):
    assert (await validate(body(amount=499)))["ResultCode"] == "C2B00013"


async def test_validation_rejects_already_paid_order(pending):
    await confirm(body())
    assert (await validate(body(receipt="TXN0002")))["ResultCode"] == "C2B00012"


# ---- 5. Fractional amounts are truncated, then treated as an exact match ------------

async def test_fractional_overpayment_is_not_an_exact_match(pending):
    """`int(Decimal("500.50"))` is 500, so a 500.50 payment matches a 500.00
    order and the escrow is held with stk_amount=500.50 while the ledger books
    500.00 - the extra 0.50 is silently unaccounted for. Any non-integral
    amount should go to manual reconciliation."""
    await confirm(body(amount="500.50"))

    assert (await reload(EscrowTransaction, pending.id)).status == EscrowStatus.PENDING_PAYMENT
    assert await count(UnmatchedC2BPayment) == 1


# ---- 6. Garbage TransAmount makes the confirmation raise instead of recording it ----

async def test_unparseable_amount_is_recorded_not_a_500(pending):
    """Decimal(str("abc")) raises decimal.InvalidOperation before any DB work.
    Safaricom will retry a non-2xx confirmation, but the money has already
    moved; the handler's own contract is 'record it and acknowledge'."""
    result = await confirm(body(amount="abc"))

    assert result["ResultCode"] == "0"
    assert await count(UnmatchedC2BPayment) == 1


# ---- 7. Expiry refund for an escrow that belongs to an Order ------------------------

async def test_expiry_sweep_refunds_an_expired_order_backed_escrow(db, run_once, provider):
    """expiry_loop selects EscrowTransaction with no eager options and
    expire_one then reads `tx.order`. With the Order not already in the
    session that is a lazy load on an AsyncSession (MissingGreenlet); the loop
    catches and logs it, the transaction rolls back, and the same escrow fails
    identically every sweep - so expired orders never get refunded.
    (Escrows with no order, as in test_reconciliation_sweep, do not hit this.)"""
    tx = await make_escrow(db, status=EscrowStatus.ESCROW_HELD, order_ref="ORD-EXP-1",
                           qr_token_expires_at=ago(minutes=5))
    await db.commit()

    await run_once(escrow_main.expiry_loop)  # also fails if the sweep logged an error

    cur = await reload(EscrowTransaction, tx.id)
    assert cur.status == EscrowStatus.REFUND_INITIATED
    assert (await reload(Order, cur.order_id)).status == OrderStatus.REFUND_PENDING
    assert await count(Payout, Payout.escrow_transaction_id == tx.id) == 1


# ---- Regression tests for the three follow-up fixes ---------------------------------

async def test_apply_unmatched_c2b_to_order_credits_the_escrow(db):
    """Was: MissingGreenlet on order.escrow, same pattern as c2b_validation and
    the expiry sweep. Fixed by lazy="selectin" on Order.escrow."""
    from escrow_main import ApplyUnmatchedC2BRequest
    from escrow_models import UnmatchedC2BPayment
    tx = await make_escrow(db, status=EscrowStatus.PENDING_PAYMENT, amount="500.00", order_ref="ORD-APPLY-1")
    row = UnmatchedC2BPayment(mpesa_receipt_number="TXNAPPLY1", amount=Decimal("500.00"),
                              msisdn="254711111111", bill_ref_number="WRONG-REF", resolved=False,
                              status="UNMATCHED")
    db.add(row)
    await db.commit()

    result = await escrow_main.apply_unmatched_c2b_to_order(
        row.id, ApplyUnmatchedC2BRequest(order_id=tx.order_id), admin_actor="test-admin")

    assert result["escrow_id"] == tx.id
    cur = await reload(EscrowTransaction, tx.id)
    assert cur.status == EscrowStatus.ESCROW_HELD and cur.mpesa_receipt_number == "TXNAPPLY1"
    assert (await reload(Order, tx.order_id)).status == OrderStatus.PAID
    assert await ledger_net("ESCROW_HOLDING", tx.id) == Decimal("-500.00")


async def test_apply_unmatched_c2b_rejects_amount_mismatch_without_mutating_anything(db):
    from escrow_main import ApplyUnmatchedC2BRequest
    from escrow_models import UnmatchedC2BPayment
    tx = await make_escrow(db, status=EscrowStatus.PENDING_PAYMENT, amount="500.00", order_ref="ORD-APPLY-2")
    row = UnmatchedC2BPayment(mpesa_receipt_number="TXNAPPLY2", amount=Decimal("499.00"),
                              msisdn="254711111111", bill_ref_number="WRONG-REF", resolved=False,
                              status="UNMATCHED")
    db.add(row)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await escrow_main.apply_unmatched_c2b_to_order(
            row.id, ApplyUnmatchedC2BRequest(order_id=tx.order_id), admin_actor="test-admin")
    assert exc.value.status_code == 422
    assert (await reload(EscrowTransaction, tx.id)).status == EscrowStatus.PENDING_PAYMENT


async def test_late_success_callback_after_reconcile_alerts_and_does_not_finalize(db, provider):
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="TIMEOUT", attempt_count=1, conversation_id="AG_LS",
                          originator_conversation_id="escrow-payout-y-1")
    await post_ledger_transaction(
        db, reference=f"MPESA-SETTLE-{p.id}-1", idempotency_key=f"mpesa-settle-{p.id}-1",
        description="reservation", escrow_transaction_id=tx.id,
        debit_account_code="RIDER_PAYABLE", credit_account_code="MPESA_CLEARING", amount=p.amount)
    await db.commit()

    await escrow_main.reconcile_timeout_payout(p.id, FakeRequest({}), admin_actor="test-admin")
    assert (await reload(Payout, p.id)).status == "FAILED"

    late_success = {"Result": {"ResultCode": 0, "ResultDesc": "ok", "ConversationID": "AG_LS",
                               "OriginatorConversationID": "escrow-payout-y-1",
                               "ResultParameters": {"ResultParameter": [
                                   {"Key": "TransactionReceipt", "Value": "RCPTLATE"},
                                   {"Key": "TransactionAmount", "Value": 500}]}}}
    await escrow_main.b2c_callback(FakeRequest(late_success), x_mpesa_secret=escrow_main.MPESA_CALLBACK_SECRET)

    assert (await reload(Payout, p.id)).status == "FAILED"  # not silently flipped to COMPLETED
    assert (await reload(EscrowTransaction, tx.id)).status != EscrowStatus.RELEASED
    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("0")  # still reversed, not re-reserved
    assert "PAYOUT_LATE_SUCCESS_AFTER_RECONCILE" in [a[0] for a in provider.alerts]


async def test_payout_retry_stops_and_dead_letters_after_the_attempt_cap(db, provider, monkeypatch):
    monkeypatch.setattr(escrow_main, "PAYOUT_RETRY_BACKOFF_SECONDS", 0)  # this test retries immediately; the backoff has its own tests
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="PENDING")
    await db.commit()
    provider.b2c_exc = escrow_main.mpesa.MpesaRejected("bad shortcode")

    for _ in range(escrow_main.PAYOUT_MAX_ATTEMPTS):
        await escrow_main.retry_failed_payouts(db, None) if (await reload(Payout, p.id)).status == "FAILED" else \
            await escrow_main.initiate_payout_for_record(db, p, None)
        await db.commit()

    cur = await reload(Payout, p.id)
    assert cur.attempt_count == escrow_main.PAYOUT_MAX_ATTEMPTS
    assert cur.status == "FAILED"

    calls_before = len(provider.b2c_calls)
    await escrow_main.retry_failed_payouts(db, None)  # at the cap: must not call the provider again
    await db.commit()

    assert len(provider.b2c_calls) == calls_before
    assert (await reload(Payout, p.id)).dead_lettered_at is not None
    assert "PAYOUT_DEAD_LETTER" in [a[0] for a in provider.alerts]

    provider.alerts.clear()
    await escrow_main.retry_failed_payouts(db, None)  # already dead-lettered: no repeat alert
    await db.commit()
    assert provider.alerts == []


# ---- More apply_unmatched_c2b_to_order edge cases ------------------------------------

async def test_apply_unmatched_c2b_rejects_an_already_resolved_row(db):
    from escrow_main import ApplyUnmatchedC2BRequest
    from escrow_models import UnmatchedC2BPayment
    tx = await make_escrow(db, status=EscrowStatus.PENDING_PAYMENT, amount="500.00", order_ref="ORD-APPLY-3")
    row = UnmatchedC2BPayment(mpesa_receipt_number="TXNAPPLY3", amount=Decimal("500.00"),
                              msisdn="254711111111", bill_ref_number="WRONG-REF",
                              resolved=True, status="APPLIED_TO_ORDER")
    db.add(row)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await escrow_main.apply_unmatched_c2b_to_order(
            row.id, ApplyUnmatchedC2BRequest(order_id=tx.order_id), admin_actor="test-admin")
    assert exc.value.status_code == 409
    assert (await reload(EscrowTransaction, tx.id)).status == EscrowStatus.PENDING_PAYMENT


async def test_apply_unmatched_c2b_rejects_a_nonexistent_payment_id(db):
    from escrow_main import ApplyUnmatchedC2BRequest
    with pytest.raises(HTTPException) as exc:
        await escrow_main.apply_unmatched_c2b_to_order(999999, ApplyUnmatchedC2BRequest(order_id=1),
                                                        admin_actor="test-admin")
    assert exc.value.status_code == 404


async def test_apply_unmatched_c2b_rejects_a_nonexistent_order_id(db):
    from escrow_main import ApplyUnmatchedC2BRequest
    from escrow_models import UnmatchedC2BPayment
    row = UnmatchedC2BPayment(mpesa_receipt_number="TXNAPPLY4", amount=Decimal("500.00"),
                              msisdn="254711111111", bill_ref_number="WRONG-REF", resolved=False,
                              status="UNMATCHED")
    db.add(row)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await escrow_main.apply_unmatched_c2b_to_order(
            row.id, ApplyUnmatchedC2BRequest(order_id=999999), admin_actor="test-admin")
    assert exc.value.status_code == 404
    assert (await reload(UnmatchedC2BPayment, row.id)).resolved is False


async def test_apply_unmatched_c2b_rejects_an_order_not_awaiting_payment(db):
    """The order's escrow is already ESCROW_HELD (or any non-PENDING_PAYMENT
    state) - applying a second payment on top must not double-hold it."""
    from escrow_main import ApplyUnmatchedC2BRequest
    from escrow_models import UnmatchedC2BPayment
    tx = await make_escrow(db, status=EscrowStatus.ESCROW_HELD, amount="500.00", order_ref="ORD-APPLY-5")
    row = UnmatchedC2BPayment(mpesa_receipt_number="TXNAPPLY5", amount=Decimal("500.00"),
                              msisdn="254711111111", bill_ref_number="WRONG-REF", resolved=False,
                              status="UNMATCHED")
    db.add(row)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await escrow_main.apply_unmatched_c2b_to_order(
            row.id, ApplyUnmatchedC2BRequest(order_id=tx.order_id), admin_actor="test-admin")
    assert exc.value.status_code == 409
    assert await ledger_net("ESCROW_HOLDING", tx.id) == Decimal("0")  # no second hold posted


async def test_apply_unmatched_c2b_rejects_a_receipt_already_on_another_escrow(db):
    """CORRECTIONS.md v20-#13: the same M-Pesa receipt can't be applied to
    two different escrows - this is the explicit pre-check for that."""
    from escrow_main import ApplyUnmatchedC2BRequest
    from escrow_models import UnmatchedC2BPayment
    other = await make_escrow(db, status=EscrowStatus.ESCROW_HELD, amount="500.00",
                              mpesa_receipt_number="SHARED1")
    target = await make_escrow(db, status=EscrowStatus.PENDING_PAYMENT, amount="500.00", order_ref="ORD-APPLY-6")
    row = UnmatchedC2BPayment(mpesa_receipt_number="SHARED1", amount=Decimal("500.00"),
                              msisdn="254711111111", bill_ref_number="WRONG-REF", resolved=False,
                              status="UNMATCHED")
    db.add(row)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await escrow_main.apply_unmatched_c2b_to_order(
            row.id, ApplyUnmatchedC2BRequest(order_id=target.order_id), admin_actor="test-admin")
    assert exc.value.status_code == 409
    assert str(other.id) in str(exc.value.detail)
    assert (await reload(EscrowTransaction, target.id)).status == EscrowStatus.PENDING_PAYMENT


async def test_apply_unmatched_c2b_sends_sms_and_notifies_seller_exactly_once(db, monkeypatch):
    from escrow_main import ApplyUnmatchedC2BRequest
    from escrow_models import UnmatchedC2BPayment
    sms_calls, seller_calls = [], []
    async def fake_sms(db, phone, message, key): sms_calls.append((phone, key))
    async def fake_seller(tx_id): seller_calls.append(tx_id)
    monkeypatch.setattr(escrow_main, "send_sms", fake_sms)
    monkeypatch.setattr(escrow_main, "notify_seller_escrow_held_after_commit", fake_seller)

    tx = await make_escrow(db, status=EscrowStatus.PENDING_PAYMENT, amount="500.00", order_ref="ORD-APPLY-7")
    row = UnmatchedC2BPayment(mpesa_receipt_number="TXNAPPLY7", amount=Decimal("500.00"),
                              msisdn="254711111111", bill_ref_number="WRONG-REF", resolved=False,
                              status="UNMATCHED")
    db.add(row)
    await db.commit()

    await escrow_main.apply_unmatched_c2b_to_order(
        row.id, ApplyUnmatchedC2BRequest(order_id=tx.order_id), admin_actor="test-admin")

    assert len(sms_calls) == 1 and sms_calls[0][0] == tx.buyer_phone
    assert seller_calls == [tx.id]
