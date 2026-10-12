"""Reconciliation sweep, payout retry safety, and the expiry sweep.

Every test here drives the real loop bodies for exactly one iteration (see the
`run_once` fixture in conftest.py) against a real SQLite database, with only the
Safaricom calls and the ops-alert channel faked.

The invariant behind most of these: when the outcome of a payment is UNKNOWN
(TIMEOUT / stuck SUBMITTING / INITIATED with no callback), automation may ASK
the provider and may FLAG for a human, but must never reverse the ledger
reservation or make the payout retryable on its own - that would risk paying
the recipient twice.
"""
import httpx
import pytest
from decimal import Decimal

import escrow_main
from escrow_models import (
    AccountBalanceCheck, AsyncSessionLocal, EscrowStatus, EscrowTransaction, Payout,
    TransactionStatusQuery, post_ledger_transaction,
)
from sqlalchemy import select
from support import ago, count, ledger_net, make_escrow, make_payout, reload, utcnow

STALE = escrow_main.PAYOUT_STALE_INITIATED_MINUTES + 60  # comfortably past the threshold


# ----------------------------------------------------------------- stale payouts

async def test_stale_initiated_payout_is_queried_but_not_resolved_automatically(db, run_once, provider):
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="INITIATED", conversation_id="AG_1",
                          last_attempt_at=ago(minutes=STALE))
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    assert provider.status_calls == ["AG_1"]
    q = (await _all(TransactionStatusQuery))[0]
    assert q.payout_id == p.id and q.status == "PENDING"
    # Asking the provider is all the sweep may do: the payout's own status is
    # only ever changed by a callback or a human.
    assert (await reload(Payout, p.id)).status == "INITIATED"


async def test_recent_initiated_payout_is_left_alone(db, run_once, provider):
    tx = await make_escrow(db)
    await make_payout(db, tx, status="INITIATED", conversation_id="AG_1", last_attempt_at=utcnow())
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    assert provider.status_calls == []
    assert await count(TransactionStatusQuery) == 0


async def test_status_query_prefers_receipt_over_conversation_id(db, run_once, provider):
    tx = await make_escrow(db)
    await make_payout(db, tx, status="INITIATED", conversation_id="AG_1",
                      transaction_id="RCPT1", last_attempt_at=ago(minutes=STALE))
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    assert provider.status_calls == ["RCPT1"]


async def test_stale_submitting_payout_with_a_provider_reference_is_queried_not_timed_out(db, run_once, provider):
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="SUBMITTING", conversation_id="AG_1",
                          last_attempt_at=ago(minutes=STALE))
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    assert provider.status_calls == ["AG_1"]
    assert (await reload(Payout, p.id)).status == "SUBMITTING"
    assert provider.alerts == []


@pytest.mark.parametrize("status", ["PENDING", "COMPLETED", "FAILED", "TIMEOUT"])
async def test_sweep_never_touches_payouts_in_other_states(db, run_once, provider, status):
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status=status, conversation_id="AG_1",
                          last_attempt_at=ago(minutes=STALE))
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    assert provider.status_calls == []
    assert (await reload(Payout, p.id)).status == status


async def test_sweep_queries_at_most_ten_payouts_oldest_first(db, run_once, provider):
    for i in range(12):
        tx = await make_escrow(db)
        await make_payout(db, tx, status="INITIATED", conversation_id=f"AG_{i:02d}",
                          last_attempt_at=ago(minutes=STALE))
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    assert provider.status_calls == [f"AG_{i:02d}" for i in range(10)]
    assert await count(TransactionStatusQuery) == 10


async def test_failed_status_query_does_not_mutate_payout_or_stop_the_batch(db, run_once, provider):
    tx1 = await make_escrow(db)
    tx2 = await make_escrow(db)
    p1 = await make_payout(db, tx1, status="INITIATED", conversation_id="AG_BAD",
                           last_attempt_at=ago(minutes=STALE))
    p2 = await make_payout(db, tx2, status="INITIATED", conversation_id="AG_GOOD",
                           last_attempt_at=ago(minutes=STALE))
    await db.commit()
    provider.status_exc_for = {"AG_BAD"}

    await run_once(escrow_main.reconciliation_loop)

    assert provider.status_calls == ["AG_BAD", "AG_GOOD"]  # kept going past the failure
    assert (await reload(Payout, p1.id)).status == "INITIATED"  # retryable next sweep, not dropped
    assert (await reload(Payout, p2.id)).status == "INITIATED"


# ------------------------------------------------- stuck SUBMITTING, nothing to query

