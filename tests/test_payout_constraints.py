"""N19/C4: the database itself must reject invalid payout status/purpose/method
values, and originator_conversation_id (used to match late callbacks) must be
indexed."""
import secrets
from decimal import Decimal

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from escrow_models import AsyncSessionLocal, EscrowStatus, EscrowTransaction, PayeeRole, Payout, engine


async def _save_payout(**overrides):
    async with AsyncSessionLocal() as db:
        tx = EscrowTransaction(
            order_id=None, buyer_phone="254700000001", payee_phone="254700000002",
            payee_role=PayeeRole.RIDER, amount=Decimal("500.00"),
            status=EscrowStatus.DELIVERY_CONFIRMED, buyer_access_token=secrets.token_hex(16))
        db.add(tx)
        await db.flush()
        fields = dict(escrow_transaction_id=tx.id, purpose="RELEASE", recipient_phone="254700000002",
                      amount=Decimal("500.00"), status="PENDING", method="B2C")
        fields.update(overrides)
        db.add(Payout(**fields))
        await db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["PENDING", "SUBMITTING", "INITIATED", "COMPLETED", "FAILED", "TIMEOUT"])
async def test_every_status_the_code_uses_is_accepted(status):
    await _save_payout(status=status)


@pytest.mark.asyncio
async def test_every_purpose_and_method_the_code_uses_is_accepted():
    for purpose in ("RELEASE", "REFUND"):
        for method in ("B2C", "REVERSAL", "B2B"):
            await _save_payout(purpose=purpose, method=method)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("status", "BOGUS"), ("status", "pending"), ("purpose", "TOPUP"), ("method", "WIRE"),
])
async def test_invalid_values_are_rejected_by_the_database(field, value):
    with pytest.raises(IntegrityError):
        await _save_payout(**{field: value})


def test_originator_conversation_id_is_declared_as_an_index():
    # Checks the model declaration rather than querying the live database: raw
    # schema queries proved order-dependent after the concurrent-claim test.
    # The migration's index is proven by the Alembic chain step in CI (PostgreSQL).
    assert any(
        [c.name for c in ix.columns] == ["originator_conversation_id"]
        for ix in Payout.__table__.indexes
    )
