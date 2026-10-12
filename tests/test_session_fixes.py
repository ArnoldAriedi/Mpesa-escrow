import pytest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import escrow_core as c
import escrow_main as m


def test_stk_query_still_processing_is_unknown():
    out = c._stk_query_outcome(200, {"ResponseCode": "0", "ResultCode": "4999"})
    assert out["outcome"] == "UNKNOWN"


def test_stk_query_real_failure_and_success_still_decided():
    assert c._stk_query_outcome(200, {"ResponseCode": "0", "ResultCode": "1032"})["outcome"] == "FAILED"
    assert c._stk_query_outcome(200, {"ResponseCode": "0", "ResultCode": "0"})["outcome"] == "PAID"


def test_preset_security_credential_ignored_outside_sandbox(monkeypatch):
    monkeypatch.setenv("MPESA_SECURITY_CREDENTIAL", "PRESET")
    monkeypatch.setattr(c, "MPESA_ENV", "production")
    monkeypatch.setattr(c, "MPESA_INITIATOR_PASSWORD", None)
    with pytest.raises(RuntimeError):
        c.generate_security_credential()


def test_preset_security_credential_used_in_sandbox(monkeypatch):
    monkeypatch.setenv("MPESA_SECURITY_CREDENTIAL", " PRESET ")
    monkeypatch.setattr(c, "MPESA_ENV", "sandbox")
    assert c.generate_security_credential() == "PRESET"


def _tx(hours_ago):
    naive = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).replace(tzinfo=None)
    return SimpleNamespace(payment_method="STK", mpesa_receipt_number="TESTRCPT", stk_callback_received_at=naive)


def test_refund_method_handles_naive_datetimes_from_sqlite():
    assert m._select_refund_method(_tx(1)) == "REVERSAL"
    assert m._select_refund_method(_tx(m.MPESA_REVERSAL_WINDOW_HOURS + 24)) == "B2C"