async def test_submitting_with_no_provider_reference_becomes_timeout_never_failed(db, run_once, provider):
    refund_tx = await make_escrow(db, status=EscrowStatus.REFUND_FAILED)
    release_tx = await make_escrow(db, status=EscrowStatus.DELIVERY_CONFIRMED)
    refund = await make_payout(db, refund_tx, status="SUBMITTING", purpose="REFUND",
                               last_attempt_at=ago(minutes=STALE))
    release = await make_payout(db, release_tx, status="SUBMITTING", purpose="RELEASE",
                                last_attempt_at=ago(minutes=STALE))
    # The reservation that initiate_payout_for_record committed before the crash.
    await post_ledger_transaction(
        db, reference="MPESA-SETTLE-x", idempotency_key="mpesa-settle-x",
        description="reservation", escrow_transaction_id=release_tx.id,
        debit_account_code="RIDER_PAYABLE", credit_account_code="MPESA_CLEARING",
        amount=release.amount,
    )
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    for pid in (refund.id, release.id):
        cur = await reload(Payout, pid)
        # TIMEOUT => human must reconcile. FAILED would make it auto-retryable
        # and risk a double payment if Safaricom did receive the request.
        assert cur.status == "TIMEOUT"
        assert cur.timed_out_at is not None
        assert "needs manual reconciliation" in cur.result_desc
    assert (await reload(EscrowTransaction, refund_tx.id)).status == EscrowStatus.REFUND_INITIATED
    assert (await reload(EscrowTransaction, release_tx.id)).status == EscrowStatus.PAYOUT_FAILED
    assert provider.status_calls == []  # nothing to query with
    assert sorted(a for a in provider.alerts if a[0] == "PAYOUT_TIMEOUT") == sorted(
        [("PAYOUT_TIMEOUT", refund.id, 0), ("PAYOUT_TIMEOUT", release.id, 0)])
    # Reservation untouched: only reconcile-timeout may reverse it.
    assert await ledger_net("MPESA_CLEARING", release_tx.id) == Decimal("-500.00")


async def test_timeout_payouts_produced_by_the_sweep_are_not_swept_again(db, run_once, provider):
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="SUBMITTING", last_attempt_at=ago(minutes=STALE))
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)
    provider.alerts.clear()
    await run_once(escrow_main.reconciliation_loop)

    assert (await reload(Payout, p.id)).status == "TIMEOUT"
    assert provider.alerts == []  # no repeat alert, no provider query
    assert provider.status_calls == []


# ------------------------------------------------------------ balance check + reaping

async def test_balance_check_row_is_created_with_provider_conversation_id(db, run_once, provider):
    await run_once(escrow_main.reconciliation_loop)

    checks = await _all(AccountBalanceCheck)
    assert len(checks) == 1
    assert checks[0].status == "PENDING" and checks[0].conversation_id == "AG_BAL_1"


async def test_balance_check_failure_is_recorded_and_payout_sweep_still_runs(db, run_once, provider):
    tx = await make_escrow(db)
    await make_payout(db, tx, status="INITIATED", conversation_id="AG_1",
                      last_attempt_at=ago(minutes=STALE))
    await db.commit()
    provider.balance_exc = RuntimeError("daraja down")

    await run_once(escrow_main.reconciliation_loop)

    check = (await _all(AccountBalanceCheck))[0]
    assert check.status == "FAILED" and "daraja down" in check.result_desc
    assert provider.status_calls == ["AG_1"]


async def test_stale_pending_balance_checks_and_status_queries_time_out_fresh_ones_do_not(db, run_once, provider):
    old_bal = escrow_main.ACCOUNT_BALANCE_STALE_MINUTES + 30
    old_q = escrow_main.TRANSACTION_STATUS_QUERY_STALE_MINUTES + 30
    db.add_all([
        AccountBalanceCheck(conversation_id="B-OLD", status="PENDING", created_at=ago(minutes=old_bal)),
        AccountBalanceCheck(conversation_id="B-NEW", status="PENDING", created_at=utcnow()),
        AccountBalanceCheck(conversation_id="B-DONE", status="COMPLETED", created_at=ago(minutes=old_bal)),
        TransactionStatusQuery(query_conversation_id="Q-OLD", status="PENDING", created_at=ago(minutes=old_q)),
        TransactionStatusQuery(query_conversation_id="Q-NEW", status="PENDING", created_at=utcnow()),
        TransactionStatusQuery(query_conversation_id="Q-DONE", status="ANSWERED", created_at=ago(minutes=old_q)),
    ])
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    bal = {b.conversation_id: b for b in await _all(AccountBalanceCheck)}
    assert bal["B-OLD"].status == "TIMEOUT" and bal["B-OLD"].completed_at is not None
    assert bal["B-NEW"].status == "PENDING"
    assert bal["B-DONE"].status == "COMPLETED"
    qs = {q.query_conversation_id: q for q in await _all(TransactionStatusQuery)}
    assert qs["Q-OLD"].status == "TIMEOUT" and qs["Q-OLD"].answered_at is not None
    assert qs["Q-NEW"].status == "PENDING"
    assert qs["Q-DONE"].status == "ANSWERED"


