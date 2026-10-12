"""C2B (direct Paybill) validation + confirmation.

Confirmation fires AFTER Safaricom has credited the shortcode, so the handler
can only ever acknowledge. The invariants under test:

  * money that matches a pending order holds that escrow exactly once
  * money that does NOT match is recorded for manual reconciliation, never lost
  * a replayed/duplicated callback changes nothing
  * an unauthenticated call has no side effects
"""
import pytest
from decimal import Decimal

from fastapi import HTTPException

import escrow_main
from escrow_models import (
    EscrowStatus, EscrowTransaction, LedgerTransaction, Order, OrderStatus,
    ReconciliationRecord, UnmatchedC2BPayment, AsyncSessionLocal,
)
from sqlalchemy import select
from support import FakeRequest, count, ledger_net, make_escrow, reload

REF = "ORD-C2B-0001"
SHORTCODE = "174379"


@pytest.fixture(autouse=True)
def spy(monkeypatch):
    monkeypatch.setattr(escrow_main.mpesa, "MPESA_SHORTCODE", SHORTCODE)
    s = type("Spy", (), {})()
    s.sms, s.alerts, s.seller_notified = [], [], []

    async def fake_sms(db, phone, message, key):
        s.sms.append((phone, key))

    async def fake_alert(kind, payout_id, escrow_id, detail):
        s.alerts.append(kind)

    async def fake_seller(tx_id):
        s.seller_notified.append(tx_id)

    monkeypatch.setattr(escrow_main, "send_sms", fake_sms)
    monkeypatch.setattr(escrow_main, "alert_payout_issue", fake_alert)
    monkeypatch.setattr(escrow_main, "notify_seller_escrow_held_after_commit", fake_seller)
    return s


def body(ref=REF, amount=500, receipt="TXN0001", msisdn="254711111111", shortcode=SHORTCODE):
    return {
        "TransactionType": "Pay Bill", "TransID": receipt, "TransTime": "20260919101500",
        "TransAmount": amount, "BusinessShortCode": shortcode, "BillRefNumber": ref,
        "MSISDN": msisdn, "FirstName": "Jane",
    }


async def confirm(b, secret=None):
    secret = escrow_main.MPESA_CALLBACK_SECRET if secret is None else secret
    return await escrow_main.c2b_confirmation(FakeRequest(b), x_mpesa_secret=secret)


async def validate(b, secret=None):
    secret = escrow_main.MPESA_CALLBACK_SECRET if secret is None else secret
    return await escrow_main.c2b_validation(FakeRequest(b), x_mpesa_secret=secret)


@pytest.fixture
async def pending(db):
    tx = await make_escrow(db, status=EscrowStatus.PENDING_PAYMENT, amount="500.00", order_ref=REF)
    await db.commit()
    return tx


async def assert_still_unpaid(tx):
    cur = await reload(EscrowTransaction, tx.id)
    assert cur.status == EscrowStatus.PENDING_PAYMENT
    assert cur.mpesa_receipt_number is None and cur.qr_token is None
    assert await count(LedgerTransaction, LedgerTransaction.escrow_transaction_id == tx.id) == 0
    assert (await reload(Order, cur.order_id)).status != OrderStatus.PAID


# ---------------------------------------------------------------- confirmation: match

async def test_matching_payment_holds_the_escrow_and_posts_one_ledger_entry_pair(pending, spy):
    assert await confirm(body()) == {"ResultCode": "0", "ResultDesc": "Accepted"}

    tx = await reload(EscrowTransaction, pending.id)
    assert tx.status == EscrowStatus.ESCROW_HELD
    assert tx.mpesa_receipt_number == "TXN0001"
    assert tx.payment_method == "C2B"
    assert tx.stk_amount == Decimal("500") and tx.stk_phone_number == "254711111111"
    assert tx.paid_at is not None
    assert tx.qr_token and tx.qr_view_token and tx.qr_token_expires_at is not None
    assert (await reload(Order, tx.order_id)).status == OrderStatus.PAID
    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("500.00")     # cash in
    assert await ledger_net("ESCROW_HOLDING", tx.id) == Decimal("-500.00")    # owed to buyer/payee
    assert await count(ReconciliationRecord, ReconciliationRecord.provider == "MPESA_C2B",
                       ReconciliationRecord.provider_reference == "TXN0001") == 1
    assert (tx.buyer_phone, f"qr-ready-buyer-{tx.id}") in spy.sms
    assert spy.seller_notified == [tx.id]
    assert await count(UnmatchedC2BPayment) == 0


@pytest.mark.parametrize("amount", [500, "500", "500.00", 500.0])
async def test_amount_is_matched_regardless_of_json_representation(pending, amount):
    await confirm(body(amount=amount))
    assert (await reload(EscrowTransaction, pending.id)).status == EscrowStatus.ESCROW_HELD


async def test_payer_phone_need_not_match_the_buyer(pending):
    # Someone paying the paybill on the buyer's behalf is normal for C2B.
    await confirm(body(msisdn="254799999999"))
    assert (await reload(EscrowTransaction, pending.id)).status == EscrowStatus.ESCROW_HELD


