"""Regression tests: an HTTP 5xx from Safaricom on a money-moving request is an
UNKNOWN outcome (never a confirmed rejection), so it must never trigger a
ledger reversal or an automatic retry."""
import secrets
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import select

import escrow_core as mpesa
from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerTransaction, PayeeRole, Payout,
)


def _resp(code):
    return httpx.Response(code, request=httpx.Request("POST", "https://example.test/x"))


def test_server_errors_are_unknown_outcome():
    for code in (500, 502, 503, 504, 408):
        with pytest.raises(mpesa.MpesaOutcomeUnknown):
            mpesa.raise_for_definite_status(_resp(code))


def test_client_errors_are_definite_rejections():
    for code in (400, 401, 403, 404, 429):
        with pytest.raises(httpx.HTTPStatusError):
            mpesa.raise_for_definite_status(_resp(code))


def test_success_passes_through():
    mpesa.raise_for_definite_status(_resp(200))


@pytest.fixture
def stub_auth(monkeypatch):
    async def fake_token(client):
        return "tok"

    monkeypatch.setattr(mpesa, "get_access_token", fake_token)
    monkeypatch.setattr(mpesa, "generate_security_credential", lambda: "cred")
    monkeypatch.setattr(mpesa, "MPESA_SHORTCODE", "174379")
    monkeypatch.setattr(mpesa, "MPESA_PASSKEY", "test")
    monkeypatch.setattr(mpesa, "MPESA_CALLBACK_BASE_URL", "http://test")
    monkeypatch.setattr(mpesa, "MPESA_INITIATOR_NAME", "test")


BUILDERS = {
    "stk": lambda c: mpesa.initiate_stk_push(c, "254700000001", 100, "ESC1", "Order"),
    "b2c": lambda c: mpesa.initiate_b2c_payout(
        c, phone_number="254700000001", amount=100, remarks="r",
        originator_conversation_id="o-b2c", occasion="Escrow"),
    "reversal": lambda c: mpesa.initiate_reversal(
        c, transaction_id="QWE123", amount=100, remarks="r",
        originator_conversation_id="o-rev", occasion="Escrow"),
    "b2b": lambda c: mpesa.initiate_b2b_payout(
        c, receiver_shortcode="123456", receiver_channel="PAYBILL", amount=100, remarks="r",
        originator_conversation_id="o-b2b", account_reference="REF"),
}


def _client(status_code, body):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(status_code, json=body)))


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(BUILDERS))
async def test_gateway_error_means_outcome_unknown(stub_auth, name):
    async with _client(503, {"error": "bad gateway"}) as c:
        with pytest.raises(mpesa.MpesaOutcomeUnknown):
            await BUILDERS[name](c)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(BUILDERS))
async def test_client_error_is_a_definite_rejection(stub_auth, name):
    async with _client(400, {"errorMessage": "Bad Request"}) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await BUILDERS[name](c)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", list(BUILDERS))
async def test_accepted_request_returns_the_response(stub_auth, name):
    async with _client(200, {"ResponseCode": "0", "ConversationID": "AG_1"}) as c:
        data = await BUILDERS[name](c)
    assert data["ConversationID"] == "AG_1"


@pytest.mark.asyncio
async def test_payout_gateway_error_is_timeout_and_keeps_the_reservation(provider):
    import escrow_main

    provider.b2c_exc = mpesa.MpesaOutcomeUnknown("HTTP 503: outcome unknown")
    async with AsyncSessionLocal() as db:
        tx = EscrowTransaction(
            order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
            payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
            status=EscrowStatus.DELIVERY_CONFIRMED, buyer_access_token=secrets.token_hex(16))
        db.add(tx)
        await db.flush()
        po = Payout(escrow_transaction_id=tx.id, purpose="RELEASE",
                    recipient_phone=tx.payee_phone, amount=tx.amount, status="PENDING")
        db.add(po)
        await db.flush()
        tx_id, po_id = tx.id, po.id
        await db.commit()
        ok = await escrow_main.initiate_payout_for_record(db, po, None)

    assert ok is False
    async with AsyncSessionLocal() as s:
        fresh = await s.get(Payout, po_id)
        keys = (await s.execute(
            select(LedgerTransaction.idempotency_key)
            .where(LedgerTransaction.escrow_transaction_id == tx_id))).scalars().all()
    assert fresh.status == "TIMEOUT"
    assert fresh.timed_out_at is not None
    assert f"mpesa-settle-{po_id}-1" in keys
    assert f"mpesa-settle-reverse-{po_id}-1" not in keys
    assert ("PAYOUT_TIMEOUT", po_id, tx_id) in provider.alerts