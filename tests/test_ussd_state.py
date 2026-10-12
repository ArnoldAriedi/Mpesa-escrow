import pytest
from sqlalchemy import select

from escrow_models import AsyncSessionLocal, USSDSession

@pytest.mark.asyncio
async def test_ussd_session_persists_state():
    async with AsyncSessionLocal() as db:
        session = USSDSession(session_id="ussd-test-1", phone_number="254700000001", state="MAIN")
        db.add(session)
        await db.commit()

    async with AsyncSessionLocal() as db:
        session = await db.scalar(select(USSDSession).where(USSDSession.session_id == "ussd-test-1"))
        session.state = "AWAITING_ORDER_REF_PAY"
        session.order_ref = "ORD-ABC123"
        await db.commit()

    async with AsyncSessionLocal() as db:
        session = await db.scalar(select(USSDSession).where(USSDSession.session_id == "ussd-test-1"))
        assert session.state == "AWAITING_ORDER_REF_PAY"
        assert session.order_ref == "ORD-ABC123"
