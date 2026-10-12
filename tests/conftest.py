import os
from pathlib import Path

TEST_DB = Path(__file__).parent / "test_escrow.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{TEST_DB}"
_db_url = os.environ["DATABASE_URL"]
if not _db_url.startswith("sqlite") and "test" not in _db_url.rsplit("/", 1)[-1]:
    raise RuntimeError("Refusing to run: the fixture drops all tables, so the database name must contain the word test.")
os.environ["DB_DISABLE_POOLING"] = "true"
os.environ.setdefault("ESCROW_API_KEY", "test-platform-key")
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
os.environ.setdefault("MPESA_CONSUMER_KEY", "test")
os.environ.setdefault("MPESA_CONSUMER_SECRET", "test")
os.environ.setdefault("MPESA_SHORTCODE", "174379")
os.environ.setdefault("MPESA_PASSKEY", "test")
os.environ.setdefault("MPESA_CALLBACK_BASE_URL", "http://test")
os.environ.setdefault("MPESA_INITIATOR_NAME", "test")
os.environ.setdefault("MPESA_INITIATOR_PASSWORD", "test")
os.environ.setdefault("MPESA_CALLBACK_SECRET", "test-secret")

import pytest_asyncio
from escrow_models import Base, engine, AsyncSessionLocal

@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

import pytest as _pytest_for_guard


@_pytest_for_guard.fixture(autouse=True)
def _ledger_guard_off_by_default(monkeypatch):
    """Legacy tests build escrows by hand without the payment hold entry. The H5 overdraft guard is
    exercised explicitly by tests that request the guard_on fixture."""
    import escrow_models

    monkeypatch.setattr(escrow_models, "LEDGER_OVERDRAFT_GUARD", False)


@_pytest_for_guard.fixture
def guard_on(monkeypatch, _ledger_guard_off_by_default):
    import escrow_models

    monkeypatch.setattr(escrow_models, "LEDGER_OVERDRAFT_GUARD", True)

@pytest_asyncio.fixture
async def db():
    async with AsyncSessionLocal() as session:
        yield session


# --- Shared fixtures for the money-movement tests --------------------------------
import asyncio
from types import SimpleNamespace

import pytest


class _AsyncioStopAtSleep:
    """Stand-in for the `asyncio` name inside escrow_main.

    The background loops are `while True: ...; await asyncio.sleep(N)`. Making
    sleep raise CancelledError turns "run forever" into "run exactly one
    sweep", which is also how the loops are shut down in production
    (task.cancel()), so it exercises the real exit path.
    """

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, _seconds):
        raise asyncio.CancelledError()


@pytest.fixture
def provider(monkeypatch):
    """Fakes every outbound Safaricom call and the ops-alert channel, and
    records what the code under test asked them to do."""
    import escrow_main

    p = SimpleNamespace(
        balance_calls=[], balance_exc=None,
        status_calls=[], status_exc_for=set(),
        b2c_calls=[], b2c_exc=None,
        alerts=[],
    )

    async def fake_balance(client, originator_conversation_id):
        p.balance_calls.append(originator_conversation_id)
        if p.balance_exc:
            raise p.balance_exc
        return {"ConversationID": f"AG_BAL_{len(p.balance_calls)}"}

    async def fake_status(client, transaction_id, originator_conversation_id, occasion=""):
        p.status_calls.append(transaction_id)
        if transaction_id in p.status_exc_for:
            raise RuntimeError("provider unavailable")
        return {"ConversationID": f"AG_Q_{len(p.status_calls)}"}

    async def fake_b2c(client, **kwargs):
        p.b2c_calls.append(kwargs)
        if p.b2c_exc:
            raise p.b2c_exc
        return {"ConversationID": f"AG_B2C_{len(p.b2c_calls)}"}

    async def fake_alert(kind, payout_id, escrow_transaction_id, detail):
        p.alerts.append((kind, payout_id, escrow_transaction_id))

    monkeypatch.setattr(escrow_main.mpesa, "query_account_balance", fake_balance)
    monkeypatch.setattr(escrow_main.mpesa, "query_transaction_status", fake_status)
    monkeypatch.setattr(escrow_main.mpesa, "initiate_b2c_payout", fake_b2c)
    monkeypatch.setattr(escrow_main, "alert_payout_issue", fake_alert)
    return p


@pytest.fixture
def run_once(monkeypatch, caplog, provider):
    """`await run_once(escrow_main.reconciliation_loop)` runs one full sweep.

    Fails the test if the sweep swallowed an exception (the loops catch and
    log everything so they never die, which would otherwise hide bugs)."""
    import escrow_main

    monkeypatch.setattr(escrow_main, "asyncio", _AsyncioStopAtSleep())

    async def _run(loop_fn, *, allow_errors=False):
        caplog.clear()
        with pytest.raises(asyncio.CancelledError):
            await loop_fn(None)
        if not allow_errors:
            assert "sweep failed" not in caplog.text, caplog.text

    return _run