async def test_status_query_created_this_sweep_is_not_reaped_in_the_same_sweep(db, run_once, provider):
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="INITIATED", conversation_id="AG_1",
                          last_attempt_at=ago(minutes=STALE))
    await db.commit()

    await run_once(escrow_main.reconciliation_loop)

    (q,) = [q for q in await _all(TransactionStatusQuery) if q.payout_id == p.id]
    assert q.status == "PENDING"


async def test_loop_survives_a_failing_sweep_and_reaches_sleep(monkeypatch, run_once):
    def broken_session():
        raise RuntimeError("db down")
    monkeypatch.setattr(escrow_main, "AsyncSessionLocal", broken_session)

    # run_once itself asserts the loop exited via the (faked) sleep, i.e. the
    # exception was caught rather than killing the background task.
    await run_once(escrow_main.reconciliation_loop, allow_errors=True)


# ------------------------------------------------------------ payout retry safety

async def test_transport_failure_leaves_timeout_reservation_intact_and_is_never_auto_retried(db, provider):
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="PENDING")
    await db.commit()
    provider.b2c_exc = httpx.ReadTimeout("no response")

    ok = await escrow_main.initiate_payout_for_record(db, p, None)

    assert ok is False
    cur = await reload(Payout, p.id)
    assert cur.status == "TIMEOUT" and cur.timed_out_at is not None
    # Reserved (credit to clearing), NOT reversed: Safaricom may still pay.
    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("-500.00")
    assert [a[0] for a in provider.alerts] == ["PAYOUT_TIMEOUT"]

    await escrow_main.retry_failed_payouts(db, None)
    await db.commit()
    assert len(provider.b2c_calls) == 1  # the retry sweep did not touch it


async def test_confirmed_rejection_reverses_reservation_and_retry_reserves_exactly_once(db, provider, monkeypatch):
    monkeypatch.setattr(escrow_main, "PAYOUT_RETRY_BACKOFF_SECONDS", 0)  # this test retries immediately; the backoff has its own tests
    tx = await make_escrow(db)
    p = await make_payout(db, tx, status="PENDING")
    await db.commit()
    provider.b2c_exc = escrow_main.mpesa.MpesaRejected("bad shortcode")

    assert await escrow_main.initiate_payout_for_record(db, p, None) is False
    assert (await reload(Payout, p.id)).status == "FAILED"
    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("0")  # reserved, then reversed

    provider.b2c_exc = None
    await escrow_main.retry_failed_payouts(db, None)
    await db.commit()

    cur = await reload(Payout, p.id)
    assert cur.status == "INITIATED" and cur.attempt_count == 2
    assert cur.conversation_id == "AG_B2C_2"
    assert await ledger_net("MPESA_CLEARING", tx.id) == Decimal("-500.00")  # one live reservation

    await escrow_main.retry_failed_payouts(db, None)  # INITIATED is not FAILED: no third call
    await db.commit()
    assert len(provider.b2c_calls) == 2


# ---------------------------------------------------------------------- expiry sweep

async def test_expiry_sweep_refunds_an_expired_escrow_exactly_once(db, run_once, provider):
    tx = await make_escrow(db, status=EscrowStatus.ESCROW_HELD, qr_token_expires_at=ago(minutes=5))
    await db.commit()

    await run_once(escrow_main.expiry_loop)
    await run_once(escrow_main.expiry_loop)  # a second sweep must be a no-op

    assert (await reload(EscrowTransaction, tx.id)).status == EscrowStatus.REFUND_INITIATED
    assert await count(Payout, Payout.escrow_transaction_id == tx.id, Payout.purpose == "REFUND") == 1
    assert len(provider.b2c_calls) == 1
    assert provider.b2c_calls[0]["phone_number"] == tx.buyer_phone


async def test_expiry_sweep_ignores_unexpired_and_non_held_escrows(db, run_once, provider):
    future = await make_escrow(db, status=EscrowStatus.ESCROW_HELD,
                               qr_token_expires_at=ago(minutes=-60))
    confirmed = await make_escrow(db, status=EscrowStatus.DELIVERY_CONFIRMED,
                                  qr_token_expires_at=ago(minutes=5))
    await db.commit()

    await run_once(escrow_main.expiry_loop)

    assert (await reload(EscrowTransaction, future.id)).status == EscrowStatus.ESCROW_HELD
    assert (await reload(EscrowTransaction, confirmed.id)).status == EscrowStatus.DELIVERY_CONFIRMED
    assert provider.b2c_calls == []


# ------------------------------------------------------------------------------ util

async def _all(model):
    async with AsyncSessionLocal() as s:
        return (await s.execute(select(model).order_by(model.id))).scalars().all()
