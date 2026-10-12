"""N13/N17: a funded order's payout cannot be redirected by reassigning the rider, and
riders can only move a delivery forward while the buyer's money is held in escrow."""
import secrets
from decimal import Decimal

import pytest
from fastapi import HTTPException

from escrow_models import (
    AsyncSessionLocal, DeliveryAgent, Delivery, DeliveryStatus, EscrowStatus, EscrowTransaction,
    Order, OrderStatus, PayeeRole, hash_api_key,
)


async def _agent(phone):
    async with AsyncSessionLocal() as db:
        a = DeliveryAgent(name="Rider", phone_number=phone, api_key_hash=hash_api_key(secrets.token_hex(8)))
        db.add(a)
        await db.flush()
        aid = a.id
        await db.commit()
        return aid


async def _order(*, escrow_status=EscrowStatus.ESCROW_HELD, delivery_status=DeliveryStatus.ASSIGNED,
                 agent_id=None, payee_phone="254700000002"):
    async with AsyncSessionLocal() as db:
        o = Order(public_ref=f"G{secrets.token_hex(3)}", buyer_phone="254700000001", amount=Decimal("500.00"),
                  delivery_fee=Decimal("0.00"), delivery_address="x", status=OrderStatus.PAID)
        db.add(o)
        await db.flush()
        tx = EscrowTransaction(order_id=o.id, buyer_phone="254700000001", payee_phone=payee_phone,
                               payee_role=PayeeRole.RIDER, amount=Decimal("500.00"), status=escrow_status,
                               buyer_access_token=secrets.token_hex(16), assigned_agent_id=agent_id)
        db.add(tx)
        db.add(Delivery(order_id=o.id, agent_id=agent_id, status=delivery_status))
        await db.flush()
        ids = (o.id, tx.id)
        await db.commit()
        return ids


async def _tx(tx_id):
    async with AsyncSessionLocal() as s:
        return await s.get(EscrowTransaction, tx_id)


async def _delivery(order_id):
    from sqlalchemy import select

    async with AsyncSessionLocal() as s:
        return await s.scalar(select(Delivery).where(Delivery.order_id == order_id))


class _Agent:
    def __init__(self, id):
        self.id = id


async def _call(fn, order_id, agent_id):
    return await fn(order_id, _Agent(agent_id))


# ---- assign_agent (N13) ----

@pytest.mark.asyncio
async def test_assign_before_payment_sets_the_rider_as_payee(provider):
    import escrow_main

    new_agent = await _agent("254711111111")
    order_id, tx_id = await _order(escrow_status=EscrowStatus.PENDING_PAYMENT, delivery_status=DeliveryStatus.UNASSIGNED)
    await escrow_main.assign_agent(order_id, new_agent, admin_actor="test-admin")
    tx = await _tx(tx_id)
    assert tx.assigned_agent_id == new_agent and tx.payee_phone == "254711111111"


@pytest.mark.asyncio
async def test_assign_after_payment_never_changes_the_payee(provider):
    import escrow_main

    new_agent = await _agent("254711111111")
    order_id, tx_id = await _order(escrow_status=EscrowStatus.ESCROW_HELD)
    await escrow_main.assign_agent(order_id, new_agent, admin_actor="test-admin")
    tx = await _tx(tx_id)
    assert tx.assigned_agent_id == new_agent
    assert tx.payee_phone == "254700000002"                    # money destination untouched


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [EscrowStatus.DELIVERY_CONFIRMED, EscrowStatus.RELEASED, EscrowStatus.DISPUTED,
                                    EscrowStatus.REFUND_INITIATED, EscrowStatus.PAYMENT_FAILED])
async def test_assign_is_refused_once_the_escrow_has_moved_on(provider, status):
    import escrow_main

    new_agent = await _agent("254711111111")
    order_id, tx_id = await _order(escrow_status=status)
    with pytest.raises(HTTPException) as e:
        await escrow_main.assign_agent(order_id, new_agent, admin_actor="test-admin")
    assert e.value.status_code == 409
    tx = await _tx(tx_id)
    assert tx.assigned_agent_id is None and tx.payee_phone == "254700000002"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [DeliveryStatus.PICKED_UP, DeliveryStatus.OUT_FOR_DELIVERY, DeliveryStatus.DELIVERED])
async def test_assign_is_refused_after_pickup(provider, status):
    import escrow_main

    old_agent = await _agent("254722222222")
    new_agent = await _agent("254711111111")
    order_id, tx_id = await _order(delivery_status=status, agent_id=old_agent)
    with pytest.raises(HTTPException) as e:
        await escrow_main.assign_agent(order_id, new_agent, admin_actor="test-admin")
    assert e.value.status_code == 409
    assert (await _tx(tx_id)).assigned_agent_id == old_agent
    assert (await _delivery(order_id)).status == status         # not reset


# ---- delivery handlers (N17) ----

@pytest.mark.asyncio
async def test_funded_order_moves_through_accept_pickup_out_for_delivery(provider):
    import escrow_main

    agent = await _agent("254722222222")
    order_id, _ = await _order(agent_id=agent)
    assert (await _call(escrow_main.accept_delivery, order_id, agent))["status"] == "ACCEPTED"
    assert (await _call(escrow_main.pickup, order_id, agent))["status"] == "PICKED_UP"
    assert (await _call(escrow_main.out_for_delivery, order_id, agent))["status"] == "OUT_FOR_DELIVERY"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [EscrowStatus.PENDING_PAYMENT, EscrowStatus.PAYMENT_FAILED, EscrowStatus.DISPUTED,
                                    EscrowStatus.REFUND_INITIATED, EscrowStatus.REFUNDED, EscrowStatus.EXPIRED])
async def test_unfunded_or_inactive_orders_cannot_be_handed_over(provider, status):
    import escrow_main

    agent = await _agent("254722222222")
    order_id, _ = await _order(escrow_status=status, agent_id=agent, delivery_status=DeliveryStatus.ACCEPTED)
    for fn in (escrow_main.accept_delivery, escrow_main.pickup):
        with pytest.raises(HTTPException) as e:
            await _call(fn, order_id, agent)
        assert e.value.status_code == 409
    assert (await _delivery(order_id)).status == DeliveryStatus.ACCEPTED


@pytest.mark.asyncio
async def test_out_for_delivery_refused_when_the_escrow_is_disputed(provider):
    import escrow_main

    agent = await _agent("254722222222")
    order_id, _ = await _order(escrow_status=EscrowStatus.DISPUTED, agent_id=agent, delivery_status=DeliveryStatus.PICKED_UP)
    with pytest.raises(HTTPException) as e:
        await _call(escrow_main.out_for_delivery, order_id, agent)
    assert e.value.status_code == 409
    assert (await _delivery(order_id)).status == DeliveryStatus.PICKED_UP


@pytest.mark.asyncio
async def test_a_rider_cannot_touch_someone_elses_delivery(provider):
    import escrow_main

    owner = await _agent("254722222222")
    stranger = await _agent("254733333333")
    order_id, _ = await _order(agent_id=owner)
    with pytest.raises(HTTPException) as e:
        await _call(escrow_main.accept_delivery, order_id, stranger)
    assert e.value.status_code == 404