# ------------------------------------------------------- confirmation: idempotency

async def test_replayed_confirmation_changes_nothing(pending, spy):
    await confirm(body())
    first = await reload(EscrowTransaction, pending.id)

    assert await confirm(body()) == {"ResultCode": "0", "ResultDesc": "Accepted"}

    again = await reload(EscrowTransaction, pending.id)
    assert (again.qr_token, again.paid_at) == (first.qr_token, first.paid_at)
    assert await count(LedgerTransaction, LedgerTransaction.escrow_transaction_id == pending.id) == 1
    assert await count(ReconciliationRecord) == 1
    assert await count(UnmatchedC2BPayment) == 0
    assert len(spy.sms) == 1 and spy.seller_notified == [pending.id]


async def test_same_receipt_resent_with_a_different_bill_ref_is_acknowledged_quietly(pending):
    await confirm(body())

    assert (await confirm(body(ref="ORD-TYPO"))) == {"ResultCode": "0", "ResultDesc": "Accepted"}

    assert await count(UnmatchedC2BPayment) == 0
    assert await count(LedgerTransaction, LedgerTransaction.escrow_transaction_id == pending.id) == 1


async def test_replay_of_an_unmatched_receipt_does_not_create_a_second_record(pending):
    await confirm(body(amount=499))
    await confirm(body(amount=499))
    assert await count(UnmatchedC2BPayment) == 1


async def test_second_payment_to_an_already_paid_order_is_recorded_not_applied(pending, spy):
    await confirm(body(receipt="TXN0001"))

    await confirm(body(receipt="TXN0002"))  # customer paid twice

    tx = await reload(EscrowTransaction, pending.id)
    assert tx.mpesa_receipt_number == "TXN0001"
    assert await count(LedgerTransaction, LedgerTransaction.escrow_transaction_id == pending.id) == 1
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(UnmatchedC2BPayment))).scalar_one()
    assert row.mpesa_receipt_number == "TXN0002" and row.matched_order_id == tx.order_id
    assert row.resolved is False
    assert "UNMATCHED_C2B_PAYMENT" in spy.alerts


# --------------------------------------------------- confirmation: no automatic match

@pytest.mark.parametrize("amount", [499, 501, 0, 50000])
async def test_wrong_amount_is_recorded_for_manual_reconciliation(pending, spy, amount):
    assert (await confirm(body(amount=amount)))["ResultCode"] == "0"  # can't reject: money moved

    await assert_still_unpaid(pending)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(UnmatchedC2BPayment))).scalar_one()
    assert row.amount == Decimal(amount) and row.matched_order_id is not None
    assert "UNMATCHED_C2B_PAYMENT" in spy.alerts


async def test_unknown_bill_ref_is_recorded_with_no_order_link(pending, spy):
    await confirm(body(ref="NOT-AN-ORDER"))

    await assert_still_unpaid(pending)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(UnmatchedC2BPayment))).scalar_one()
    assert row.bill_ref_number == "NOT-AN-ORDER" and row.matched_order_id is None


async def test_missing_receipt_id_is_recorded_under_a_placeholder_never_applied(pending):
    b = body()
    b["TransID"] = None
    await confirm(b)

    await assert_still_unpaid(pending)
    async with AsyncSessionLocal() as s:
        row = (await s.execute(select(UnmatchedC2BPayment))).scalar_one()
    assert row.mpesa_receipt_number.startswith("unknown-")


async def test_missing_msisdn_is_not_auto_applied(pending):
    await confirm(body(msisdn=None))
    await assert_still_unpaid(pending)
    assert await count(UnmatchedC2BPayment) == 1


async def test_shortcode_mismatch_raises_an_alert(pending, spy):
    await confirm(body(shortcode="999999"))
    assert "C2B_SHORTCODE_MISMATCH" in spy.alerts


# ------------------------------------------------------------------------ auth

@pytest.mark.parametrize("secret", ["wrong-secret", None])
async def test_confirmation_with_bad_secret_is_rejected_without_side_effects(pending, secret):
    with pytest.raises(HTTPException) as exc:
        await escrow_main.c2b_confirmation(FakeRequest(body()), x_mpesa_secret=secret)
    assert exc.value.status_code == 401
    await assert_still_unpaid(pending)
    assert await count(UnmatchedC2BPayment) == 0


@pytest.mark.parametrize("secret", ["wrong-secret", None])
async def test_validation_with_bad_secret_is_rejected(pending, secret):
    with pytest.raises(HTTPException) as exc:
        await escrow_main.c2b_validation(FakeRequest(body()), x_mpesa_secret=secret)
    assert exc.value.status_code == 401


# ------------------------------------------------------ validation (no DB order needed)

async def test_validation_rejects_unknown_reference():
    assert (await validate(body(ref="NOT-AN-ORDER")))["ResultCode"] == "C2B00012"


async def test_validation_rejects_wrong_shortcode_before_touching_the_database():
    assert (await validate(body(shortcode="999999")))["ResultCode"] == "C2B00016"
