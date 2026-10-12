"""Small builders/helpers shared by the money-movement tests."""
import secrets
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select

from escrow_models import (
    AsyncSessionLocal, EscrowStatus, EscrowTransaction, LedgerAccount, LedgerEntry,
    LedgerEntryDirection, LedgerTransaction, Order, OrderStatus, PayeeRole, Payout,
)


class FakeRequest:
    """Just enough of a Starlette Request for the callback/admin handlers."""

    def __init__(self, body):
        self._body = body
        self.app = SimpleNamespace(state=SimpleNamespace(http_client=None))

    async def json(self):
        return self._body


def utcnow():
    return datetime.now(timezone.utc)


def ago(**kwargs):
    return utcnow() - timedelta(**kwargs)


async def make_escrow(db, *, status=EscrowStatus.DELIVERY_CONFIRMED, amount="500.00",
                      payee_role=PayeeRole.RIDER, order_ref=None, **extra):
    """Creates an escrow (and optionally its order). Flushes; caller commits."""
    order_id = None
    if order_ref:
        order = Order(
            public_ref=order_ref, buyer_phone="254700000001", amount=Decimal(amount),
            delivery_fee=Decimal("0.00"), delivery_address="Test address",
            status=OrderStatus.PAYMENT_PENDING,
        )
        db.add(order)
        await db.flush()
        order_id = order.id
    tx = EscrowTransaction(
        order_id=order_id, buyer_phone="254700000001", payee_phone="254700000002",
        payee_role=payee_role, amount=Decimal(amount), status=status,
        buyer_access_token=secrets.token_hex(16), **extra,
    )
    db.add(tx)
    await db.flush()
    return tx


async def make_payout(db, tx, *, status, purpose="RELEASE", **extra):
    payout = Payout(
        escrow_transaction_id=tx.id, purpose=purpose, recipient_phone=tx.payee_phone,
        amount=tx.amount, status=status, **extra,
    )
    db.add(payout)
    await db.flush()
    return payout


async def count(model, *where):
    async with AsyncSessionLocal() as s:
        return await s.scalar(select(func.count()).select_from(model).where(*where))


async def reload(model, pk):
    """Fresh read through a new session (never the test's own identity map)."""
    async with AsyncSessionLocal() as s:
        return await s.get(model, pk)


async def ledger_net(account_code, escrow_id):
    """DEBIT minus CREDIT posted to one system account for one escrow."""
    async with AsyncSessionLocal() as s:
        rows = (await s.execute(
            select(LedgerEntry.direction, LedgerEntry.amount)
            .join(LedgerTransaction, LedgerEntry.ledger_transaction_id == LedgerTransaction.id)
            .join(LedgerAccount, LedgerEntry.account_id == LedgerAccount.id)
            .where(LedgerAccount.code == account_code,
                   LedgerTransaction.escrow_transaction_id == escrow_id)
        )).all()
    net = Decimal("0")
    for direction, amount in rows:
        net += Decimal(amount) if direction == LedgerEntryDirection.DEBIT else -Decimal(amount)
    return net
